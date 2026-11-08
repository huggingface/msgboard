import json
import sqlite3
import threading
import time
from contextlib import ExitStack

import httpx
import pytest

from msgboard.client import Client
from msgboard.local import ThreadedServer
from msgboard.replicate import DirStore, Replicator, request_handover, restore

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
    assert old.claim()
    assert old.push_if_changed()
    new = Replicator(replica, tmp_path / "other", interval=3600)
    assert new.claim()                   # an overlapping replacement starts
    old.generation_fn = lambda: 99       # the old one has more writes...
    assert not old.push_if_changed()     # ...but may no longer move LATEST
    assert old.status["fenced"]


def test_restore_safety(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    assert restore(replica, tmp_path / "empty") is None   # nothing to restore
    rep = Replicator(replica, data, interval=3600)
    rep.claim()
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
    assert wait_for_release(replica, stale_after=0.2, poll=0.05) == "stale"   # unchanged for that long


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


def _ask(replica, instance, since=None, timeout=10.0, **kwargs):
    # request_handover with a deadline, so a broken hand-over fails the test instead of hanging it
    stop = threading.Event()
    timer = threading.Timer(timeout, stop.set)
    timer.start()
    try:
        return request_handover(replica, instance, time.time() if since is None else since, poll=0.05,
                                stop=stop, **kwargs)
    finally:
        timer.cancel()


class SlowStore(DirStore):
    """Uploads that take a while, as rate-limited Xet uploads do (here seconds stand for minutes). The
    content is made before the delay, so a lease's timestamps are as old as the upload."""

    def __init__(self, root, delay):
        super().__init__(root)
        self.delay = delay   # destinations -> seconds

    def put(self, items):
        time.sleep(self.delay([dest for _, dest in items]))
        super().put(items)


def _bodies(data_dir, channel_id, token):
    with ThreadedServer(data_dir) as s, Client(s.url, token) as c:
        return [m["body"] for m in c.read(channel_id, limit=500)["messages"]]


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
    rep.claim()
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
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        c.post(ch["id"], "acknowledged by the old instance")
        # the new instance asks while the old one keeps running: no SIGTERM, so no 10 s deadline
        assert _ask(replica, "new") == "released"
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
    lease()                                           # nothing from the owner lands for stale_after s
    assert request_handover(replica, "me", 20.0, stale_after=0.2, poll=0.05) == "stale"
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
    assert old.claim()
    late = (tmp_path / "bucket" / "LEASE.json").read_bytes()
    assert new.claim()
    (tmp_path / "bucket" / "LEASE.json").write_bytes(late)  # a heartbeat the old one sent before it was replaced
    assert new.push_if_changed() and not new.fenced
    assert _json(tmp_path / "bucket" / "LEASE.json")["instance"] == new.instance
    old.generation_fn = lambda: 99
    assert not old.push_if_changed() and old.fenced


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


# ------------------------------------- slow uploads and superseded instances: review #4 of #25


def test_a_final_push_slower_than_stale_after_does_not_expire_the_owner(tmp_path):
    # Item 1: the lease is stamped when an upload starts, and requesters counted the owner dead
    # stale_after s after that stamp, while its final snapshot was still uploading.
    slow = threading.Event()
    replica = SlowStore(tmp_path / "bucket",
                        lambda dests: 1.5 if slow.is_set() and dests[0].startswith("snapshots/") else 0.01)
    data = tmp_path / "a"
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05, heartbeat=0.2)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        c.post(ch["id"], "acknowledged before the hand-over")
        slow.set()
        assert _ask(replica, "new", stale_after=0.8) == "released"   # the final push alone takes 1.5 s
    restore(replica, tmp_path / "b")
    assert _bodies(tmp_path / "b", ch["id"], token) == ["acknowledged before the hand-over"]


