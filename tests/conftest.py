from __future__ import annotations

import json
import socket
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from msgboard.cli import cmd_init
from msgboard.client import Client
from msgboard.config import OPERATOR_TOKEN_FILE
from msgboard.local import ThreadedServer
from msgboard.replicate import DirStore


class PartlyApplied(DirStore):
    """A bucket upload is not transactional: some of its files can land and the others not. The first upload for
    which `when(items)` holds applies only the files whose names start with one of `applies`, then fails, and
    `then()` runs (say, stopping the instance before it tries again)."""

    def __init__(self, root, applies: tuple[str, ...], when, then=lambda: None):
        super().__init__(root)
        self.applies, self.when, self.then, self.failed = applies, when, then, False

    def put(self, items):
        if self.failed or not self.when(items):
            return super().put(items)
        self.failed = True
        super().put([item for item in items if item[1].startswith(self.applies)])
        self.then()
        raise ConnectionError("the bucket applied part of the upload")


def releases(items) -> bool:
    """An upload that carries a released lease."""
    return any(dest == "LEASE.json" and json.loads(source)["released"] for source, dest in items)


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

    def _track(self, client: Client) -> Client:
        self._clients.append(client)
        return client

    def human(self, name: str, role: str = "member", display_name: str | None = None):
        """A person who joined with an admin's invite."""
        invite = self.op.create_invite("human", role=role)
        c = self._track(Client.join(self.url, invite["code"], name, display_name=display_name))
        return c.identity, c.token, c

    def agent(self, name: str, owner: Client | None = None, role: str = "member", identity_type: str = "agent",
              can_create_channels: bool = False, channels=(), display_name: str | None = None):
        """An agent that joined with an invite from `owner` (default: the operator), added to `channels`."""
        owner = owner or self.op
        invite = owner.create_invite("agent", role=role, identity_type=identity_type,
                                     can_create_channels=can_create_channels)
        c = self._track(Client.join(self.url, invite["code"], name, display_name=display_name))
        for channel in channels:
            owner.add_member(owner.channel_id(channel), c.identity["id"])
        return c.identity, c.token, c

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
