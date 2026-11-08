"""msgboard on a Hugging Face Space: local SQLite, snapshots to an HF bucket, dashboard at /.

Environment (Space secrets/variables):
  MSGBOARD_OPERATOR_TOKEN    secret; the operator (admin) board token
  HF_TOKEN                   secret; write access to the replica bucket
  MSGBOARD_REPLICA           hf://buckets/<ns>/<bucket>[/<prefix>]
  MSGBOARD_SNAPSHOT_INTERVAL seconds between snapshots (default 15)
  MSGBOARD_LEASE_STALE_AFTER seconds in which nothing from the owner lands before it counts as dead (default 300)
  MSGBOARD_INVITE_CODE       optional secret; kept working as a standing member invite link
  MSGBOARD_DEMO_BOTS         "1" to run the demo's bots (hfspace/demo_bots.py, uploaded by hfspace/deploy.py)

The Space runs `python -m msgboard.space.app`; msgboard.space.deploy() uploads this package to it.

Restarts overlap on Spaces: the new container starts while the old one serves, and
the old one gets SIGTERM once the new one's port answers, then SIGKILL about 10 s
later. A final snapshot in those 10 s fails when the bucket is slow (rate-limited
uploads take minutes). So the new container asks the old one for the replica before
it opens its port (Replicator.take_over): the old one stops taking writes (503,
Retry-After), pushes its final snapshot and releases the lease, however long that
takes while its uploads or heartbeats keep landing. The new one claims the replica,
then opens its port, restores and serves. Clients retry with the same request IDs.
A container that a newer one replaced meanwhile never opens its port. An old
container from before hand-over requests hands over on SIGTERM instead, so then the
port opens at once and answers 503 meanwhile.
"""

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
STALE_AFTER = float(os.environ.get("MSGBOARD_LEASE_STALE_AFTER", "300"))
PAGE = Path(__file__).resolve().parents[1] / "dashboard.html"


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
        self.replica = self.replicator = self.handover = None

    def take_over(self) -> None:
        """Before the port opens: get the replica from the container this one replaces (module docstring)."""
        replica_url = os.environ.get("MSGBOARD_REPLICA")
        if not replica_url:
            return
        self.replica = store_from_url(replica_url)
        self.replicator = Replicator(self.replica, DATA_DIR, stop=self.stop,
                                     interval=float(os.environ.get("MSGBOARD_SNAPSHOT_INTERVAL", "15")))
        self.handover = self.replicator.take_over(stale_after=STALE_AFTER)
        log.info("replica handover: %s", self.handover)

    def _prepare(self) -> Board:
        operator_token = os.environ["MSGBOARD_OPERATOR_TOKEN"].strip()
        if self.replicator is not None:
            if self.handover == "legacy":  # it hands over on SIGTERM, which comes now that the port answers
                why = wait_for_release(self.replica, stop=self.stop, stale_after=STALE_AFTER)
                log.info("replica handover: %s", why)
                if why == "stopped":
                    raise RuntimeError("stopped before the replica was handed over")
            while not (DATA_DIR / "board.db").exists():
                try:
                    manifest = restore(self.replica, DATA_DIR)
                except RuntimeError:  # a snapshot that is missing or corrupt: retrying will not help
                    raise
                except Exception as exc:  # a failed Hub call
                    log.warning("restore failed, retrying: %s", exc)
                    if self.stop.wait(5):
                        raise
                    continue
                log.info("restored %s" % manifest["snapshot"] if manifest else "replica empty; starting a new board")
                break
        else:
            log.warning("MSGBOARD_REPLICA not set: all data is lost when this Space restarts")
        ensure_board(DATA_DIR, operator_token)
        return Board(DATA_DIR, replicator=self.replicator)

    async def _start(self) -> None:
        try:
            board = await asyncio.to_thread(self._prepare)
            app = create_app(board)
            from msgboard.space.accounts import adopt_hf_pictures, import_shared_invite, install as install_accounts

            install_accounts(app, board)
            self._inner_lifespan = app.router.lifespan_context(app)
            await self._inner_lifespan.__aenter__()
            self.board, self.inner = board, app
            shared = os.environ.get("MSGBOARD_INVITE_CODE", "").strip()
            if shared:
                await import_shared_invite(board, shared, os.environ["MSGBOARD_OPERATOR_TOKEN"].strip())
            log.info("board ready")
            # linked accounts without a picture get their Hugging Face one (in the background, best effort)
            self._pictures = asyncio.get_running_loop().create_task(adopt_hf_pictures(board))
            if os.environ.get("MSGBOARD_DEMO_BOTS") == "1":
                try:  # not part of the package: only the demo Space has it, next to the package
                    from demo_bots import start_bots
                except ImportError:
                    log.warning("MSGBOARD_DEMO_BOTS=1 but demo_bots.py is not in the Space; no bots")
                else:
                    start_bots(f"http://127.0.0.1:{os.environ.get('PORT', '7860')}",
                               os.environ["MSGBOARD_OPERATOR_TOKEN"].strip())
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
    dispatcher.take_over()  # with the port still closed, so the platform keeps the old container serving
    if dispatcher.handover == "superseded":
        log.warning("a newer container has the replica; this one does not serve, and waits to be stopped")
        threading.Event().wait()  # until SIGTERM
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