def test_slow_uploads_in_steady_state_do_not_make_the_owner_look_dead(tmp_path):
    # Item 1: with every upload taking 150-225 s (here 0.5-0.75 s) against stale_after 300 s (1 s), a
    # lease was always at least one upload old when read, so requesters counted a live owner dead.
    durations = iter([0.5, 0.75] * 1000)
    replica = SlowStore(tmp_path / "bucket", lambda dests: next(durations))
    data = tmp_path / "a"
    token = init_board(data)
    rep = Replicator(replica, data, interval=0.1, poll=0.05, heartbeat=0.1)
    acked, stop_writing = [], threading.Event()
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token) as c:
        ch = c.create_channel("x")

        def writer():
            with Client(s.url, token, max_retries=0) as w:
                while not stop_writing.is_set():
                    body = f"m{len(acked)}"
                    try:
                        w.post(ch["id"], body)
                    except Exception:   # 503: the hand-over closed writes
                        return
                    acked.append(body)
                    time.sleep(0.05)

        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(2)   # a few slow pushes
        try:
            assert _ask(replica, "new", stale_after=1.0, timeout=30) == "released"
        finally:
            stop_writing.set()
            thread.join()
        assert rep.status["pushes"] >= 2
    restore(replica, tmp_path / "b")
    assert _bodies(tmp_path / "b", ch["id"], token) == acked and len(acked) > 10


def test_delayed_waiter_cannot_rollback_acknowledged_new_owner_data(tmp_path):
    # Item 2, as lvwerra's review of #25 reproduced it (their regression test, reformatted, with deadlines on
    # the hand-over requests): B restored the replica, then was delayed before startup while a newer C took it
    # over and acknowledged a message. B then took the lease blindly, and its final snapshot, named later,
    # rolled the board back.
    root = tmp_path
    with ExitStack() as stack:
        store = DirStore(root / "bucket")
        token = init_board(root / "a")
        a = Replicator(store, root / "a", interval=3600, poll=.01)
        sa = stack.enter_context(ThreadedServer(root / "a", replicator=a))
        ca = stack.enter_context(Client(sa.url, token, max_retries=0))
        ch = ca.create_channel("history")["id"]
        ca.post(ch, "initial acknowledged")
        b = Replicator(store, root / "b", interval=3600, poll=5)
        time.sleep(.01)
        c = Replicator(store, root / "c", interval=3600, poll=5)
        assert _ask(store, b.instance, b.since) == "released"
        restore(store, root / "b")  # b has restored but has not yet taken its lease
        assert _ask(store, c.instance, c.since) == "released"
        restore(store, root / "c")
        sc = stack.enter_context(ThreadedServer(root / "c", replicator=c))
        cc = stack.enter_context(Client(sc.url, token, max_retries=0))
        cc.post(ch, "newest acknowledged")
        c.push_if_changed()
        before = json.loads((root / "bucket" / "LEASE.json").read_text())["instance"]
        stack.enter_context(ThreadedServer(root / "b", replicator=b))  # b resumes after c is already serving
        after = json.loads((root / "bucket" / "LEASE.json").read_text())["instance"]
        time.sleep(1.1)  # the obsolete container's final snapshot has a later filename second
        b.stop()
        restore(store, root / "restored")
        with sqlite3.connect(root / "restored" / "board.db") as db:
            restored = [r[0] for r in db.execute("SELECT body FROM messages ORDER BY seq")]
        live = [m["body"] for m in cc.read(ch)["messages"]]
        result = {"newest_owned_before_delayed_start": before == c.instance,
                  "delayed_older_instance_replaced_lease": after == b.instance,
                  "live_newer_messages": live, "restored_messages_after_older_stop": restored,
                  "acknowledged_message_missing_on_restore": "newest acknowledged" not in restored}
        print(json.dumps(result, indent=2))
        assert not result["acknowledged_message_missing_on_restore"], result


