"""HTTP API: one process, one serialized SQLite writer, a small reader pool."""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import db as dbmod
from . import store
from .blobs import LocalBlobStore, new_hasher
from .config import DB_NAME, LIMITS
from .errors import ApiError, bad_request, forbidden


class NotInitialized(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


class Database:
    """Serialized writes on one connection/thread; reads on a bounded thread pool.

    Every write runs in BEGIN IMMEDIATE ... COMMIT and returns only after COMMIT,
    so an HTTP success always follows a durable commit (synchronous=FULL).
    """

    def __init__(self, path: Path):
        self.path = path
        self._writer_conn = dbmod.connect(path)
        self._writer = ThreadPoolExecutor(1, thread_name_prefix="msgboard-writer")
        self._readers = ThreadPoolExecutor(LIMITS.reader_threads, thread_name_prefix="msgboard-reader")
        self._local = threading.local()
        self._reader_conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.pending_writes = 0

    def _reader_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = dbmod.connect(self.path)
            self._local.conn = conn
            with self._lock:
                self._reader_conns.append(conn)
        return conn

    async def read(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._readers, lambda: fn(self._reader_conn(), *args))

    def _run_write(self, fn, args):
        conn = self._writer_conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    async def write(self, fn, *args):
        if self.pending_writes >= LIMITS.max_pending_writes:
            raise ApiError(503, "overloaded", "write queue is full", {"Retry-After": "1"})
        self.pending_writes += 1
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._writer, self._run_write, fn, args)
        finally:
            self.pending_writes -= 1

    def close(self) -> None:
        self._writer.shutdown(wait=True)
        self._readers.shutdown(wait=True)
        for conn in self._reader_conns:
            conn.close()
        try:
            self._writer_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._writer_conn.close()


