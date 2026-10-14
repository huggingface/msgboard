from __future__ import annotations

import json
import time

import httpx
import pytest

from msgboard.client import Client
from msgboard.local import ThreadedServer
from msgboard.replicate import DirStore, Replicator, restore

from .conftest import init_board


def wait_for(cond, timeout=10):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "timed out"
        time.sleep(0.05)


def test_snapshot_round_trip_with_attachments(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    with ThreadedServer(data, replicator=Replicator(replica, data, interval=3600)) as s, Client(s.url, token) as c:
        ch = c.create_channel("keep")
        att = c.upload(ch["id"], b"file bytes", filename="notes.txt")
        sent = [c.post(ch["id"], f"m{i}", request_id=f"r{i}") for i in range(20)]
        c.post(ch["id"], "with file", attachments=[att["id"]])
    # Graceful stop pushed a final snapshot.
    latest = json.loads((tmp_path / "bucket" / "LATEST.json").read_text())
    assert latest["snapshot"].startswith("snapshots/")

    fresh = tmp_path / "b"
    assert restore(replica, fresh)["sha256"] == latest["sha256"]
    with ThreadedServer(fresh) as s, Client(s.url, token) as c:
        history = c.read(ch["id"])["messages"]
        assert [m["body"] for m in history[:20]] == [m["body"] for m in sent]
        assert c.download(att["id"]) == b"file bytes"
        assert c.post(ch["id"], "m0", request_id="r0")["id"] == sent[0]["id"]  # request IDs restored too


def test_periodic_push_only_when_changed(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=0.1)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token) as c:
        wait_for(lambda: rep.status["pushes"] >= 1)  # initial push
        idle = rep.status["pushes"]
        time.sleep(0.5)
        assert rep.status["pushes"] == idle      # nothing changed, nothing uploaded
        c.create_channel("x")
        wait_for(lambda: rep.status["pushes"] > idle)
        with httpx.Client(base_url=s.url) as h:
            m = h.get("/v1/metrics", headers={"X-Msgboard-Token": token}).json()
        assert m["replication"]["last_snapshot"]


def test_old_snapshots_are_pruned(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=0.05, keep=3)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token) as c:
        ch = c.create_channel("x")
        for i in range(8):
            c.post(ch["id"], str(i))
            n = rep.status["pushes"]
            wait_for(lambda: rep.status["pushes"] > n)
            time.sleep(1.05)  # snapshot names have one-second resolution
    assert len([p for p in replica.list("snapshots/") if p.endswith(".db")]) == 3