def test_a_superseded_replacement_does_not_answer_a_newer_request(tmp_path):
    # Item 2: A released the replica to B, then to the newer C, which B had not taken. B started late,
    # took the lease anyway and answered C's request (still in HANDOVER.json) with its stale database,
    # published under a newer snapshot name than C's last push.
    store = DirStore(tmp_path / "bucket")
    token = init_board(tmp_path / "a")
    with ExitStack() as stack:
        a = Replicator(store, tmp_path / "a", interval=3600, poll=0.02)
        sa = stack.enter_context(ThreadedServer(tmp_path / "a", replicator=a))
        ca = stack.enter_context(Client(sa.url, token, max_retries=0))
        ch = ca.create_channel("x")["id"]
        ca.post(ch, "initial")
        b = Replicator(store, tmp_path / "b", interval=3600, poll=0.02)
        time.sleep(0.01)
        c = Replicator(store, tmp_path / "c", interval=3600, poll=0.02)
        assert _ask(store, b.instance, b.since) == "released"
        restore(store, tmp_path / "b")
        assert _ask(store, c.instance, c.since) == "released"
        restore(store, tmp_path / "c")
        sc = stack.enter_context(ThreadedServer(tmp_path / "c", replicator=c))
        stack.enter_context(Client(sc.url, token, max_retries=0)).post(ch, "acknowledged by C")
        assert c.push_if_changed()
        stack.enter_context(ThreadedServer(tmp_path / "b", replicator=b))   # late
        time.sleep(0.5)                                                      # many of B's polls
        assert b.fenced and b.status["pushes"] == 0 and not c.fenced
        assert _json(tmp_path / "bucket" / "LEASE.json")["instance"] == c.instance
        b.stop()
        assert not [p for p in store.list("snapshots/") if b.instance in p]
    restore(store, tmp_path / "restored")
    assert _bodies(tmp_path / "restored", ch, token) == ["initial", "acknowledged by C"]


def test_a_late_release_from_a_replaced_owner_is_void(tmp_path):
    # Item 2: A released the replica to B, and B took it and served. A release from A that lands later
    # (a redirect to the newer C, decided before A saw B's claim) must not give C the replica: C would
    # restore without B's writes. C asks B instead, which hands it over with them.
    store = DirStore(tmp_path / "bucket")
    token = init_board(tmp_path / "a")
    with ExitStack() as stack:
        a = Replicator(store, tmp_path / "a", interval=3600, poll=0.02)
        sa = stack.enter_context(ThreadedServer(tmp_path / "a", replicator=a))
        ca = stack.enter_context(Client(sa.url, token, max_retries=0))
        ch = ca.create_channel("x")["id"]
        ca.post(ch, "initial")
        b = Replicator(store, tmp_path / "b", interval=3600, poll=2.0)   # so the late release stays a while
        time.sleep(0.01)
        c_since = time.time()
        assert _ask(store, b.instance, b.since) == "released"
        redirect = {**_json(tmp_path / "bucket" / "LEASE.json"), "handover_to": "c", "grant": 3}
        restore(store, tmp_path / "b")
        sb = stack.enter_context(ThreadedServer(tmp_path / "b", replicator=b))
        stack.enter_context(Client(sb.url, token, max_retries=0)).post(ch, "acknowledged by B")
        store.put([(json.dumps(redirect).encode(), "LEASE.json")])   # A's late redirect lands
        assert _ask(store, "c", c_since, timeout=0.3) == "stopped"   # not released: B's claim outranks A
        assert _ask(store, "c", c_since) == "released"               # B handed it over
        assert b.handed_over and not b.fenced
    restore(store, tmp_path / "restored")
    assert _bodies(tmp_path / "restored", ch, token) == ["initial", "acknowledged by B"]


def test_a_successor_outranks_its_predecessor_in_the_same_second(tmp_path):
    # Flock #95 review #3, item 4 of review #4: snapshots were ordered by UTC second, then by a generation
    # counter that restarts in each container, so a predecessor with more writes outranked its successor
    # in the same second: restore lost the successor's writes, and pruning deleted its snapshot.
    replica = DirStore(tmp_path / "bucket")
    for name in "ab":
        init_board(tmp_path / name)
    old = Replicator(replica, tmp_path / "a", interval=3600, keep=1)
    new = Replicator(replica, tmp_path / "b", interval=3600, keep=1)
    while time.time() % 1 > 0.5:   # both pushes within one second
        time.sleep(0.02)
    try:
        old.start(lambda: 50)
        assert old.push_if_changed()
        new.start(lambda: 1)       # takes over, with fewer writes of its own so far
        assert new.push_if_changed()
        names = [p for p in replica.list("snapshots/") if p.endswith(".db")]
        assert len(names) == 1 and new.instance in names[0]   # pruning kept the successor's
        assert new.instance in restore(replica, tmp_path / "c")["snapshot"]
    finally:
        old.stop(final_push=False)
        new.stop(final_push=False)


