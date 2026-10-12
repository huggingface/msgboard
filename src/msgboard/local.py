"""Run a board server on a background thread (used by the demo and the tests)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn

from .server import Board, create_app


class ThreadedServer:
    def __init__(self, data_dir: str | Path, host: str = "127.0.0.1", port: int = 0):
        self.board = Board(Path(data_dir))
        config = uvicorn.Config(create_app(self.board), host=host, port=port,
                                log_level="warning", timeout_graceful_shutdown=5)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="msgboard-server", daemon=True)
        self.host = host
        self.port = port

    def start(self) -> "ThreadedServer":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.time() > deadline:
                self.board.close()
                raise RuntimeError("server failed to start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.board.begin_shutdown()
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.board.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
