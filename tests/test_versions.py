"""Upgrades and rollbacks between the released version and this one, each version run through its real entry point.

The released version (RELEASED, from this repo's git history) names snapshots by UTC time and has no epochs,
claims or hand-over requests: its new container opens its port at once, waits for the old one to release the
lease after SIGTERM, and restores the snapshot LATEST.json names.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import msgboard
from msgboard.auth import new_token
from msgboard.client import Client
from msgboard.replicate import DirStore, Replicator

from .conftest import free_port

RELEASED = "563911d"  # main when this version was written; the live demo runs the same replication code
CURRENT = Path(msgboard.__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def released(tmp_path_factory):
    """The released version's package, extracted from git history."""
    out = tmp_path_factory.mktemp("released")
    repo = Path(__file__).resolve().parents[1]
    try:
        archive = subprocess.run(["git", "-C", str(repo), "archive", RELEASED, "src/msgboard"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(out)], input=archive, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"needs this repo's git history with commit {RELEASED}: {exc}")
    return out / "src"


class SpaceApp:
    """`python -m msgboard.space.app` from one version's source tree, as a Space container runs it."""

    def __init__(self, src: Path, root: Path, name: str, token: str):
        self.name, self.log = name, root / f"{name}.log"
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        with self.log.open("w") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "msgboard.space.app"], stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONPATH": str(src), "PORT": str(port), "MSGBOARD_OPERATOR_TOKEN": token,
                     "MSGBOARD_REPLICA": str(root / "bucket"), "MSGBOARD_DATA_DIR": str(root / name),
                     "MSGBOARD_SNAPSHOT_INTERVAL": "3600", "MSGBOARD_LEASE_STALE_AFTER": "10"})
        self.client = Client(self.url, token, max_retries=0)

    def answers(self) -> bool:
        assert self.proc.poll() is None, f"{self.name} exited:\n{self.log.read_text()[-3000:]}"
        try:
            httpx.get(self.url + "/v1/health", timeout=1)
            return True
        except httpx.TransportError:
            return False

    def ready(self, timeout: float = 60) -> "SpaceApp":
        deadline = time.monotonic() + timeout
        while True:
            assert self.answers() or time.monotonic() < deadline, f"{self.name}'s port stayed closed"
            try:
                self.client.me()
                return self
            except Exception:
                assert time.monotonic() < deadline, f"{self.name} never served:\n{self.log.read_text()[-3000:]}"
                time.sleep(0.1)

    def stop(self, sig=signal.SIGTERM) -> None:
        self.client.close()
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        code = self.proc.wait(timeout=60)
        assert code == 0 or sig != signal.SIGTERM, self.log.read_text()[-3000:]


def _replace(old: SpaceApp, start, overlapping: bool) -> SpaceApp:
    """A Space restart. Overlapping, as the platform does it: the new container starts while the old one serves,
    and the old one gets SIGTERM once the new one's port answers."""
    if not overlapping:
        old.stop()
        return start().ready()
    new = start()
    deadline = time.monotonic() + 30
    while not new.answers():
        assert time.monotonic() < deadline, f"{new.name}'s port stayed closed while {old.name} served"
        time.sleep(0.1)
    old.stop()
    return new.ready()


@pytest.mark.parametrize("schedule", ["one after the other", "overlapping", "a replacement stops before serving",
                                      "a replacement dies after it starts serving"])
def test_a_board_rolled_back_to_the_released_version_and_upgraded_again_keeps_every_post(tmp_path, released,
                                                                                          schedule):
    # Review #2 (Codex) #1: after a rollback, the released version's snapshots are named by UTC time, which ranked
    # below every epoch snapshot, and its lease was ignored in favour of the old claims. On the upgrade after the
    # rollback, this version restored the board it had left before the rollback, without what the released
    # version had acknowledged, and its next snapshot made that the board for good.
    token, overlapping = new_token()[1], schedule != "one after the other"
    versions = {"released": released, "this": CURRENT}

    def start(version, n):
        return lambda: SpaceApp(versions[version], tmp_path, f"{n}-{version}", token)

    app = start("released", 0)().ready()
    channel = app.client.create_channel("restarts")["id"]
    posted = []
    try:
        for n, version in enumerate(["released", "this", "released", "this"]):
            if n:
                if n == 3 and schedule == "a replacement stops before serving":
                    # a container of this version claims the replica after the rollback, and is stopped before
                    # it serves (a double restart): the next one must still restore the released version's board
                    app.stop()
                    stopped = Replicator(DirStore(tmp_path / "bucket"), tmp_path / "stopped", interval=3600,
                                         poll=0.05, stop=threading.Event())
                    assert stopped.take_over(stale_after=10, poll=0.05) == "released"
                    stopped.stop()
                    app = start(version, n)().ready()
                elif n == 3 and schedule == "a replacement dies after it starts serving":
                    # one serves, and is killed before its first periodic snapshot: what it acknowledged is
                    # lost, as in any crash, but not what the released version had
                    app.stop()
                    start(version, "3-killed")().ready().stop(signal.SIGKILL)
                    app = start(version, n)().ready()
                else:
                    app = _replace(app, start(version, n), overlapping)
            bodies = [m["body"] for m in app.client.read(channel)["messages"]]
            assert bodies == posted, f"stage {n} ({version}) restored {bodies}, expected {posted}"
            body = f"acknowledged by stage {n} ({version})"
            app.client.post(channel, body)
            posted.append(body)
    finally:
        app.stop()
