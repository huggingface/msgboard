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


# ------------------------------------------------- restarts: huggingface/msgboard#26


class CountingStore(DirStore):
    def __init__(self, root, fail_puts: int = 0):
        super().__init__(root)
        self.puts: list[list[str]] = []
        self.fail_puts = fail_puts

    def put(self, items):
        if self.fail_puts:
            self.fail_puts -= 1
            raise ConnectionError("Rate limited: 429 Too Many Requests")
        self.puts.append([dest for _, dest in items])
        super().put(items)


def _json(path):
    return json.loads(path.read_text())


def _request(replica, instance, since):
    replica.put([(json.dumps({"instance": instance, "since": since}).encode(), "HANDOVER.json")])


def test_restore_takes_the_newest_snapshot_when_latest_lags(tmp_path):
    # An instance killed between a snapshot and its pointer left LATEST behind; restoring LATEST lost
    # the newer snapshot's writes (the rollback in #26's B2).
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token) as c:
        ch = c.create_channel("x")
        c.post(ch["id"], "in both snapshots")
        assert rep.push_if_changed()
        lagging = (tmp_path / "bucket" / "LATEST.json").read_bytes()
        c.post(ch["id"], "only in the newer one")
        assert rep.push_if_changed()
    (tmp_path / "bucket" / "LATEST.json").write_bytes(lagging)
    restore(replica, tmp_path / "b")
    with ThreadedServer(tmp_path / "b") as s, Client(s.url, token) as c:
        assert [m["body"] for m in c.read(ch["id"])["messages"]] == ["in both snapshots", "only in the newer one"]


def test_a_push_is_one_upload(tmp_path):
    # Bucket uploads are rate-limited, and a rate-limited one takes minutes: no extra upload for the
    # pointer, the lease or an event.
    data, replica = tmp_path / "a", CountingStore(tmp_path / "bucket")
    init_board(data)
    rep = Replicator(replica, data, interval=3600)
    rep.take_lease()
    rep.record("something happened")
    replica.puts.clear()
    assert rep.push_if_changed()
    [batch] = replica.puts
    assert batch[0].startswith("snapshots/")
    assert batch[1:] == ["LATEST.json", "LEASE.json", f"events/{rep.instance}.json"]


def test_start_keeps_trying_to_take_the_lease(tmp_path):
    data, replica = tmp_path / "a", CountingStore(tmp_path / "bucket", fail_puts=2)
    init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.01)
    rep.start(lambda: 0)
    rep.stop(final_push=False)
    assert _json(tmp_path / "bucket" / "LEASE.json")["instance"] == rep.instance


def test_owner_hands_over_on_request_before_it_is_stopped(tmp_path):
    from msgboard.replicate import request_handover

    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        c.post(ch["id"], "acknowledged by the old instance")
        # the new instance asks while the old one keeps running: no SIGTERM, so no deadline
        assert request_handover(replica, "new", time.time(), poll=0.05) == "released"
        lease = _json(tmp_path / "bucket" / "LEASE.json")
        assert lease["released"] and lease["handover_to"] == "new" and rep.handed_over
        with pytest.raises(Exception) as exc:
            c.post(ch["id"], "after the hand-over")
        assert getattr(exc.value, "status", None) == 503   # clients retry and reach the new instance
    assert _json(tmp_path / "bucket" / "LEASE.json")["handover_to"] == "new"   # stopping changed nothing
    restore(replica, tmp_path / "b")
    with ThreadedServer(tmp_path / "b") as s, Client(s.url, token) as c:
        assert [m["body"] for m in c.read(ch["id"])["messages"]] == ["acknowledged by the old instance"]