def test_a_hand_over_commits_writes_queued_before_it(tmp_path):
    # Item 6: a write accepted before the hand-over but queued behind a slow one is in the final snapshot.
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    posted = {}
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        s.board.db._writer.submit(time.sleep, 0.5)   # a slow write ahead in the queue

        def post():
            with Client(s.url, token, max_retries=0) as w:
                posted["message"] = w.post(ch["id"], "queued behind it")

        thread = threading.Thread(target=post)
        thread.start()
        time.sleep(0.1)                               # accepted, and waiting for the writer
        assert _ask(replica, "new") == "released"
        thread.join()
        assert posted["message"]["body"] == "queued behind it"
    restore(replica, tmp_path / "b")
    assert _bodies(tmp_path / "b", ch["id"], token) == ["queued behind it"]


def test_take_over_claims_the_replica_before_serving(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    with ThreadedServer(data, replicator=rep):
        new = Replicator(replica, tmp_path / "b", interval=3600)
        assert new.take_over(poll=0.05) == "released"
        assert _json(tmp_path / "bucket" / "LEASE.json")["instance"] == new.instance
        assert [p for p in replica.list("claims/") if new.instance in p] == [f"claims/{new.epoch:08d}-"
                                                                          f"{int(new.since * 1000)}-{new.instance}.json"]
        assert new.epoch == rep.epoch + 1
        older = Replicator(replica, tmp_path / "c", interval=3600)
        older.since = rep.since + 0.001          # started before the new owner: never takes it from it
        assert older.claim() is False


def test_a_hand_over_waits_for_storage_writes_and_their_rollback(tmp_path):
    # Item 3: storage objects go straight to the shared bucket. A storage upload in flight at the hand-over
    # used to finish (or roll back) after the release, over what the next owner had stored by then.
    log = []

    class RecordingStore(DirStore):
        def put(self, items):
            if any(dest.startswith("storage/") for _, dest in items):
                time.sleep(0.8)   # a slow storage upload
            super().put(items)
            released = any(dest == "LEASE.json" and json.loads(source)["released"] for source, dest in items)
            log.append(("release" if released else "put", [dest for _, dest in items]))

        def delete(self, paths):
            super().delete(paths)
            log.append(("delete", list(paths)))

    data, replica = tmp_path / "a", RecordingStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    outcome = {}
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        handle = c.me()["external_id"]

        def upload():
            with httpx.Client(base_url=s.url, headers={"X-Msgboard-Token": token}) as h:
                outcome["status"] = h.put(f"/v1/storage/{handle}/notes.txt", content=b"draft").status_code

        thread = threading.Thread(target=upload)
        thread.start()
        time.sleep(0.2)                               # the storage upload is in flight
        assert _ask(replica, "new") == "released"
        thread.join()
    released = [op for op, _ in log].index("release")
    assert not [entry for entry in log[released:] if any(d.startswith("storage/") for d in entry[1])], log
    assert outcome["status"] in (201, 503)            # stored before the hand-over, or refused and rolled back


def test_a_dead_owner_behind_a_late_lease_write_is_taken_over(tmp_path):
    # B claimed the replica, a late write from A replaced its lease, and both died: the next instance
    # takes over above B's epoch instead of waiting for B forever.
    replica = DirStore(tmp_path / "bucket")
    replica.put([(b"{}", "claims/00000002-2000-b.json"),
                 (json.dumps({"protocol": 2, "instance": "a", "since": 1.0, "epoch": 1,
                              "released": False}).encode(), "LEASE.json")])
    stop = threading.Event()
    timer = threading.Timer(10, stop.set)
    timer.start()
    rep = Replicator(replica, tmp_path / "r", stop=stop)
    try:
        assert rep.take_over(stale_after=0.3, poll=0.05) == "stale"
    finally:
        timer.cancel()
    assert rep.epoch == 3