def test_newer_instance_fences_older_one(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    old = Replicator(replica, data, interval=3600)
    old.take_lease()
    assert old.push_if_changed()
    new = Replicator(replica, tmp_path / "other", interval=3600)
    new.take_lease()                     # an overlapping replacement starts
    old.generation_fn = lambda: 99       # the old one has more writes...
    assert not old.push_if_changed()     # ...but may no longer move LATEST
    assert old.status["fenced"]


def test_restore_safety(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    assert restore(replica, tmp_path / "empty") is None   # nothing to restore
    rep = Replicator(replica, data, interval=3600)
    rep.take_lease()
    rep.push_if_changed()
    with pytest.raises(FileExistsError):
        restore(replica, data)                            # never overwrites a live database
    latest = json.loads((tmp_path / "bucket" / "LATEST.json").read_text())
    (tmp_path / "bucket" / latest["snapshot"]).write_bytes(b"corrupt")
    with pytest.raises(RuntimeError):
        restore(replica, tmp_path / "c")
    assert not (tmp_path / "c" / "board.db").exists()


def test_header_token_and_dashboard(env):
    with httpx.Client(base_url=env.url) as h:
        assert h.get("/v1/me", headers={"X-Msgboard-Token": env.operator_token,
                                        "Authorization": "Bearer some-proxy-token"}).status_code == 200
        page = h.get("/")
        assert page.status_code == 200 and "msgboard" in page.text
    c = env.client(env.operator_token)
    via_header = Client(env.url, env.operator_token, token_header="X-Msgboard-Token",
                        headers={"Authorization": "Bearer proxy"})
    assert via_header.me()["id"] == c.me()["id"]
    via_header.close()


def test_attachment_info_and_encoded_filename(env):
    ch = env.op.create_channel("f")
    with env.http(env.operator_token) as h:
        att = h.put(f"/channels/{ch['id']}/attachments/u1", content=b"abc",
                    headers={"X-Filename": "r%C3%A9sum%C3%A9%20v2.txt"}).json()
        info = h.get(f"/attachments/{att['id']}/info").json()
    assert info["filename"] == "résumé v2.txt" and info["size"] == 3 and "storage_key" not in info


def test_sigterm_pushes_final_snapshot(tmp_path):
    import signal
    import subprocess
    import sys

    from .conftest import free_port

    data, bucket = tmp_path / "a", tmp_path / "bucket"
    token = init_board(data)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "msgboard", "serve", "--data-dir", str(data), "--port", str(port),
         "--replica", str(bucket), "--snapshot-interval", "3600", "--log-level", "warning"],
        stderr=subprocess.PIPE, text=True)
    try:
        url = f"http://127.0.0.1:{port}"
        wait_for(lambda: _up(url), timeout=20)
        with Client(url, token) as c:
            ch = c.create_channel("last")
            c.post(ch["id"], "posted right before shutdown")
    finally:
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "Board stopped cleanly" in err
    fresh = tmp_path / "b"
    restore(DirStore(bucket), fresh)
    with ThreadedServer(fresh) as s, Client(s.url, token) as c:
        assert [m["body"] for m in c.read(ch["id"])["messages"]] == ["posted right before shutdown"]


def _up(url):
    try:
        return httpx.get(url + "/v1/health", timeout=1).status_code == 200
    except httpx.TransportError:
        return False


def test_handover_waits_for_release_then_restores_everything(tmp_path):
    import threading

    from msgboard.replicate import wait_for_release

    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600)
    server = ThreadedServer(data, replicator=rep).start()
    c = Client(server.url, token)
    ch = c.create_channel("x")
    c.post(ch["id"], "before the new instance starts")

    result = {}
    waiter = threading.Thread(target=lambda: result.setdefault("why", wait_for_release(replica, poll=0.05)))
    waiter.start()                          # the replacement starts while the old one still serves
    time.sleep(0.3)
    assert waiter.is_alive()                # ...and waits
    c.post(ch["id"], "accepted by the old instance during the overlap")
    c.close()
    server.stop()                           # SIGTERM: final snapshot, then release
    waiter.join(timeout=10)
    assert result["why"] == "released"
    restore(replica, tmp_path / "b")
    with ThreadedServer(tmp_path / "b") as s, Client(s.url, token) as c2:
        assert [m["body"] for m in c2.read(ch["id"])["messages"]] == [
            "before the new instance starts", "accepted by the old instance during the overlap"]


def test_handover_gives_up_on_a_dead_owner(tmp_path):
    from msgboard.replicate import wait_for_release

    replica = DirStore(tmp_path / "bucket")
    assert wait_for_release(replica) == "free"
    replica.put([(json.dumps({"instance": "gone", "heartbeat": time.time() - 500}).encode(), "LEASE.json")])
    assert wait_for_release(replica, stale_after=120) == "stale"


def test_writes_rejected_once_shutdown_begins(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    with ThreadedServer(data, replicator=Replicator(replica, data, interval=3600)) as s, Client(s.url, token,
                                                                                               max_retries=0) as c:
        ch = c.create_channel("x")
        s.board.begin_shutdown()
        with pytest.raises(Exception) as exc:
            c.post(ch["id"], "too late")
        assert getattr(exc.value, "status", None) == 503
        assert c.read(ch["id"])["messages"] == []   # reads still work while draining
