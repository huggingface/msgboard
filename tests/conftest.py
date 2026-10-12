from __future__ import annotations

import socket
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from msgboard.cli import cmd_init
from msgboard.client import Client
from msgboard.config import OPERATOR_TOKEN_FILE
from msgboard.local import ThreadedServer


def init_board(data_dir: Path) -> str:
    assert cmd_init(Namespace(data_dir=str(data_dir))) == 0
    return (data_dir / OPERATOR_TOKEN_FILE).read_text().strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    """A running board plus helpers to create identities."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.operator_token = init_board(data_dir)
        self.server = ThreadedServer(data_dir).start()
        self.url = self.server.url
        self.op = Client(self.url, self.operator_token)
        self._clients = [self.op]

    def client(self, token: str, **kw) -> Client:
        c = Client(self.url, token, **kw)
        self._clients.append(c)
        return c

    def http(self, token: str | None) -> httpx.Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.url + "/v1", headers=headers, timeout=20)

    def agent(self, external_id: str, role: str = "worker", channels=(), via: Client | None = None,
              ttl: int = 3600, **kw) -> tuple[dict, str, Client]:
        issuer = via or self.op
        identity = issuer.provision_agent(external_id, role=role, channels=list(channels), **kw)
        token = issuer.issue_token(identity["id"], ttl_seconds=ttl)
        return identity, token["token"], self.client(token["token"])

    def provisioner(self, external_id: str = "launcher", policy: dict | None = None):
        return self.agent(external_id, role="provisioner", identity_type="system", policy=policy)

    def restart(self) -> None:
        for c in self._clients:
            c.close()
        self.server.stop()
        self.server = ThreadedServer(self.data_dir).start()
        self.url = self.server.url
        self.op = Client(self.url, self.operator_token)
        self._clients = [self.op]

    def close(self) -> None:
        for c in self._clients:
            c.close()
        self.server.stop()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "board")
    yield e
    e.close()