def test_newest_requester_gets_the_replica(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    lease = tmp_path / "bucket" / "LEASE.json"
    with ThreadedServer(data, replicator=rep):
        _request(replica, "b", time.time())
        wait_for(lambda: _json(lease).get("handover_to") == "b")
        _request(replica, "c", time.time())   # a second restart before "b" took over
        wait_for(lambda: _json(lease).get("handover_to") == "c")


def test_owner_takes_the_replica_back_if_nobody_takes_it(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05, reclaim_after=0.5)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        _request(replica, "gone", time.time())   # asked, then died before taking over
        wait_for(lambda: rep.handed_over)
        wait_for(lambda: not rep.handed_over)
        c.post(ch["id"], "accepted again")
        lease = _json(tmp_path / "bucket" / "LEASE.json")
        assert lease["instance"] == rep.instance and not lease["released"]
        time.sleep(0.3)
        assert not rep.handed_over                  # the same request is not served twice


def test_request_handover_outcomes(tmp_path):
    import threading

    from msgboard.replicate import request_handover

    replica, bucket = DirStore(tmp_path / "bucket"), tmp_path / "bucket"

    def lease(**fields):
        replica.put([(json.dumps({"protocol": 2, "instance": "old", "since": 10.0, "heartbeat": time.time(),
                                  "released": False, **fields}).encode(), "LEASE.json")])

    def ask(instance, since):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        return request_handover(replica, instance, since, poll=0.05, stop=stop)

    assert request_handover(replica, "me", 20.0) == "free"
    lease(released=True)                              # released when it stopped
    assert request_handover(replica, "me", 20.0) == "released"
    lease(heartbeat=time.time() - 500)
    assert request_handover(replica, "me", 20.0, stale_after=300) == "stale"
    replica.put([(json.dumps({"instance": "old", "heartbeat": time.time()}).encode(), "LEASE.json")])
    assert request_handover(replica, "me", 20.0) == "legacy"   # hands over on SIGTERM only
    lease(released=True, handover_to="someone-else")
    assert ask("me", 20.0) == "stopped"               # not ours: keep waiting
    lease()
    assert ask("me", 20.0) == "stopped" and _json(bucket / "HANDOVER.json")["instance"] == "me"
    assert ask("older", 15.0) == "stopped" and _json(bucket / "HANDOVER.json")["instance"] == "me"
    assert ask("newer", 30.0) == "stopped" and _json(bucket / "HANDOVER.json")["instance"] == "newer"
    lease(since=40.0)                                 # an owner newer than the one asking: never ask it
    assert ask("late", 35.0) == "stopped" and _json(bucket / "HANDOVER.json")["instance"] == "newer"


def test_a_late_lease_write_from_an_older_instance_does_not_fence_the_newer_one(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    old = Replicator(replica, data, interval=3600)
    new = Replicator(replica, data, interval=3600)
    new.take_lease()
    old.take_lease()            # a heartbeat the old instance sent before it was replaced lands late
    assert new.push_if_changed() and not new.fenced
    assert _json(tmp_path / "bucket" / "LEASE.json")["instance"] == new.instance


def test_space_restart_hands_over_before_the_new_port_opens(tmp_path):
    # Two Space app processes as an HF Space restart runs them: the new one starts while the old one
    # serves, and the platform stops the old one only once the new one's port answers.
    import os
    import subprocess
    import sys

    from msgboard.auth import new_token

    from .conftest import free_port

    token = new_token()[1]
    env = {**os.environ, "MSGBOARD_OPERATOR_TOKEN": token, "MSGBOARD_REPLICA": str(tmp_path / "bucket"),
           "MSGBOARD_SNAPSHOT_INTERVAL": "3600"}

    def app(name):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "msgboard.space.app"], stderr=subprocess.PIPE, text=True,
                                env={**env, "PORT": str(port), "MSGBOARD_DATA_DIR": str(tmp_path / name)})
        return proc, f"http://127.0.0.1:{port}"

    def ready(url):
        try:
            return httpx.get(url + "/v1/health", timeout=1).json().get("status") == "ok"
        except httpx.TransportError:
            return False

    old, old_url = app("old")
    new = None
    try:
        wait_for(lambda: ready(old_url), timeout=30)
        with Client(old_url, token, max_retries=0) as c:
            ch = c.create_channel("x")
            c.post(ch["id"], "posted right before the restart")
            new, new_url = app("new")
            wait_for(lambda: ready(new_url), timeout=60)
            with pytest.raises(Exception) as exc:   # the old one handed over before the new port opened
                c.post(ch["id"], "too late for the old one")
            assert getattr(exc.value, "status", None) == 503
        with Client(new_url, token) as c:
            assert [m["body"] for m in c.read(ch["id"])["messages"]] == ["posted right before the restart"]
            c.post(ch["id"], "posted after the restart")
    finally:
        for proc in (old, new):
            if proc is not None:
                proc.terminate()
                _, err = proc.communicate(timeout=30)
    assert "replica handover: released" in err
