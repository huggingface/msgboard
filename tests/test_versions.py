"""Upgrades and rollbacks between the released version and this one, each version run through its real entry point.

The released version (RELEASED, from this repo's git history) names snapshots by UTC time and has no epochs,
claims or hand-over requests: its new container opens its port at once, waits for the old one to release the
lease after SIGTERM, and restores the snapshot LATEST.json names. The last two tests run the released version's
replication code in this process, on boards of this version, to control what lands in the bucket and when.
"""

import importlib.util
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
from msgboard.local import ThreadedServer
from msgboard.replicate import DirStore, Replicator, restore, wait_for_release

from .conftest import free_port, init_board

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


@pytest.fixture(scope="module")
def released_replication(released):
    """The released version's msgboard.replicate, which imports nothing else from msgboard."""
    spec = importlib.util.spec_from_file_location("released_replicate", released / "msgboard" / "replicate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _bodies(server: ThreadedServer, token: str, channel: int) -> list[str]:
    with Client(server.url, token) as c:
        return [m["body"] for m in c.read(channel)["messages"]]


def test_a_late_lease_from_the_released_version_does_not_take_the_replica_from_its_successor(
        tmp_path, released_replication):
    # Review #3 (Codex) #4: a container of this version took the replica from the released version's instance and
    # served. That instance's release landed again later (an upload of it that was slow), and the next container
    # took the replica at once instead of asking the serving one: it restored the first snapshot of the one
    # serving, without the post that one had acknowledged since, and fenced it.
    bucket, token = tmp_path / "bucket", init_board(tmp_path / "a")
    old = released_replication.Replicator(released_replication.DirStore(bucket), tmp_path / "a", interval=3600)
    with ThreadedServer(tmp_path / "a", replicator=old) as s, Client(s.url, token) as c:
        channel = c.create_channel("restarts")["id"]
        c.post(channel, "acknowledged by the released version")
    late = (bucket / "LEASE.json").read_bytes()   # its release
    upgraded = Replicator(DirStore(bucket), tmp_path / "b", interval=3600, poll=0.05)
    assert upgraded.take_over(poll=0.05) == "released"
    restore(DirStore(bucket), tmp_path / "b")
    with ThreadedServer(tmp_path / "b", replicator=upgraded) as s, Client(s.url, token) as c:
        c.post(channel, "acknowledged after the upgrade")
        (bucket / "LEASE.json").write_bytes(late)
        following = Replicator(DirStore(bucket), tmp_path / "c", interval=3600, poll=0.05)
        assert following.take_over(poll=0.05) == "released" and upgraded.handed_over and not upgraded.fenced
    assert restore(DirStore(bucket), tmp_path / "c")["snapshot"] == upgraded.status["last_snapshot"]
    with ThreadedServer(tmp_path / "c", replicator=following) as s:
        assert _bodies(s, token, channel) == ["acknowledged by the released version", "acknowledged after the upgrade"]


class CutShort(DirStore):
    """A bucket that a released instance's LATEST.json never reaches: it was killed after uploading its snapshot."""

    def put(self, items):
        if any(dest == "LATEST.json" for _, dest in items):
            raise ConnectionError("killed before LATEST.json")
        super().put(items)


@pytest.mark.parametrize("publication", ["complete", "cut short before LATEST.json"])
def test_an_upgrade_after_a_rollback_restores_the_rolled_back_board_whatever_the_snapshot_times(
        tmp_path, released_replication, publication):
    # Review #3 (Codex) #5: snapshots of different released instances were ordered by their UTC second, then by a
    # generation that counts only each instance's writes. Released A (generation 10) -> this B -> released C
    # (generation 2) -> this D, A's and C's snapshots in the same second: D restored A's, without B's and C's
    # posts. With C's LATEST.json cut short, C's snapshot was compared with the time of B's, and in the same
    # second D restored B's, without C's post. Each run whose snapshots are not all in one second runs again.
    for attempt in range(5):
        case = tmp_path / str(attempt)
        bucket, token = case / "bucket", init_board(case / "a")
        old = released_replication.Replicator(released_replication.DirStore(bucket), case / "a", interval=3600)
        with ThreadedServer(case / "a", replicator=old) as s, Client(s.url, token) as c:
            channel = c.create_channel("restarts")["id"]
            posted = [f"acknowledged by released A, {n}" for n in range(8)]
            for body in posted:
                c.post(channel, body)
            second = int(time.time()) + 1
            while time.time() < second:   # A's final snapshot at the start of a second
                time.sleep(0.001)
        upgraded = Replicator(DirStore(bucket), case / "b", interval=3600, poll=0.05)
        assert upgraded.take_over(poll=0.05) == "released"
        restore(DirStore(bucket), case / "b")
        with ThreadedServer(case / "b", replicator=upgraded) as s, Client(s.url, token) as c:
            c.post(channel, "acknowledged by B")
        assert released_replication.wait_for_release(released_replication.DirStore(bucket)) == "released"
        released_replication.restore(released_replication.DirStore(bucket), case / "c")
        rolled_back = released_replication.Replicator(
            released_replication.DirStore(bucket) if publication == "complete" else CutShort(bucket), case / "c",
            interval=3600)
        with ThreadedServer(case / "c", replicator=rolled_back) as s, Client(s.url, token) as c:
            c.post(channel, "acknowledged by released C")
        one_second = int(time.time()) == second
        posted += ["acknowledged by B", "acknowledged by released C"]
        upgraded = Replicator(DirStore(bucket), case / "d", interval=3600, poll=0.05)
        if upgraded.take_over(poll=0.05) == "legacy":   # C never released the lease: as the Space app does
            assert wait_for_release(DirStore(bucket), stale_after=0.2, poll=0.05) == "stale" and upgraded.claim()
        restore(DirStore(bucket), case / "d")
        with ThreadedServer(case / "d", replicator=upgraded) as s:
            assert _bodies(s, token, channel) == posted
        if one_second:
            return
    pytest.fail("no run of the schedule fitted in one second")
