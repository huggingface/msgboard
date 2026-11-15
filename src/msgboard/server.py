"""HTTP API: one process, one serialized SQLite writer, a small reader pool."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import mimetypes
import os
import random
import shutil
import sqlite3
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import unquote

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask

from . import db as dbmod
from . import guide
from . import store
from .blobs import LocalBlobStore, new_hasher
from .config import DB_NAME, LIMITS
from .errors import ApiError, bad_request, forbidden
from .presence import PRESENCE_FLUSH_SECONDS, Presence
from . import transfer
from . import queue as personal_queue
from .reservations import Reservations
from .auth import DOWNLOAD_TTL_SECONDS, DownloadLinks


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
        self.queued_reads = 0
        self.shed_reads = 0
        self.slow_reads = 0
        # token -> (Principal, checked at): see LIMITS.auth_cache_seconds
        self.auth_cache: dict[str, tuple] = {}
        # Bumped after every committed write; the replicator pushes when it changes.
        self.generation = 0
        self.closing = False

    def _reader_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = dbmod.connect(self.path)
            local = self._local

            def over_time():
                deadline = getattr(local, "deadline", None)
                return 1 if deadline is not None and time.monotonic() > deadline else 0
            conn.set_progress_handler(over_time, 20_000)
            self._local.conn = conn
            with self._lock:
                self._reader_conns.append(conn)
        return conn

    async def read(self, fn, *args, shed: bool = True):
        """Run a read on the reader pool. Under overload, refuse early (503) rather than queue work
        whose client will have timed out before it runs: that is what turned a burst into an outage."""
        if shed and self.queued_reads >= LIMITS.max_queued_reads:
            self.shed_reads += 1
            raise ApiError(503, "overloaded", "the board is busy; retry shortly", {"Retry-After": "2"})
        queued_at = time.monotonic()

        def run():
            if time.monotonic() - queued_at > LIMITS.read_queue_timeout:
                self.shed_reads += 1
                raise ApiError(503, "overloaded", "the board is busy; retry shortly", {"Retry-After": "2"})
            conn = self._reader_conn()
            self._local.deadline = time.monotonic() + LIMITS.max_read_seconds
            try:
                return fn(conn, *args)
            except sqlite3.OperationalError as e:
                if "interrupt" in str(e):
                    self.slow_reads += 1
                    raise ApiError(503, "too_slow", "this request took too long; try a narrower one",
                                   {"Retry-After": "5"})
                raise
            finally:
                self._local.deadline = None

        self.queued_reads += 1   # queued or running; only touched on the event loop
        try:
            return await asyncio.get_running_loop().run_in_executor(self._readers, run)
        finally:
            self.queued_reads -= 1

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
        if self.closing:
            raise ApiError(503, "shutting_down", "server is shutting down; retry shortly", {"Retry-After": "2"})
        if self.pending_writes >= LIMITS.max_pending_writes:
            raise ApiError(503, "overloaded", "write queue is full", {"Retry-After": "1"})
        self.pending_writes += 1
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(self._writer, self._run_write, fn, args)
            self.generation += 1
            if fn not in AUTH_SAFE_WRITES:
                self.auth_cache.clear()
            return result
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


ANY_CHANNEL = -1   # notifier key woken by a post in any channel (activity long-polls wait on it)
# Writes that cannot change who a token belongs to or what it may do; every other write clears the auth cache.
AUTH_SAFE_WRITES = frozenset({store.post_message, store.react, store.mark_read, store.mark_thread_read,
                              store.mark_all_read, store.save_presence})


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

    def __init__(self, data_dir: Path, replicator=None):
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
        # one-time maintenance on the writer connection before serving
        with self.db._lock:
            conn = self.db._writer_conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                store.backfill_mentions(conn)
                store.backfill_mentioned_as(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        self.presence = Presence(store.load_presence(conn))
        self.blobs = LocalBlobStore(self.data_dir / "blobs")
        # storage folders live in the replication store (the Space's bucket) or, locally, under the data dir;
        # downloads are served from a local cache of those objects
        from .replicate import DirStore
        self.storage = replicator.store if replicator is not None else DirStore(self.data_dir)
        self.storage_cache = self.data_dir / "storage-cache"
        self.storage_cache.mkdir(exist_ok=True)
        self.notifier = Notifier()
        self.shutting_down = False
        self.open_polls = 0
        self.open_uploads = 0
        self.commit_lock: asyncio.Lock | None = None
        self.storage_reservations = Reservations(paths=True)
        self.import_reservations = Reservations()
        self.started = time.time()
        self.counters = {"posts": 0, "posts_replayed": 0, "edits": 0, "polls": 0, "errors_5xx": 0}
        self.replicator = replicator
        if replicator is not None:
            replicator.start(lambda: self.db.generation)

    def begin_shutdown(self) -> None:
        """Release held long-polls so a Ctrl-C does not wait for them to time out."""
        if self.replicator is not None and not self.shutting_down:
            self.replicator.record("shutdown-signal")
        self.shutting_down = True
        if self.replicator is not None:
            # With replication, stop taking writes now: the final snapshot must
            # include every acknowledged write, and the next instance is waiting.
            self.db.closing = True
        loop = self.notifier.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.notifier.wake_all)

    def _release_lock(self) -> None:
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
        finally:
            self._lock_fh.close()

    async def flush_presence(self) -> None:
        versions, records = self.presence.pending()
        history = self.presence.pending_history()
        if records or history:
            await self.db.write(store.save_presence, records, history)
            self.presence.saved(versions)
            self.presence.history_saved(history)

    async def presence_loop(self) -> None:
        while True:
            await asyncio.sleep(PRESENCE_FLUSH_SECONDS)
            if self.shutting_down:
                return
            self.presence.sample(time.time())
            try:
                await self.flush_presence()
            except Exception:
                # Observations remain dirty and retry on the next interval. Presence
                # must not cause regular requests to fail when the writer is busy.
                logging.getLogger(__name__).exception("could not persist agent presence")

    async def queue_notice_loop(self) -> None:
        """Deliver coalesced queue changes through ordinary agent-owner DMs.

        The pending batch and its message are committed together by the writer;
        cancelling this task or restarting the server cannot duplicate delivery.
        """
        while True:
            await asyncio.sleep(1)
            if self.shutting_down:
                return
            try:
                if not await self.db.read(personal_queue.notice_due, time.time()):
                    continue
                message = await self.db.write(personal_queue.deliver_notice, time.time())
                if message:
                    self.counters["posts"] += 1
                    self.notifier.notify(message["channel_id"])
                    self.notifier.notify(ANY_CHANNEL)
            except Exception:
                logging.getLogger(__name__).exception("could not deliver queue update")

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        # The event loop has stopped. Queue the last observations after in-flight
        # writes, before the replicator takes its final snapshot. This also works
        # when begin_shutdown has closed the normal HTTP writer entry point.
        versions, records = self.presence.pending()
        history = self.presence.pending_history()
        if records or history:
            try:
                self.db._writer.submit(self.db._run_write, store.save_presence, (records, history)).result()
                self.db.generation += 1
                self.presence.saved(versions)
                self.presence.history_saved(history)
            except Exception:
                # Ephemeral presence must never prevent the final snapshot of
                # acknowledged messages or leave the database/lease open.
                logging.getLogger(__name__).exception("could not persist final agent presence")
        if self.replicator is not None:
            self.replicator.stop(final_push=True)  # after in-flight writes, before closing
        self.db.close()
        self._release_lock()


def _bearer(request: Request) -> str | None:
    # X-Msgboard-Token lets clients reach the board through a proxy that uses
    # Authorization for its own login (for example a private HF Space).
    direct = request.headers.get("x-msgboard-token")
    if direct:
        return direct.strip()
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


def create_app(board: Board, dashboard: bool = True) -> FastAPI:
    download_links = DownloadLinks()  # process-local and bounded; restarts invalidate outstanding links

    @asynccontextmanager
    async def lifespan(app):
        board.notifier.loop = asyncio.get_running_loop()
        board.commit_lock = asyncio.Lock()
        board.presence.sample(time.time())
        task = asyncio.create_task(board.presence_loop())
        queue_task = asyncio.create_task(board.queue_notice_loop())
        try:
            yield
        finally:
            board.begin_shutdown()
            task.cancel()
            queue_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await queue_task

    app = FastAPI(title="msgboard", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json")
    app.state.board = board
    db = board.db

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        if exc.status >= 500:
            board.counters["errors_5xx"] += 1
        body = {"code": exc.code, "message": exc.message}
        if exc.details:
            body["details"] = exc.details
        return JSONResponse({"error": body}, status_code=exc.status, headers=exc.headers)

    async def authn(request: Request, *, observe: bool = True):
        token = _bearer(request)
        now = time.monotonic()
        hit = db.auth_cache.get(token) if token else None
        if hit is not None and now - hit[1] < LIMITS.auth_cache_seconds:
            if observe:
                board.presence.seen(hit[0], time.time())
            return hit[0]
        p = await db.read(store.authenticate, token, time.time())
        if len(db.auth_cache) > 20_000:
            db.auth_cache.clear()
        db.auth_cache[token] = (p, now)
        if observe:
            board.presence.seen(p, time.time())
        return p

    async def with_presence(p, items):
        return await db.read(store.attach_presence, p, items, board.presence.snapshot())

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
        if not p.is_admin:
            raise forbidden("admin only")
        return health_summary()

    def health_summary():
        db_path = board.data_dir / DB_NAME
        wal = Path(str(db_path) + "-wal")
        return {
            **board.counters,
            "open_polls": board.open_polls,
            "open_uploads": board.open_uploads,
            "pending_writes": db.pending_writes,
            "queued_reads": db.queued_reads,
            "shed_reads": db.shed_reads,
            "slow_reads": db.slow_reads,
            "db_bytes": db_path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "disk_free_bytes": shutil.disk_usage(board.data_dir).free,
            "uptime_seconds": time.time() - board.started,
            "replication": board.replicator.status if board.replicator else None,
        }

    # ------------------------------------------------------- identities

    def body_dict(body):
        if not isinstance(body, dict):
            raise bad_request("request body must be a JSON object")
        return body

    @app.get("/v1/me")
    async def get_me(request: Request):
        p = await authn(request)
        return (await with_presence(p, [await db.read(store.me, p)]))[0]

    @app.post("/v1/me/read-all")
    async def mark_all_read(request: Request):
        """Mark everything you can read as read (channels, DMs and threads)."""
        p = await authn(request)
        return await db.write(store.mark_all_read, p, time.time())

    @app.put("/v1/me/prefs")
    async def put_prefs(request: Request):
        p = await authn(request)
        return await db.write(store.update_prefs, p, await _json_body(request, 64 * 1024), time.time())

    @app.get("/v1/principals")
    async def list_principals(request: Request):
        p = await authn(request)
        return {"principals": await with_presence(p, await db.read(store.list_principals, p))}

    @app.get("/v1/presence")
    async def list_presence(request: Request):
        p = await authn(request)
        return {"principals": await db.read(store.list_presence, p, board.presence.snapshot())}

    @app.patch("/v1/principals/{principal_id}")
    async def update_principal(principal_id: int, request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        result = await db.write(store.update_principal, p, principal_id, body, time.time())
        return (await with_presence(p, [result]))[0]

    @app.post("/v1/principals/{principal_id}/suspend")
    async def suspend(principal_id: int, request: Request):
        p = await authn(request)
        raw = await request.body()
        body = body_dict(await _json_body(request)) if raw.strip() else {}
        return await db.write(store.suspend, p, principal_id, body.get("reason"), time.time())

    @app.post("/v1/principals/{principal_id}/unsuspend")
    async def unsuspend(principal_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.unsuspend, p, principal_id, time.time())

    @app.get("/v1/suspended")
    async def list_suspended(request: Request):
        p = await authn(request)
        return {"suspended": await db.read(store.list_suspended, p)}

    @app.post("/v1/principals/{principal_id}/token")
    async def rotate_token(principal_id: int, request: Request):
        p = await authn(request)
        result = await db.write(store.rotate_agent_token, p, principal_id, time.time())
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    @app.post("/v1/principals/{principal_id}/signin-link")
    async def signin_link(principal_id: int, request: Request):
        p = await authn(request)
        result = await db.write(store.create_signin_link, p, principal_id, time.time())
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    @app.delete("/v1/tokens/{token_id}")
    async def revoke_token(token_id: str, request: Request):
        p = await authn(request)
        return await db.write(store.revoke_token, p, token_id, time.time())

    # ------------------------------------------------- invites and joining

    @app.post("/v1/invites")
    async def create_invite(request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        return JSONResponse(await db.write(store.create_invite, p, body, time.time()), status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/invites")
    async def list_invites(request: Request):
        p = await authn(request)
        return {"invites": await db.read(store.list_invites, p, time.time())}

    @app.delete("/v1/invites/{invite_id}")
    async def revoke_invite(invite_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.revoke_invite, p, invite_id, time.time())

    @app.get("/v1/join/{code}")
    async def lookup_invite(code: str):
        return await db.read(store.lookup_invite, code, time.time())

    @app.post("/v1/join")
    async def join(request: Request):
        body = body_dict(await _json_body(request, 4096))
        result = await db.write(store.join, body.get("code"), body.get("name"), body.get("display_name"), time.time())
        return JSONResponse(result, status_code=201 if result["created"] else 200, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ settings

    @app.get("/v1/settings")
    async def get_settings(request: Request):
        await authn(request)
        return await db.read(store.get_settings)

    @app.patch("/v1/settings")
    async def update_settings(request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        return await db.write(store.update_settings, p, body, time.time())

    # --------------------------------------------------------- channels

    @app.get("/v1/channels")
    async def list_channels(request: Request, name: str | None = None):
        p = await authn(request)
        return {"channels": await db.read(store.list_channels, p, name)}

    @app.post("/v1/channels")
    async def create_channel(request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        channel, created = await db.write(store.create_channel, p, body.get("name"), body.get("description"),
                                          body.get("private", False), time.time())
        return JSONResponse(channel, status_code=201 if created else 200)

    @app.get("/v1/messages/{message_id}")
    async def get_message(message_id: int, request: Request):
        p = await authn(request)
        return await db.read(store.get_message, p, message_id)

    @app.get("/v1/channels/{channel_id}/export")
    async def export_channel(channel_id: int, request: Request):
        p = await authn(request)
        transfer.require_admin(p)
        manifest, keys = await db.read(transfer.export_manifest, p, channel_id, _origin(request))
        scratch = tempfile.TemporaryDirectory(prefix="msgboard-export-")
        path = Path(scratch.name) / "channel.zip"
        try:
            await anyio.to_thread.run_sync(transfer.build_bundle, manifest, keys, board.blobs, path, board.storage)
        except BaseException:
            scratch.cleanup()
            raise
        return FileResponse(path, media_type="application/zip", filename=f"channel-{channel_id}.zip",
                            background=BackgroundTask(scratch.cleanup))

    @app.post("/v1/channels/{channel_id}/import")
    async def import_channel(channel_id: int, request: Request, dry_run: bool = False, keep_target_doc: bool = False):
        p = await authn(request, observe=not dry_run)
        await db.read(transfer.public_channel, p, channel_id)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > transfer.MAX_BUNDLE_BYTES:
            raise ApiError(413, "too_large", "channel bundle exceeds 512 MiB")
        with tempfile.TemporaryDirectory(prefix="msgboard-import-") as scratch:
            path = Path(scratch) / "bundle.zip"
            with path.open("xb") as fh:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > transfer.MAX_BUNDLE_BYTES:
                        raise ApiError(413, "too_large", "channel bundle exceeds 512 MiB")
                    await anyio.to_thread.run_sync(fh.write, chunk)
            manifest, digest, files = await anyio.to_thread.run_sync(transfer.read_bundle, path, Path(scratch))
            if dry_run:
                report = await db.read(transfer.plan_import, p, channel_id, manifest, digest, keep_target_doc)
                report = await anyio.to_thread.run_sync(transfer.verify_reused_storage, report, board.storage)
            else:
                # A disconnected caller must not remove staged local files while
                # its publication task is still using them. Retries are idempotent.
                task = asyncio.create_task(apply_history(p, channel_id, manifest, digest, files, keep_target_doc))
                try:
                    report = await asyncio.shield(task)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await task
                    raise
            return {**report, "dry_run": dry_run}

    async def apply_history(p, channel_id, manifest, digest, files, keep_target_doc):
        keys = [("channel", str(channel_id))] + [("author", a["external_id"].lower()) for a in manifest["principals"]]
        async with board.import_reservations.hold(keys):
            prepared = await db.write(transfer.prepare_import, p, channel_id, manifest, digest, time.time(), keep_target_doc)
            paths = [(prepared["people"][f["owner_id"]], f["path"]) for f in manifest["storage_files"]]
            published = False
            uploaded_storage, uploaded_attachments = [], []
            try:
                async with board.storage_reservations.hold(paths):
                    try:
                        prepared["report"] = await db.read(transfer.checked_import_plan, p, channel_id, manifest, digest, prepared)
                        await anyio.to_thread.run_sync(transfer.stage_import, channel_id, manifest, files, prepared,
                                                      board.blobs, board.storage, uploaded_storage, uploaded_attachments)
                        report = await db.write(transfer.import_channel, p, channel_id, manifest, digest, prepared, time.time())
                        published = True
                        return report
                    finally:
                        if not published:
                            if uploaded_storage:
                                try:
                                    await anyio.to_thread.run_sync(board.storage.delete, uploaded_storage)
                                except Exception:
                                    logging.getLogger(__name__).exception("could not remove unindexed import files")
                            for key in uploaded_attachments:
                                board.blobs.discard(board.blobs.path_for(key))
            finally:
                if not published:
                    await db.write(transfer.cleanup_ghosts, prepared)

    @app.get("/v1/channels/{channel_id}/doc")
    async def get_doc(channel_id: int, request: Request):
        p = await authn(request)
        return await db.read(store.get_doc, p, channel_id)

    @app.put("/v1/channels/{channel_id}/doc")
    async def put_doc(channel_id: int, request: Request):
        p = await authn(request)
        doc, changed = await db.write(store.save_doc, p, channel_id,
                                      await _json_body(request, store.MAX_DOC_BYTES * 2 + 4096), time.time())
        return doc

    @app.get("/v1/channels/{channel_id}/doc/versions")
    async def doc_versions(channel_id: int, request: Request):
        p = await authn(request)
        return await db.read(store.doc_versions, p, channel_id)

    @app.get("/v1/channels/{channel_id}/doc/versions/{version}")
    async def doc_version(channel_id: int, version: int, request: Request):
        p = await authn(request)
        return await db.read(store.doc_version, p, channel_id, version)

    @app.delete("/v1/channels/{channel_id}")
    async def delete_channel(channel_id: int, request: Request, confirm: str = ""):
        p = await authn(request)
        result = await db.write(store.delete_channel, p, channel_id, confirm, time.time())
        for key in result.pop("storage_keys"):   # the rows are gone; now the files
            try:
                board.blobs.path_for(key).unlink()
            except FileNotFoundError:
                pass
        board.notifier.notify(channel_id)
        return result

    @app.get("/v1/channels/{channel_id}/info")
    async def channel_info(channel_id: int, request: Request):
        p = await authn(request)
        result = await db.read(store.channel_info, p, channel_id)
        result["members"] = await with_presence(p, result["members"])
        result["posters"] = await with_presence(p, result["posters"])
        return result

    @app.patch("/v1/channels/{channel_id}")
    async def update_channel(channel_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.update_channel, p, channel_id, await _json_body(request, 4096), time.time())

    @app.post("/v1/channels/{channel_id}/archive")
    async def archive_channel(channel_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.archive_channel, p, channel_id, time.time())

    @app.post("/v1/channels/{channel_id}/threads/{thread_id}/read")
    async def mark_thread_read(channel_id: int, thread_id: int, request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request, 1024))
        return await db.write(store.mark_thread_read, p, channel_id, thread_id, body.get("seq"), time.time())

    @app.post("/v1/channels/{channel_id}/read")
    async def mark_read(channel_id: int, request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request, 1024))
        return await db.write(store.mark_read, p, channel_id, body.get("seq"), time.time())

    @app.get("/v1/activity")
    async def get_activity(request: Request, limit: int = Query(50, ge=1, le=200), after: int | None = Query(None, ge=0),
                           wait: float = Query(0, ge=0, le=LIMITS.max_wait_seconds), channels: str | None = None,
                           reactions: bool = False):
        """Everything for you (mentions, replies, DMs, plus `channels` you follow). With `after`, a
        cursor read, oldest first; add `wait` to long-poll it."""
        p = await authn(request)
        parts = [x.strip() for x in channels.split(",") if x.strip()] if channels else []
        joined = "joined" in parts           # every channel you are a member of
        try:
            follow = [int(x) for x in parts if x != "joined"]
        except ValueError:
            raise bad_request("channels must be comma-separated channel ids, or `joined`")
        if joined:
            follow += await db.read(store.joined_channel_ids, p)
        if len(follow) > 100:
            raise bad_request("follow at most 100 channels")
        if wait > 0 and after is None:
            raise bad_request("wait needs a cursor: pass `after` (start from the next_cursor of a plain read)")
        token = board.presence.next_poll()
        polled_at = time.time()
        mode = "joined" if joined else ("channels" if follow else "addressed")

        def observe(result):
            board.presence.polled(p, token, polled_at, after, mode, follow, bool(result["messages"]))

        if wait <= 0:
            result = await db.read(store.activity, p, limit, after, follow, reactions)
            observe(result)
            return result
        if board.open_polls >= LIMITS.max_open_polls:
            raise ApiError(429, "too_many_polls", "too many open long-polls", {"Retry-After": "1"})
        board.open_polls += 1
        board.counters["polls"] += 1
        fut = board.notifier.register(ANY_CHANNEL)
        try:
            result = await db.read(store.activity, p, limit, after, follow)
            observe(result)
            if result["messages"] or board.shutting_down:
                return result
            deadline = time.monotonic() + wait
            while True:   # any post wakes us; keep waiting until one is actually for this caller
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                done, _ = await asyncio.wait({fut}, timeout=left)
                if done and not board.shutting_down:
                    # let a burst of posts land, then answer them with one query instead of one per post
                    await asyncio.sleep(min(LIMITS.poll_coalesce_seconds * (0.75 + random.random() / 2),
                                            max(0.0, deadline - time.monotonic())))
                if await request.is_disconnected():
                    return result
                board.notifier.unregister(ANY_CHANNEL, fut)
                fut = board.notifier.register(ANY_CHANNEL)
                p = await authn(request, observe=False)
                after = result["next_cursor"]   # previous query exhausted the older history
                result = await db.read(store.activity, p, limit, after, follow, shed=False)
                board.presence.observed(p, token, bool(result["messages"]), time.time())
                if result["messages"] or board.shutting_down:
                    return result
            return result
        finally:
            board.notifier.unregister(ANY_CHANNEL, fut)
            board.open_polls -= 1

    @app.get("/v1/search")
    async def search(request: Request, q: str = "", channel: int | None = None, sender: int | None = None,
                     after: str | None = None, before: str | None = None, sort: str = "newest",
                     limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=10_000)):
        """Full-text search over messages you can read, plus matching channel and people names.
        `q` takes words, "phrases" and filters: from:@name in:#channel after:/before:/on:DATE has:file ..."""
        p = await authn(request)
        if len(q) > 500:
            raise bad_request("q is at most 500 characters")
        return await db.read(lambda conn: store.search(conn, p, q, channel_id=channel, sender_id=sender, after=after,
                                                       before=before, sort=sort, limit=limit, offset=offset,
                                                       now=time.time()))

    @app.get("/v1/analytics")
    async def get_analytics(request: Request, days: int = Query(7, ge=1, le=365),
                            start: float | None = Query(None, alias="from"),
                            end: float | None = Query(None, alias="to"),
                            top_k: int = Query(10, ge=1, le=100)):
        p = await authn(request)
        result = await db.read(store.analytics, p, time.time(), days, start, end, top_k,
                               board.presence.pending_history())
        result["listening"]["now"] = board.presence.listening_now(time.time())
        result["health"] = health_summary()
        return result

    def _origin(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}{request.scope.get('root_path', '')}"

    @app.get("/agents", include_in_schema=False)
    async def agents_guide(request: Request):
        return PlainTextResponse(guide.render(_origin(request), public=True), media_type="text/markdown; charset=utf-8")

    @app.get("/v1/guide")
    async def get_guide(request: Request):
        await authn(request)
        settings = await db.read(store.get_settings)
        return PlainTextResponse(guide.render(_origin(request), settings["guidelines"], docs=settings["channel_docs"]),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/v1/channels/{channel_id}/members")
    async def list_members(channel_id: int, request: Request):
        p = await authn(request)
        return {"members": await with_presence(p, await db.read(store.list_members, p, channel_id))}

    @app.post("/v1/channels/{channel_id}/members")
    async def add_member(channel_id: int, request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        members = await db.write(store.add_member, p, channel_id, body.get("principal_id"), time.time())
        return {"members": await with_presence(p, members)}

    @app.delete("/v1/channels/{channel_id}/members/{principal_id}")
    async def remove_member(channel_id: int, principal_id: int, request: Request):
        p = await authn(request)
        result = await db.write(store.remove_member, p, channel_id, principal_id, time.time())
        return result if isinstance(result, dict) else {"members": await with_presence(p, result)}

    @app.post("/v1/dms")
    async def open_dm(request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request))
        channel, created = await db.write(store.open_dm, p, body.get("with"), time.time())
        return JSONResponse(channel, status_code=201 if created else 200)

    # --------------------------------------------------------- messages

    @app.get("/v1/queue")
    async def get_queue(request: Request, archived: bool = False, sort: str = "manual", direction: str = "asc"):
        p = await authn(request)
        return await db.read(personal_queue.list_items, p, archived, sort, direction)

    @app.get("/v1/queue/items/{item_id}")
    async def get_queue_item(item_id: int, request: Request):
        p = await authn(request)
        return await db.read(personal_queue.get_item, p, item_id)

    @app.put("/v1/queue/items/by-key/{key}")
    async def put_queue_item(key: str, request: Request):
        p = await authn(request)
        payload = body_dict(await _json_body(request))
        return await db.write(personal_queue.put_item, p, key, payload, time.time())

    @app.post("/v1/queue/items/{item_id}/actions")
    async def queue_action(item_id: int, request: Request):
        p = await authn(request)
        key = request.headers.get("idempotency-key")
        if not key or not store.CLIENT_ID.fullmatch(key):
            raise bad_request("Idempotency-Key header is required and must match " + store.CLIENT_ID.pattern)
        payload = body_dict(await _json_body(request, LIMITS.max_body_bytes * 2 + 64 * 1024))
        result, message, replayed = await db.write(personal_queue.action, p, item_id, payload, key, time.time())
        if message:
            board.counters["posts"] += 1
            board.notifier.notify(message["channel_id"])
            board.notifier.notify(ANY_CHANNEL)
        return JSONResponse(result, headers={"Idempotent-Replayed": "true"} if replayed else None)

    @app.post("/v1/queue/items/{item_id}/move")
    async def move_queue_item(item_id: int, request: Request):
        p = await authn(request)
        return await db.write(personal_queue.move_item, p, item_id,
                              body_dict(await _json_body(request)), time.time())

    @app.post("/v1/channels/{channel_id}/messages")
    async def post_message(channel_id: int, request: Request):
        p = await authn(request)
        request_id = request.headers.get("idempotency-key")
        if not request_id or not store.CLIENT_ID.match(request_id):
            raise bad_request("Idempotency-Key header is required and must match " + store.CLIENT_ID.pattern)
        payload = await _json_body(request, LIMITS.max_body_bytes * 2 + 64 * 1024)
        normalized, digest = store.normalize_payload(channel_id, payload)
        message, created = await db.write(
            store.post_message, p, channel_id, request_id, normalized, digest, time.time())
        if created:
            board.counters["posts"] += 1
            board.notifier.notify(channel_id)
            board.notifier.notify(ANY_CHANNEL)
            return JSONResponse(message, status_code=201)
        board.counters["posts_replayed"] += 1
        return JSONResponse(message, status_code=200, headers={"Idempotent-Replayed": "true"})

    @app.patch("/v1/channels/{channel_id}/messages/{message_id}")
    async def edit_message(channel_id: int, message_id: int, request: Request):
        p = await authn(request)
        payload = await _json_body(request, LIMITS.max_body_bytes * 2 + 1024)
        message, changed = await db.write(store.edit_message, p, channel_id, message_id, payload, time.time())
        if changed:
            board.counters["edits"] += 1
            board.notifier.notify(channel_id)
        return message

    @app.put("/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}")
    async def add_reaction(channel_id: int, message_id: int, emoji: str, request: Request):
        p = await authn(request)
        message, changed = await db.write(store.react, p, channel_id, message_id, unquote(emoji), True, time.time())
        if changed:
            board.notifier.notify(channel_id)
        return message

    @app.delete("/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}")
    async def remove_reaction(channel_id: int, message_id: int, emoji: str, request: Request):
        p = await authn(request)
        message, changed = await db.write(store.react, p, channel_id, message_id, unquote(emoji), False, time.time())
        if changed:
            board.notifier.notify(channel_id)
        return message

    @app.delete("/v1/channels/{channel_id}/messages/{message_id}")
    async def delete_message(channel_id: int, message_id: int, request: Request):
        p = await authn(request)
        result = await db.write(store.delete_message, p, channel_id, message_id, time.time())
        for key in result.pop("storage_keys"):
            try:
                board.blobs.path_for(key).unlink()
            except FileNotFoundError:
                pass
        board.notifier.notify(channel_id)
        return result

    @app.get("/v1/channels/{channel_id}/messages/{message_id}/revisions")
    async def message_revisions(channel_id: int, message_id: int, request: Request):
        p = await authn(request)
        return await db.read(store.message_revisions, p, channel_id, message_id)

    @app.get("/v1/channels/{channel_id}/messages")
    async def read_messages(
        channel_id: int,
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(LIMITS.default_page_size, ge=1, le=LIMITS.max_page_size),
        wait: float = Query(0, ge=0, le=LIMITS.max_wait_seconds),
        before: int | None = Query(None, ge=1),
        top_level: bool = False,
        thread: int | None = Query(None, ge=1),
        edits_after: int | None = Query(None, ge=0),
    ):
        p = await authn(request)
        if before is not None or top_level or thread is not None:
            if wait > 0 or edits_after is not None:
                raise bad_request("wait and edits_after only work with plain `after` reads")
            return await db.read(store.read_messages, p, channel_id, after, limit, before, top_level, thread)

        async def page(p):
            result = await db.read(store.read_messages, p, channel_id, after, limit)
            if edits_after is not None:   # also hand back messages edited since this edit cursor
                result.update(await db.read(store.read_edits, p, channel_id, edits_after, limit))
            return result

        if wait <= 0:
            return await page(p)
        if board.open_polls >= LIMITS.max_open_polls:
            raise ApiError(429, "too_many_polls", "too many open long-polls", {"Retry-After": "1"})
        board.open_polls += 1
        board.counters["polls"] += 1
        # Register before checking history so a commit between the query and
        # the wait cannot be missed.
        fut = board.notifier.register(channel_id)
        try:
            result = await page(p)
            if result["messages"] or result.get("edits") or board.shutting_down:
                return result
            await asyncio.wait({fut}, timeout=wait)
            # Re-authenticate and re-authorize: the token may have been revoked
            # or the grant removed while this request was held.
            p = await authn(request)
            return await page(p)
        finally:
            board.notifier.unregister(channel_id, fut)
            board.open_polls -= 1

    # ------------------------------------------------- profiles, invites

    @app.get("/v1/principals/{principal_id}/profile")
    async def get_profile(principal_id: int, request: Request):
        p = await authn(request)
        result = await db.read(store.profile, p, principal_id, time.time())
        result = (await with_presence(p, [result]))[0]
        result["agents"] = await with_presence(p, result["agents"])
        return result

    @app.put("/v1/principals/{principal_id}/avatar")
    async def put_avatar(principal_id: int, request: Request):
        p = await authn(request)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > LIMITS.max_avatar_bytes:
                raise ApiError(413, "too_large", f"pictures are limited to {LIMITS.max_avatar_bytes // 1024} KB")
        return await db.write(store.set_avatar, p, principal_id, bytes(data), time.time())

    @app.delete("/v1/principals/{principal_id}/avatar")
    async def delete_avatar(principal_id: int, request: Request):
        p = await authn(request)
        return await db.write(store.delete_avatar, p, principal_id, time.time())

    @app.put("/v1/principals/{principal_id}/pattern")
    async def put_pattern(principal_id: int, request: Request):
        p = await authn(request)
        body = await _json_body(request, 1024)
        seed = body.get("seed") if isinstance(body, dict) else None
        return await db.write(store.set_pattern, p, principal_id, seed, time.time())

    @app.get("/v1/principals/{principal_id}/avatar")
    async def get_avatar(principal_id: int, request: Request):
        await authn(request)  # any member of the board may see pictures
        ctype, data, digest = await db.read(store.get_avatar, principal_id)
        return Response(data, media_type=ctype, headers={
            "ETag": f'"{digest[:16]}"', "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})

    # ------------------------------------------------------ attachments

    @app.put("/v1/channels/{channel_id}/attachments/{upload_id}")
    async def upload(channel_id: int, upload_id: str, request: Request):
        p = await authn(request)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > LIMITS.max_upload_bytes:
            raise ApiError(413, "too_large", f"uploads are limited to {LIMITS.max_upload_bytes} bytes")
        if board.open_uploads >= LIMITS.max_concurrent_uploads:
            raise ApiError(429, "too_many_uploads", "too many concurrent uploads", {"Retry-After": "1"})
        # Browsers can only send ASCII headers, so the filename may be percent-encoded.
        filename = os.path.basename(unquote(request.headers.get("x-filename", "")))[:255] or None
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

    @app.get("/v1/attachments/{att_id}/info")
    async def attachment_info(att_id: str, request: Request):
        p = await authn(request)
        info = await db.read(store.get_attachment, p, att_id)
        info.pop("storage_key")
        return info

    @app.get("/v1/attachments/{att_id}")
    async def download(att_id: str, request: Request):
        p = await authn(request)
        return await attachment_file(p, att_id)

    async def attachment_file(p, att_id: str):
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

    # ------------------------------------------------------------------ storage folders

    def _cache_put(src: Path, sha: str) -> Path:
        dest = board.storage_cache / sha
        os.replace(src, dest)
        files = sorted(board.storage_cache.iterdir(), key=lambda f: f.stat().st_atime)
        total = sum(f.stat().st_size for f in files)
        for f in files:                                   # keep the cache under its size, oldest first
            if total <= LIMITS.storage_cache_bytes or f == dest:
                continue
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
        return dest

    @app.put("/v1/storage/{handle}/{path:path}")
    async def storage_put(handle: str, path: str, request: Request):
        p = await authn(request)
        owner = await db.read(store.storage_owner, p, handle, True)
        path = store.storage_path(path)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            await db.read(store.storage_check_quota, owner["id"], path, int(declared))
        content_type = (request.headers.get("content-type") or mimetypes.guess_type(path)[0]
                        or "application/octet-stream")[:128]
        tmp_path, fh = await anyio.to_thread.run_sync(board.blobs.open_temp)
        try:
            hasher, size = new_hasher(), 0
            try:
                max_file = (await db.read(store.storage_limits))[0]
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_file:
                        raise ApiError(413, "too_large", f"files are limited to {store._mb(max_file)} on this board")
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
            task = asyncio.create_task(publish_storage(p, owner, path, size, digest, content_type, tmp_path))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await task
                raise
            tmp_path = None
            return JSONResponse(result, status_code=201)
        finally:
            if tmp_path is not None:
                board.blobs.discard(tmp_path)

    async def publish_storage(p, owner, path, size, digest, content_type, tmp_path):
        async with board.storage_reservations.hold([(owner["id"], path)]):
            await db.read(store.storage_check_quota, owner["id"], path, size)
            key = store.storage_key(owner["id"], path)
            # Replacing a file needs a rollback copy in case the final quota
            # check loses to an unrelated upload. This I/O holds only its path.
            try:
                await db.read(store.storage_lookup, p, owner["external_id"], path)
                replacing = True
            except ApiError as exc:
                if exc.status != 404:
                    raise
                replacing = False
            with tempfile.TemporaryDirectory(prefix="msgboard-replace-") as scratch:
                backup = Path(scratch) / "old"
                backed_up = replacing and await anyio.to_thread.run_sync(board.storage.get, key, backup)
                changed = recorded = False
                try:
                    changed = True
                    await anyio.to_thread.run_sync(lambda: board.storage.put([(tmp_path, key)]))
                    await anyio.to_thread.run_sync(_cache_put, tmp_path, digest)
                    result = await db.write(store.storage_record_checked, p, owner["id"], path, size,
                                            digest, content_type, time.time())
                    recorded = True
                    return result
                finally:
                    if changed and not recorded:
                        if backed_up:
                            await anyio.to_thread.run_sync(lambda: board.storage.put([(backup, key)]))
                        else:
                            await anyio.to_thread.run_sync(board.storage.delete, [key])

    @app.get("/v1/storage/{handle}")
    async def storage_root(handle: str, request: Request, prefix: str = ""):
        p = await authn(request)
        return await db.read(store.storage_list, p, handle, prefix)

    @app.get("/v1/storage/{handle}/{path:path}")
    async def storage_get(handle: str, path: str, request: Request, info: bool = False):
        p = await authn(request)
        return await storage_file(p, handle, path, info)

    async def storage_file(p, handle: str, path: str, info: bool = False):
        if path.endswith("/"):                            # a folder: list it
            return await db.read(store.storage_list, p, handle, path)
        meta, key = await db.read(store.storage_lookup, p, handle, path)
        if info:
            return meta
        cached = board.storage_cache / meta["sha256"]
        if not cached.exists():
            owner = await db.read(store.storage_owner, p, handle, False)
            async with board.storage_reservations.hold([(owner["id"], store.storage_path(path))]):
                # A replacement may have completed while we waited. Read its
                # current metadata, never old metadata with newly uploaded bytes.
                meta, key = await db.read(store.storage_lookup, p, handle, path)
                cached = board.storage_cache / meta["sha256"]
                if not cached.exists():
                    fd, name = tempfile.mkstemp(prefix=".storage-", suffix=".part", dir=board.storage_cache)
                    os.close(fd)
                    tmp = Path(name)
                    try:
                        ok = await anyio.to_thread.run_sync(board.storage.get, key, tmp)
                        if not ok:
                            raise ApiError(503, "storage_unavailable", "the file is listed but could not be fetched from storage; try again")
                        # the cache is named by the recorded hash, so only bytes with that hash may go in it:
                        # a storage object overwritten behind the database (e.g. a mismatched restore) is never served
                        h = new_hasher()
                        with tmp.open("rb") as fh:
                            for chunk in iter(lambda: fh.read(1 << 20), b""):
                                h.update(chunk)
                        if h.hexdigest() != meta["sha256"]:
                            raise ApiError(409, "storage_mismatch", "the stored bytes do not match this file's recorded checksum")
                        os.replace(tmp, cached)
                    finally:
                        tmp.unlink(missing_ok=True)
        # always a download: never let stored HTML or SVG run on the board's own origin
        return FileResponse(cached, media_type="application/octet-stream", filename=os.path.basename(meta["path"]),
                            headers={"X-Checksum-Sha256": meta["sha256"], "X-Content-Type-Options": "nosniff",
                                     "X-Upload-Content-Type": meta["content_type"] or "application/octet-stream",
                                     "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-cache"})

    async def download_file(p, file_id: str):
        if file_id.startswith("storage:"):
            parts = file_id[8:].split("/", 1)
            if len(parts) != 2 or not parts[1] or parts[1].endswith("/"):
                raise bad_request("choose a file, not a storage folder")
            return await storage_file(p, parts[0], parts[1])
        return await attachment_file(p, file_id)

    @app.post("/v1/downloads")
    async def prepare_download(request: Request):
        p = await authn(request)
        body = body_dict(await _json_body(request, max_bytes=4096))
        file_id = body.get("file_id")
        if not isinstance(file_id, str) or not file_id or len(file_id) > 1024:
            raise bad_request("file_id must name an attachment or storage file")
        # Resolve permissions and fill/verify the storage cache before handing off. No file bytes in this JSON.
        response = await download_file(p, file_id)
        if not await anyio.to_thread.run_sync(Path(response.path).is_file):
            raise ApiError(404, "not_found", "this file is no longer available")
        key = download_links.issue(p.id, _bearer(request), file_id, response.headers["X-Checksum-Sha256"])
        return JSONResponse({"url": f"v1/downloads/{key}", "filename": response.filename,
                             "expires_in": DOWNLOAD_TTL_SECONDS}, headers={"Cache-Control": "no-store"})

    @app.api_route("/v1/downloads/{key}", methods=["GET", "HEAD"])
    async def browser_download(key: str):
        link = download_links.get(key)
        # A capability authorizes only its file/version. Recheck revocation, suspension, ownership and file ACLs.
        p = await db.read(store.authenticate, link.token, time.time())
        if p.id != link.principal_id:
            raise ApiError(404, "download_expired", "download link expired; click Download again")
        board.presence.seen(p, time.time())
        response = await download_file(p, link.file_id)
        if response.headers["X-Checksum-Sha256"] != link.sha256:
            raise ApiError(409, "download_changed", "this file changed; click Download again")
        response.headers.update({"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
        return response

    @app.delete("/v1/storage/{handle}/{path:path}")
    async def storage_delete(handle: str, path: str, request: Request):
        p = await authn(request)
        owner = await db.read(store.storage_owner, p, handle, True)
        path = store.storage_path(path)
        async with board.storage_reservations.hold([(owner["id"], path)]):
            out = await db.write(store.storage_remove, p, handle, path, time.time())
            keys = out.pop("keys")
            await anyio.to_thread.run_sync(lambda: board.storage.delete(keys))
        return out

    if dashboard:
        page = Path(__file__).with_name("dashboard.html")

        @app.get("/", include_in_schema=False)
        async def dashboard_page():
            return FileResponse(page, media_type="text/html",
                                headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    return app
