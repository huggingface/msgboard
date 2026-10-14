"""msgboard on a Hugging Face Space: local SQLite, snapshots to an HF bucket, dashboard at /.

Environment (Space secrets/variables):
  MSGBOARD_OPERATOR_TOKEN    secret; the operator (admin) board token
  HF_TOKEN                   secret; write access to the replica bucket
  MSGBOARD_REPLICA           hf://buckets/<ns>/<bucket>[/<prefix>]
  MSGBOARD_SNAPSHOT_INTERVAL seconds between snapshots (default 15)
  MSGBOARD_DEMO_BOTS         "1" to run a few slow demo agents

Restarts overlap on Spaces: the new container starts before the old one gets
SIGTERM. So this app opens its port at once but answers 503 (Retry-After) until
the old container has pushed its final snapshot and released the lease. Only
then does it restore and serve. Clients retry with the same request IDs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import uvicorn

from msgboard import auth, db, store
from msgboard.replicate import Replicator, restore, store_from_url, wait_for_release
from msgboard.server import Board, create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
log = logging.getLogger("msgboard.space")

DATA_DIR = Path(os.environ.get("MSGBOARD_DATA_DIR", "/home/user/board-data"))
PAGE = Path(__file__).resolve().parent / "dashboard.html"


def ensure_board(data_dir: Path, operator_token: str) -> None:
    """Create the schema if needed and make the operator credential equal the Space secret."""
    parsed = auth.parse_token(operator_token)
    if parsed is None:
        raise SystemExit("MSGBOARD_OPERATOR_TOKEN is not a valid board token")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = db.connect(data_dir / "board.db")
    try:
        db.migrate(conn, data_dir / "board.db")
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        if not store.admin_exists(conn):
            store.create_operator(conn, now)
        pid = conn.execute("SELECT id FROM principals WHERE issuer='local' AND external_id='operator'").fetchone()[0]
        cred_id, secret = parsed
        current = conn.execute("SELECT id FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
                               (pid,)).fetchone()
        if current is None or current[0] != cred_id:
            conn.execute("UPDATE credentials SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL", (now, pid))
            conn.execute("DELETE FROM credentials WHERE id=?", (cred_id,))
            conn.execute("INSERT INTO credentials (id, principal_id, secret_hash, created_at) VALUES (?, ?, ?, ?)",
                         (cred_id, pid, hashlib.sha256(secret).digest(), now))
        conn.execute("COMMIT")
    finally:
        conn.close()


class Dispatcher:
    """ASGI app that answers 'starting' until the board is ready, then forwards to it."""

    def __init__(self):
        self.inner = None
        self.board: Board | None = None
        self.error: str | None = None
        self.stop = threading.Event()
        self._inner_lifespan = None

    def _prepare(self) -> Board:
        operator_token = os.environ["MSGBOARD_OPERATOR_TOKEN"].strip()
        replica_url = os.environ.get("MSGBOARD_REPLICA")
        replicator = None
        if replica_url:
            replica = store_from_url(replica_url)
            why = wait_for_release(replica, stop=self.stop,
                                   stale_after=float(os.environ.get("MSGBOARD_LEASE_STALE_AFTER", "120")))
            log.info("replica handover: %s", why)
            if why == "stopped":
                raise RuntimeError("stopped before the replica was handed over")
            if not (DATA_DIR / "board.db").exists():
                manifest = restore(replica, DATA_DIR)
                log.info("restored %s" % manifest["snapshot"] if manifest else "replica empty; starting a new board")
            replicator = Replicator(replica, DATA_DIR,
                                    interval=float(os.environ.get("MSGBOARD_SNAPSHOT_INTERVAL", "15")))
        else:
            log.warning("MSGBOARD_REPLICA not set: all data is lost when this Space restarts")
        ensure_board(DATA_DIR, operator_token)
        return Board(DATA_DIR, replicator=replicator)

    async def _start(self) -> None:
        try:
            board = await asyncio.to_thread(self._prepare)
            app = create_app(board)
            from accounts import import_shared_invite, install as install_accounts

            install_accounts(app, board)
            self._inner_lifespan = app.router.lifespan_context(app)
            await self._inner_lifespan.__aenter__()
            self.board, self.inner = board, app
            shared = os.environ.get("MSGBOARD_INVITE_CODE", "").strip()
            if shared:
                await import_shared_invite(board, shared, os.environ["MSGBOARD_OPERATOR_TOKEN"].strip())
            log.info("board ready")
            if os.environ.get("MSGBOARD_DEMO_BOTS") == "1":
                from demo_bots import start_bots

                start_bots(f"http://127.0.0.1:{os.environ.get('PORT', '7860')}", os.environ["MSGBOARD_OPERATOR_TOKEN"].strip())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("board failed to start")

    def begin_shutdown(self) -> None:
        self.stop.set()
        if self.board is not None:
            self.board.begin_shutdown()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self._task = asyncio.create_task(self._start())
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if self._inner_lifespan is not None:
                        await self._inner_lifespan.__aexit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if self.inner is not None:
            return await self.inner(scope, receive, send)
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if path == "/":
            body, status, ctype, extra = PAGE.read_bytes(), 200, b"text/html", []
        elif path == "/v1/health":
            body, status, ctype, extra = json.dumps({"status": "starting", "error": self.error}).encode(), 200, \
                b"application/json", []
        else:
            body = json.dumps({"error": {"code": "starting",
                                         "message": self.error or "board is starting; retry shortly"}}).encode()
            status, ctype, extra = 503, b"application/json", [(b"retry-after", b"2")]
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype), (b"cache-control", b"no-cache")] + extra})
        await send({"type": "http.response.body", "body": body})


def main() -> None:
    dispatcher = Dispatcher()

    class SpaceServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            dispatcher.begin_shutdown()
            super().handle_exit(sig, frame)

    def _exit(signum, frame):  # see msgboard.cli.run_board: keep cleanup from being skipped
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)
    config = uvicorn.Config(dispatcher, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")),
                            log_level="warning", timeout_graceful_shutdown=5, server_header=False)
    try:
        SpaceServer(config).run()
    finally:
        if dispatcher.board is not None:
            dispatcher.board.close()  # final snapshot, then release the lease
        log.info("stopped")


if __name__ == "__main__":
    main()