class Notifier:
    """In-memory per-channel wakeups. Hints only: waiters always re-query the database."""

    def __init__(self):
        self._waiters: dict[int, set[asyncio.Future]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def register(self, channel_id: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(channel_id, set()).add(fut)
        return fut

    def unregister(self, channel_id: int, fut: asyncio.Future) -> None:
        waiters = self._waiters.get(channel_id)
        if waiters is not None:
            waiters.discard(fut)
            if not waiters:
                del self._waiters[channel_id]

    def notify(self, channel_id: int) -> None:
        for fut in self._waiters.pop(channel_id, ()):
            if not fut.done():
                fut.set_result(None)

    def wake_all(self) -> None:
        for channel_id in list(self._waiters):
            self.notify(channel_id)


class Board:
    """Everything one running server owns for a data directory."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        db_path = self.data_dir / DB_NAME
        if not db_path.exists():
            raise NotInitialized(f"no board in {self.data_dir}; run `python -m msgboard init` first")
        self._lock_fh = open(self.data_dir / "server.lock", "a")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_fh.close()
            raise AlreadyRunning(f"another server is already using {self.data_dir}")
        check = dbmod.connect(db_path)
        try:
            version = dbmod.schema_version(check)
            if version > dbmod.SCHEMA_VERSION:
                raise dbmod.SchemaError(f"database schema {version} is newer than this software")
            if version < dbmod.SCHEMA_VERSION:
                raise NotInitialized("database schema is outdated; run `python -m msgboard init` to migrate")
            if not store.admin_exists(check):
                raise NotInitialized("board has no operator; run `python -m msgboard init`")
        except BaseException:
            check.close()
            self._release_lock()
            raise
        check.close()
        self.db = Database(db_path)
        self.blobs = LocalBlobStore(self.data_dir / "blobs")
        self.notifier = Notifier()
        self.shutting_down = False
        self.open_polls = 0
        self.open_uploads = 0
        self.commit_lock: asyncio.Lock | None = None
        self.started = time.time()
        self.counters = {"posts": 0, "posts_replayed": 0, "polls": 0, "errors_5xx": 0}

    def begin_shutdown(self) -> None:
        """Release held long-polls so a Ctrl-C does not wait for them to time out."""
        self.shutting_down = True
        loop = self.notifier.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.notifier.wake_all)

    def _release_lock(self) -> None:
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
        finally:
            self._lock_fh.close()

    def close(self) -> None:
        self.db.close()
        self._release_lock()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


async def _json_body(request: Request, max_bytes: int = 256 * 1024):
    import json

    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > max_bytes:
            raise ApiError(413, "too_large", "request body too large")
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError:
        raise bad_request("request body is not valid JSON")


def create_app(board: Board) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        board.notifier.loop = asyncio.get_running_loop()
        board.commit_lock = asyncio.Lock()
        yield
        board.begin_shutdown()

    app = FastAPI(title="msgboard", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json")
    app.state.board = board
    db = board.db

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        if exc.status >= 500:
            board.counters["errors_5xx"] += 1
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                            status_code=exc.status, headers=exc.headers)

    async def authn(request: Request):
        return await db.read(store.authenticate, _bearer(request), time.time())

    # ------------------------------------------------------------- health

    @app.get("/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/ready")
    async def ready():
        await db.read(lambda c: c.execute("SELECT 1").fetchone())
        return {"status": "ready", "shutting_down": board.shutting_down}

    @app.get("/v1/metrics")
    async def metrics(request: Request):
        p = await authn(request)
        if not await db.read(store.is_admin, p.id):
            raise forbidden("admin only")
        db_path = board.data_dir / DB_NAME
        wal = Path(str(db_path) + "-wal")
        return {
            **board.counters,
            "open_polls": board.open_polls,
            "open_uploads": board.open_uploads,
            "pending_writes": db.pending_writes,
            "db_bytes": db_path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "disk_free_bytes": shutil.disk_usage(board.data_dir).free,
            "uptime_seconds": time.time() - board.started,
        }

    # ------------------------------------------------------- identities

    @app.get("/v1/me")
    async def get_me(request: Request):
        p = await authn(request)
        return await db.read(store.me, p)

    @app.post("/v1/agents")
    async def provision(request: Request):
        p = await authn(request)
        body = await _json_body(request)
        if not isinstance(body, dict):
            raise bad_request("request body must be a JSON object")
        agent, created = await db.write(store.provision_agent, p, body, time.time())
        return JSONResponse(agent, status_code=201 if created else 200)

    @app.get("/v1/agents/{agent_id}")
    async def get_agent(agent_id: int, request: Request):
        p = await authn(request)
        return await db.read(store.get_agent, p, agent_id)

    @app.post("/v1/agents/{agent_id}/token")
    async def issue_token(agent_id: int, request: Request):
        p = await authn(request)
        body = await _json_body(request)
        ttl = body.get("ttl_seconds") if isinstance(body, dict) else None
        result = await db.write(store.issue_token, p, agent_id, ttl, time.time())
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    @app.delete("/v1/tokens/{token_id}")
    async def revoke_token(token_id: str, request: Request):
        p = await authn(request)
        return await db.write(store.revoke_token, p, token_id, time.time())

    # --------------------------------------------------------- channels

    @app.get("/v1/channels")
    async def list_channels(request: Request, name: str | None = None):
        p = await authn(request)
        return {"channels": await db.read(store.list_channels, p, name)}

    @app.post("/v1/channels")
    async def create_channel(request: Request):
        p = await authn(request)
        body = await _json_body(request)
        if not isinstance(body, dict):
            raise bad_request("request body must be a JSON object")
        channel, created = await db.write(
            store.create_channel, p, body.get("name"), body.get("description"), time.time())
        return JSONResponse(channel, status_code=201 if created else 200)

    @app.post("/v1/channels/{channel_id}/archive")
    async def archive_channel(channel_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.archive_channel, p, channel_id, time.time())

    # --------------------------------------------------------- messages

    @app.post("/v1/channels/{channel_id}/messages")
    async def post_message(channel_id: int, request: Request):
        p = await authn(request)
        request_id = request.headers.get("idempotency-key")
        if not request_id or not store.CLIENT_ID.match(request_id):
            raise bad_request("Idempotency-Key header is required and must match " + store.CLIENT_ID.pattern)
        payload = await _json_body(request, LIMITS.max_body_bytes * 2 + LIMITS.max_metadata_bytes * 2)
        normalized, digest = store.normalize_payload(channel_id, payload)
        message, created = await db.write(
            store.post_message, p, channel_id, request_id, normalized, digest, time.time())
        if created:
            board.counters["posts"] += 1
            board.notifier.notify(channel_id)
            return JSONResponse(message, status_code=201)
        board.counters["posts_replayed"] += 1
        return JSONResponse(message, status_code=200, headers={"Idempotent-Replayed": "true"})

    @app.get("/v1/channels/{channel_id}/messages")
    async def read_messages(
        channel_id: int,
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(LIMITS.default_page_size, ge=1, le=LIMITS.max_page_size),
        wait: float = Query(0, ge=0, le=LIMITS.max_wait_seconds),
    ):
        p = await authn(request)
        if wait <= 0:
            return await db.read(store.read_messages, p, channel_id, after, limit)
        if board.open_polls >= LIMITS.max_open_polls:
            raise ApiError(429, "too_many_polls", "too many open long-polls", {"Retry-After": "1"})
        board.open_polls += 1
        board.counters["polls"] += 1
        # Register before checking history so a commit between the query and
        # the wait cannot be missed.
        fut = board.notifier.register(channel_id)
        try:
            result = await db.read(store.read_messages, p, channel_id, after, limit)
            if result["messages"] or board.shutting_down:
                return result
            await asyncio.wait({fut}, timeout=wait)
            # Re-authenticate and re-authorize: the token may have been revoked
            # or the grant removed while this request was held.
            p = await authn(request)
            return await db.read(store.read_messages, p, channel_id, after, limit)
        finally:
            board.notifier.unregister(channel_id, fut)
            board.open_polls -= 1

    # ------------------------------------------------------ attachments

    @app.put("/v1/channels/{channel_id}/attachments/{upload_id}")
    async def upload(channel_id: int, upload_id: str, request: Request):
        p = await authn(request)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > LIMITS.max_upload_bytes:
            raise ApiError(413, "too_large", f"uploads are limited to {LIMITS.max_upload_bytes} bytes")
        if board.open_uploads >= LIMITS.max_concurrent_uploads:
            raise ApiError(429, "too_many_uploads", "too many concurrent uploads", {"Retry-After": "1"})
        filename = os.path.basename(request.headers.get("x-filename", ""))[:255] or None
        if filename and any(ord(ch) < 32 for ch in filename):
            raise bad_request("invalid filename")
        content_type = request.headers.get("content-type", "application/octet-stream")[:128]
        board.open_uploads += 1
        tmp_path = None
        try:
            row = await db.write(store.begin_upload, p, channel_id, upload_id, filename, content_type, time.time())
            tmp_path, fh = await anyio.to_thread.run_sync(board.blobs.open_temp)
            hasher, size = new_hasher(), 0
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > LIMITS.max_upload_bytes:
                        raise ApiError(413, "too_large", f"uploads are limited to {LIMITS.max_upload_bytes} bytes")
                    hasher.update(chunk)
                    await anyio.to_thread.run_sync(fh.write, chunk)
                await anyio.to_thread.run_sync(board.blobs.finish_temp, fh)
            finally:
                if not fh.closed:
                    fh.close()
            digest = hasher.hexdigest()
            expected = request.headers.get("x-checksum-sha256")
            if expected and expected.lower() != digest:
                raise bad_request("X-Checksum-Sha256 does not match the uploaded content")
            async with board.commit_lock:
                current = await db.read(store.attachment_state, row["id"])
                if current["state"] == "ready":
                    result = await db.write(store.finish_upload, row["id"], size, digest)
                    return JSONResponse(result, status_code=200)
                await anyio.to_thread.run_sync(board.blobs.commit, tmp_path, row["storage_key"])
                tmp_path = None
                result = await db.write(store.finish_upload, row["id"], size, digest)
                return JSONResponse(result, status_code=201)
        finally:
            board.open_uploads -= 1
            if tmp_path is not None:
                board.blobs.discard(tmp_path)

    @app.get("/v1/attachments/{att_id}")
    async def download(att_id: str, request: Request):
        p = await authn(request)
        info = await db.read(store.get_attachment, p, att_id)
        return FileResponse(
            board.blobs.path_for(info["storage_key"]),
            media_type="application/octet-stream",
            filename=info["filename"] or att_id,
            headers={
                "X-Checksum-Sha256": info["sha256"],
                "X-Content-Type-Options": "nosniff",
                "X-Upload-Content-Type": info["content_type"] or "application/octet-stream",
            },
        )

    return app
