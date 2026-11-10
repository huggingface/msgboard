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


def _request(replica, instance, since, **fields):
    replica.put([(json.dumps({"instance": instance, "since": since, **fields}).encode(), "HANDOVER.json")])


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
    assert old.claim() and old.push_if_changed()
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
        name = f"claims/{new.epoch:08d}-{new.since!r}-{new.instance}"
        assert [p for p in replica.list("claims/") if new.instance in p] == [name + ".json", name + ".won"]
        assert new.epoch == rep.epoch + 1
        lease = _json(tmp_path / "bucket" / "LEASE.json")   # its lease comes once it serves (start())
        assert lease["instance"] == rep.instance and lease["handover_to"] == new.instance
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


def test_a_cancelled_storage_delete_holds_off_the_hand_over_until_it_lands(tmp_path):
    # Item 5 of review #5: the DELETE route awaited the bucket delete directly. When uvicorn cancelled the
    # request (it does at its shutdown deadline), the route left storage_write while the delete still ran, so
    # a hand-over released the replica first, and the late delete could remove the next owner's file.
    import asyncio

    log, entered, release = [], threading.Event(), threading.Event()

    class SlowDeleteStore(DirStore):
        def put(self, items):
            super().put(items)
            if any(dest == "LEASE.json" and json.loads(source)["released"] for source, dest in items):
                log.append("release")

        def delete(self, paths):
            if any(path.startswith("storage/") for path in paths):
                entered.set()
                release.wait(10)
                log.append("storage delete")
            super().delete(paths)

    data, replica = tmp_path / "a", SlowDeleteStore(tmp_path / "bucket")
    token = init_board(data)
    headers = {"X-Msgboard-Token": token}
    rep = Replicator(replica, data, interval=3600, poll=0.05)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token) as c:
        handle = c.me()["external_id"]
        assert httpx.put(f"{s.url}/v1/storage/{handle}/notes.txt", content=b"draft", headers=headers).status_code == 201
        deleting = threading.Thread(target=lambda: httpx.delete(f"{s.url}/v1/storage/{handle}/notes.txt",
                                                                headers=headers, timeout=15))
        deleting.start()
        asking = threading.Thread(target=lambda: _ask(replica, "new"))
        try:
            assert entered.wait(10)                                  # the bucket delete is running

            async def cancel_requests():                             # as uvicorn does at its shutdown deadline
                for task in asyncio.all_tasks():
                    if task.get_coro().__qualname__.endswith("run_asgi"):
                        task.cancel()

            loop = s.board.notifier.loop
            asyncio.run_coroutine_threadsafe(cancel_requests(), loop).result(5)
            asking.start()                                           # a newer instance asks for the replica
            time.sleep(0.5)
        finally:
            release.set()
            deleting.join(timeout=15)
            if asking.ident is not None:
                asking.join(timeout=15)
    assert log[:2] == ["storage delete", "release"], log


def test_a_dead_owner_behind_a_late_lease_write_is_taken_over(tmp_path):
    # B claimed the replica, a late write from A replaced its lease, and both died: the next instance
    # takes over above B's epoch instead of waiting for B forever.
    replica = DirStore(tmp_path / "bucket")
    replica.put([(b"{}", "claims/00000002-2.0-b.json"),
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


def test_a_replacement_that_claims_and_dies_before_serving_does_not_block_the_next(tmp_path):
    # Seen live in a double restart: the first replacement claimed the replica, and the platform discarded
    # it before it served. The old owner took the claim as the end of the hand-over, and the replacement
    # the platform kept, older than the discarded one, could not ask it: it waited out stale_after.
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    stop = threading.Event()
    timer = threading.Timer(15, stop.set)
    timer.start()
    a = Replicator(replica, data, interval=3600, poll=0.05, reclaim_after=0.5)
    try:
        with ThreadedServer(data, replicator=a) as s, Client(s.url, token, max_retries=0) as c:
            ch = c.create_channel("x")["id"]
            c.post(ch, "before the restarts")
            kept = Replicator(replica, tmp_path / "c", interval=3600, stop=stop)
            time.sleep(0.01)
            discarded = Replicator(replica, tmp_path / "b", interval=3600, stop=stop)
            assert discarded.take_over(poll=0.05) == "released"   # claims, then never serves
            assert kept.take_over(stale_after=300, poll=0.05) == "released"
            assert kept.epoch > discarded.epoch
            discarded.start(lambda: 0)                             # too late: it must not serve
            assert discarded.fenced
    finally:
        timer.cancel()
    restore(replica, tmp_path / "c")
    assert _bodies(tmp_path / "c", ch, token) == ["before the restarts"]


def test_a_replacement_stopped_before_serving_releases_the_replica(tmp_path):
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    a = Replicator(replica, data, interval=3600, poll=0.05)
    with ThreadedServer(data, replicator=a):
        first = Replicator(replica, tmp_path / "b", interval=3600)
        assert first.take_over(poll=0.05) == "released"
        first.stop()                                    # SIGTERM while restoring, say
        lease = _json(tmp_path / "bucket" / "LEASE.json")
        assert (lease["instance"], lease["released"], lease["handover_to"], lease["serving"]) == (
            first.instance, True, None, False)
        assert not [p for p in replica.list("snapshots/") if first.instance in p]   # nothing of its own
        assert _ask(replica, "next", timeout=5) == "released"


def test_a_slow_request_upload_does_not_use_up_the_owners_time(tmp_path):
    # Seen live with 20 s uploads: the requester's clock started before its own request had landed, and the
    # owner, which can answer only after that, looked dead early.
    replica = SlowStore(tmp_path / "bucket", lambda dests: 1.2 if dests == ["HANDOVER.json"] else 0.01)
    data = tmp_path / "a"
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600, poll=0.4)   # no lease writes of its own meanwhile
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        c.post(c.create_channel("x")["id"], "acknowledged")
        assert _ask(replica, "new", stale_after=0.8) == "released"   # it sees the request up to 0.4 s later


# ------------------------------------------ concurrent claimants and recovery: review #5 of #25


class GatedStore(DirStore):
    """Holds back chosen instances' claim uploads until released, as a slow (rate-limited) upload would."""

    def __init__(self, root):
        super().__init__(root)
        self.gates: dict[str, threading.Event] = {}
        self.held: set[str] = set()
        self.fail_next_claim = False

    def hold(self, instance):
        self.gates[instance] = threading.Event()

    def release(self, instance):
        self.gates[instance].set()

    def put(self, items):
        for _, dest in items:
            if dest.startswith("claims/") and dest.endswith(".json"):
                if self.fail_next_claim:
                    self.fail_next_claim = False
                    raise ConnectionError("Rate limited: 429 Too Many Requests")
                gate = self.gates.get(dest.removesuffix(".json").rsplit("-", 1)[1])
                if gate is not None:
                    self.held.add(dest)
                    gate.wait(30)
        super().put(items)

    def holding(self, rep):
        return any(rep.instance in dest for dest in self.held)


def _take_over(rep, results, key, **kwargs):
    try:
        results[key] = rep.take_over(poll=0.02, **kwargs)
    except RuntimeError:   # stopped while claiming
        results[key] = "stopped"


def _withdrawn(store, rep):
    return any(rep.instance in p and p.endswith(".withdrawn") for p in store.list("claims/"))


def _contest(store, first, second, results):
    """Run `first` and `second`'s take_over in threads: `second` claims first but its upload lands only after
    `first` has finished. Returns who won."""
    store.hold(second.instance)
    threads = [threading.Thread(target=_take_over, args=(second, results, "second"))]
    threads[0].start()
    wait_for(lambda: store.holding(second), timeout=15)
    threads.append(threading.Thread(target=_take_over, args=(first, results, "first")))
    threads[1].start()
    wait_for(lambda: "first" in results, timeout=25)
    store.release(second.instance)
    wait_for(lambda: "second" in results or _withdrawn(store, second), timeout=15)
    first._stop.set()
    second._stop.set()
    for t in threads:
        t.join(timeout=30)
    return [key for key in ("first", "second") if results.get(key) not in (None, "stopped")]


def test_two_claimants_after_a_redirect_do_not_both_serve(tmp_path):
    # Item 1: A released the replica to B, whose claim upload was slow, so A released it to the newer C, which
    # claimed too. B confirmed before C's claim landed and served; then C's landed, and C served as well.
    store = GatedStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    a = Replicator(store, tmp_path / "a", interval=3600, poll=0.02, regrant_after=0.2)
    results = {}
    with ThreadedServer(tmp_path / "a", replicator=a):
        b = Replicator(store, tmp_path / "b", interval=3600, poll=0.02, stop=threading.Event())
        time.sleep(0.01)
        c = Replicator(store, tmp_path / "c", interval=3600, poll=0.02, stop=threading.Event())
        store.hold(b.instance)
        store.hold(c.instance)
        threads = [threading.Thread(target=_take_over, args=(b, results, "b"))]
        threads[0].start()
        wait_for(lambda: store.holding(b), timeout=15)
        threads.append(threading.Thread(target=_take_over, args=(c, results, "c")))
        threads[1].start()
        wait_for(lambda: store.holding(c), timeout=15)        # A released it to C, which claims too
        store.release(b.instance)                              # B's claim lands first
        wait_for(lambda: "b" in results, timeout=25)
        store.release(c.instance)
        wait_for(lambda: "c" in results or _withdrawn(store, c), timeout=15)
        b._stop.set()
        c._stop.set()
        for t in threads:
            t.join(timeout=30)
    assert [k for k in ("b", "c") if results.get(k) not in (None, "stopped")] == ["b"], results


def test_two_waiters_after_a_clean_release_do_not_both_serve(tmp_path):
    # Item 1: a release to nobody in particular (stop()) let two waiters claim the same epoch, and both served.
    store = GatedStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    with ThreadedServer(tmp_path / "a", replicator=Replicator(store, tmp_path / "a", interval=3600, poll=0.02)):
        pass                                                   # stopped: final snapshot, release to anyone
    second = Replicator(store, tmp_path / "c", interval=3600, poll=0.02, stop=threading.Event())
    first = Replicator(store, tmp_path / "b", interval=3600, poll=0.02, stop=threading.Event())
    results = {}
    assert _contest(store, first, second, results) == ["first"], results


def test_two_requesters_that_judge_the_owner_dead_do_not_both_serve(tmp_path):
    # Item 1: two instances that both counted the owner dead both claimed, and both served.
    store = GatedStore(tmp_path / "bucket")
    store.put([(json.dumps({"protocol": 2, "instance": "dead", "since": 1.0, "epoch": 1, "released": False,
                            "heartbeat": time.time()}).encode(), "LEASE.json")])
    first = Replicator(store, tmp_path / "b", interval=3600, poll=0.02, stop=threading.Event())
    time.sleep(0.01)
    second = Replicator(store, tmp_path / "c", interval=3600, poll=0.02, stop=threading.Event())
    results = {}
    store.hold(second.instance)
    threads = [threading.Thread(target=_take_over, args=(second, results, "second"), kwargs={"stale_after": 0.3})]
    threads[0].start()
    wait_for(lambda: store.holding(second), timeout=15)
    threads.append(threading.Thread(target=_take_over, args=(first, results, "first"), kwargs={"stale_after": 0.3}))
    threads[1].start()
    wait_for(lambda: "first" in results, timeout=25)
    store.release(second.instance)
    wait_for(lambda: "second" in results or _withdrawn(store, second), timeout=15)
    first._stop.set()
    second._stop.set()
    for t in threads:
        t.join(timeout=30)
    assert [k for k in ("first", "second") if results.get(k) not in (None, "stopped")] == ["first"], results


def test_an_owner_checks_the_claims_even_when_the_lease_names_it(tmp_path):
    # Item 1: a late lease write from B kept B pushing after C's higher claim had landed, because B checked
    # the claims only when the lease named another instance.
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    b = Replicator(replica, data, interval=3600)
    c = Replicator(replica, data, interval=3600)
    assert b.claim() and b.push_if_changed()
    late = (tmp_path / "bucket" / "LEASE.json").read_bytes()
    assert c.claim()
    (tmp_path / "bucket" / "LEASE.json").write_bytes(late)    # B's heartbeat lands after C's claim
    b.generation_fn = lambda: 99
    assert not b.push_if_changed() and b.fenced


def test_the_old_owner_does_not_take_the_replica_back_from_a_slow_live_claimant(tmp_path):
    # Item 2: the old owner counted 2 minutes from its release and took the replica back, under a higher
    # epoch, from a new container whose claim upload was still running; that one then never served.
    store = GatedStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    a = Replicator(store, tmp_path / "a", interval=3600, poll=0.02, reclaim_after=0.5)
    results = {}
    with ThreadedServer(tmp_path / "a", replicator=a):
        n = Replicator(store, tmp_path / "n", interval=3600, poll=0.02, heartbeat=0.1, stop=threading.Event())
        store.hold(n.instance)
        t = threading.Thread(target=_take_over, args=(n, results, "n"))
        t.start()
        wait_for(lambda: store.holding(n), timeout=15)
        time.sleep(1.5)                     # three reclaim windows, while its heartbeats land
        store.release(n.instance)
        wait_for(lambda: "n" in results, timeout=20)
        n._stop.set()
        t.join(timeout=30)
        assert results["n"] == "released"
        assert a.handed_over and not a.fenced and a.epoch < n.epoch


def test_a_request_given_up_on_does_not_refuse_the_next_one(tmp_path):
    # Item 2: an instance the owner once took the replica back from was refused for good.
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    init_board(data)
    a = Replicator(replica, data, interval=3600, poll=0.02, reclaim_after=0.3)
    with ThreadedServer(data, replicator=a):
        _request(replica, "x", time.time(), at=1.0)
        wait_for(lambda: a.handed_over)
        wait_for(lambda: not a.handed_over)             # x never claimed it: taken back
        _request(replica, "x", time.time(), at=2.0)     # x asks again
        wait_for(lambda: a.handed_over and _json(tmp_path / "bucket" / "LEASE.json").get("handover_to") == "x")


def test_an_instance_that_lost_the_replica_takes_it_once_the_winner_stops(tmp_path):
    # Item 3: being superseded was permanent. B lost to C; C then stopped before serving (released the
    # replica to anyone), and B kept waiting with its port closed instead of taking it.
    store = GatedStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    with ThreadedServer(tmp_path / "a", replicator=Replicator(store, tmp_path / "a", interval=3600, poll=0.02)):
        pass
    b = Replicator(store, tmp_path / "b", interval=3600, poll=0.02, stop=threading.Event())
    time.sleep(0.01)
    c = Replicator(store, tmp_path / "c", interval=3600, poll=0.02, stop=threading.Event())
    results = {}
    store.hold(b.instance)
    tb = threading.Thread(target=_take_over, args=(b, results, "b"))
    tb.start()
    wait_for(lambda: store.holding(b), timeout=15)
    assert c.take_over(poll=0.02) == "released"   # C wins while B's claim is uploading
    store.release(b.instance)
    time.sleep(0.5)
    assert "b" not in results or results["b"] == "stopped"   # B gave way...
    c.stop()                                                 # ...and C stops before it serves
    wait_for(lambda: "b" in results, timeout=15)
    b._stop.set()
    tb.join(timeout=30)
    assert results["b"] == "released" and b.epoch > c.epoch


def test_an_instance_that_lost_the_replica_takes_it_once_the_winner_dies(tmp_path):
    # Item 3, the other way the winner goes away: C took the replica from under B and died before serving,
    # without releasing it. B, which kept asking, takes it once nothing from C has landed for the stale window.
    store = GatedStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    with ThreadedServer(tmp_path / "a", replicator=Replicator(store, tmp_path / "a", interval=3600, poll=0.02)):
        pass
    b = Replicator(store, tmp_path / "b", interval=3600, poll=0.02, stop=threading.Event())
    time.sleep(0.01)
    c = Replicator(store, tmp_path / "c", interval=3600, poll=0.02, stop=threading.Event())
    results = {}
    store.hold(b.instance)
    tb = threading.Thread(target=_take_over, args=(b, results, "b"), kwargs={"stale_after": 2.0})
    tb.start()
    try:
        wait_for(lambda: store.holding(b), timeout=15)
        assert c.take_over(poll=0.02) == "released"   # C wins while B's claim is uploading, then dies:
        store.release(b.instance)                     # nothing more from it lands, and it never releases
        time.sleep(0.5)
        assert "b" not in results                     # B gave way and asks C...
        wait_for(lambda: "b" in results, timeout=15)  # ...until nothing from C has landed for the window
    finally:
        store.release(b.instance)
        b._stop.set()
        tb.join(timeout=30)
    assert results["b"] == "stale" and b.epoch > c.epoch


def test_health_and_readiness_report_a_fenced_instance(tmp_path):
    # Item 3: a fenced instance answered /v1/health "ok" and /v1/ready "ready" while refusing every write.
    data, replica = tmp_path / "a", DirStore(tmp_path / "bucket")
    token = init_board(data)
    rep = Replicator(replica, data, interval=3600)
    with ThreadedServer(data, replicator=rep) as s, Client(s.url, token, max_retries=0) as c:
        c.create_channel("x")
        assert Replicator(replica, tmp_path / "b", interval=3600).claim()   # a newer instance takes over
        assert not rep.push_if_changed() and rep.fenced
        with httpx.Client(base_url=s.url) as h:
            health, ready = h.get("/v1/health"), h.get("/v1/ready")
        assert (health.status_code, health.json()["status"]) == (503, "fenced")
        assert (ready.status_code, ready.json()["status"]) == (503, "fenced")


def test_a_fenced_space_app_exits_so_the_platform_restarts_it(tmp_path):
    # Item 3: the Space app kept a fenced board up, serving reads and refusing writes, until restarted by hand.
    import os
    import subprocess
    import sys

    from msgboard.auth import new_token

    from .conftest import free_port

    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "msgboard.space.app"], stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "MSGBOARD_OPERATOR_TOKEN": new_token()[1], "PORT": str(port),
                                 "MSGBOARD_REPLICA": str(tmp_path / "bucket"), "MSGBOARD_DATA_DIR": str(tmp_path / "a"),
                                 "MSGBOARD_SNAPSHOT_INTERVAL": "3600", "MSGBOARD_LEASE_STALE_AFTER": "2"})
    try:
        wait_for(lambda: _up(f"http://127.0.0.1:{port}") and (tmp_path / "bucket" / "LEASE.json").exists(), timeout=30)
        assert Replicator(DirStore(tmp_path / "bucket"), tmp_path / "b", interval=3600).claim()
        assert proc.wait(timeout=30) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
        _, err = proc.communicate(timeout=30)
    assert "exiting so the platform restarts this one" in err


def test_a_space_app_restarted_in_its_container_serves_the_replica_not_its_old_board(tmp_path):
    # Item 3: a Space app that exits (fenced) or crashes may be started again in the same container, where
    # the board it left is still on disk. Another container may have taken writes since: it must restore
    # the replica's board instead of serving the one on its disk.
    import os
    import subprocess
    import sys

    from .conftest import free_port

    bucket = DirStore(tmp_path / "bucket")
    token = init_board(tmp_path / "other")
    with ThreadedServer(tmp_path / "other", replicator=Replicator(bucket, tmp_path / "other", interval=3600)) as s, \
            Client(s.url, token) as c:
        c.create_channel("taken-by-another-container")
    init_board(tmp_path / "a")
    with ThreadedServer(tmp_path / "a") as s, Client(s.url, (tmp_path / "a" / "operator.token").read_text().strip()) as c:
        c.create_channel("left-on-this-disk")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "msgboard.space.app"], stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "MSGBOARD_OPERATOR_TOKEN": token, "PORT": str(port),
                                 "MSGBOARD_REPLICA": str(tmp_path / "bucket"), "MSGBOARD_DATA_DIR": str(tmp_path / "a"),
                                 "MSGBOARD_SNAPSHOT_INTERVAL": "3600"})
    try:
        wait_for(lambda: _up(f"http://127.0.0.1:{port}")
                 and httpx.get(f"http://127.0.0.1:{port}/v1/health").json()["status"] == "ok", timeout=30)
        with Client(f"http://127.0.0.1:{port}", token) as c:
            names = {ch["name"] for ch in c.list_channels()}
    finally:
        proc.terminate()
        proc.communicate(timeout=30)
    assert "taken-by-another-container" in names and "left-on-this-disk" not in names
    assert [p.name.startswith("a.set-aside-") and (p / "board.db").exists() for p in tmp_path.glob("a.*")] == [True]


def test_a_space_app_stopped_while_it_claims_withdraws_its_claim(tmp_path):
    # Seen live in a double restart (lewtun/msgboard-27-test-1): the platform replaced the first new container
    # while it claimed the replica. SIGTERM ended the claim without a .withdrawn marker, and the old owner,
    # which hands the replica on to a newer container only once the first has given its claim up, waited
    # the 5-minute stale window instead.
    import os
    import signal
    import subprocess
    import sys

    from .conftest import free_port

    bucket = DirStore(tmp_path / "bucket")
    token = init_board(tmp_path / "a")
    a = Replicator(bucket, tmp_path / "a", interval=3600, poll=0.05)
    with ThreadedServer(tmp_path / "a", replicator=a):
        # an older instance's claim on the next epoch, alive as far as anyone knows: the app waits for it
        bucket.put([(b"{}", f"claims/00000002-{a.since + 0.001!r}-older.json")])
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "msgboard.space.app"], stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "MSGBOARD_OPERATOR_TOKEN": token, "PORT": str(port),
                                     "MSGBOARD_REPLICA": str(tmp_path / "bucket"),
                                     "MSGBOARD_DATA_DIR": str(tmp_path / "x"), "MSGBOARD_LEASE_STALE_AFTER": "60"})

        def its_claim():
            return [path for path in bucket.list("claims/") if path.startswith("claims/00000003-")]

        try:
            wait_for(lambda: any(path.endswith(".json") for path in its_claim()), timeout=30)
            time.sleep(0.5)                                    # it claimed, and waits in the election
            proc.send_signal(signal.SIGTERM)                   # the platform replaces it
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=30)
        assert any(path.endswith(".withdrawn") for path in its_claim()), its_claim()
        assert _ask(bucket, "newer") == "released"             # the owner hands the replica on at once


def test_a_reclaim_whose_upload_fails_once_still_takes_the_replica_back(tmp_path):
    # Item 4: _reclaim set the new epoch and dropped its grant before uploading; one failed upload left it
    # raising TypeError on every pass, quiesced, with nobody taking writes.
    store = GatedStore(tmp_path / "bucket")
    data = tmp_path / "a"
    token = init_board(data)
    a = Replicator(store, data, interval=3600, poll=0.02, reclaim_after=0.3)
    with ThreadedServer(data, replicator=a) as s, Client(s.url, token, max_retries=0) as c:
        ch = c.create_channel("x")
        _request(store, "x", time.time())
        wait_for(lambda: a.handed_over)
        store.fail_next_claim = True                 # its reclaim's first upload fails
        wait_for(lambda: not a.handed_over, timeout=10)
        c.post(ch["id"], "accepted again")
        lease = _json(tmp_path / "bucket" / "LEASE.json")
        assert lease["instance"] == a.instance and not lease["released"]


class HeldStore(DirStore):
    """Holds back uploads whose destinations match a rule until the rule's gate opens, as a slow
    (rate-limited) upload would: the content is made before the delay, so it lands late."""

    def __init__(self, root):
        super().__init__(root)
        self.rules: list[tuple] = []

    def hold(self, pred):
        gate, reached = threading.Event(), threading.Event()
        self.rules.append((pred, gate, reached))
        return gate, reached

    def put(self, items):
        dests = [dest for _, dest in items]
        for pred, gate, reached in list(self.rules):
            if any(pred(dest) for dest in dests):
                reached.set()
                assert gate.wait(30), f"gate for {dests} never opened"
        super().put(items)


def _won_marker_of(rep, epoch):
    return lambda dest: dest.startswith(f"claims/{epoch:08d}-") and dest.endswith(f"-{rep.instance}.won")


def test_a_reclaim_does_not_take_the_replica_from_a_claimant_that_started_serving(tmp_path):
    # The remaining no-CAS window of review #5 (#1/#2): the old owner A counted the new owner N dead (nothing
    # from N landed for reclaim_after) and took the replica back. N was only slow: it passed its last check
    # before A's won marker landed, served and acknowledged a write; then A's marker landed, A served too,
    # and N was fenced with that write in no snapshot.
    store = HeldStore(tmp_path / "bucket")
    token = init_board(tmp_path / "a")
    a = Replicator(store, tmp_path / "a", interval=3600, poll=0.02, reclaim_after=0.5)
    with ThreadedServer(tmp_path / "a", replicator=a) as sa, Client(sa.url, token, max_retries=0) as ca:
        ch = ca.create_channel("x")["id"]
        ca.post(ch, "before the restart")
        n = Replicator(store, tmp_path / "n", interval=3600, poll=0.02, heartbeat=3600, stop=threading.Event())
        gate, reached = store.hold(_won_marker_of(a, 3))       # A's reclaim: epoch 3, above N's 2
        try:
            assert n.take_over(poll=0.02) == "released" and n.epoch == 2
            assert reached.wait(10)                            # N was silent; A's won marker is uploading
            restore(store, tmp_path / "n")
            with ThreadedServer(tmp_path / "n", replicator=n) as sn, Client(sn.url, token, max_retries=0) as cn:
                cn.post(ch, "acknowledged by N")
                gate.set()                                     # A's won marker lands
                wait_for(lambda: _withdrawn(store, a))       # A saw N serving, and gave way
                assert a.handed_over and a.epoch == 1          # A gave way instead of serving again
                assert n.push_if_changed() and not n.fenced
        finally:
            gate.set()
            n._stop.set()
    restore(store, tmp_path / "restored")
    assert "acknowledged by N" in _bodies(tmp_path / "restored", ch, token)


def test_a_requester_does_not_take_the_replica_from_an_owner_that_started_serving(tmp_path):
    # The same window from the other side: M counted the owner W dead (nothing from W landed for the stale
    # window while W restored) and claimed above it. W was only slow: it served and acknowledged a write
    # before M's won marker landed, and M then served without it.
    store = HeldStore(tmp_path / "bucket")
    token = init_board(tmp_path / "w")
    # W polls for hand-over requests every 0.5 s: it takes writes before it answers M's
    w = Replicator(store, tmp_path / "w", interval=3600, poll=0.5, heartbeat=3600)
    assert w.claim() and w.epoch == 1                          # W won the replica, and is slow to serve
    m = Replicator(store, tmp_path / "m", interval=3600, poll=0.02, stop=threading.Event())
    gate, reached = store.hold(_won_marker_of(m, 2))
    results = {}
    t = threading.Thread(target=_take_over, args=(m, results, "m"), kwargs={"stale_after": 1.5})
    t.start()
    try:
        assert reached.wait(10)                                # M judged W dead; its won marker is uploading
        with ThreadedServer(tmp_path / "w", replicator=w) as sw, Client(sw.url, token, max_retries=0) as cw:
            ch = cw.create_channel("x")["id"]
            cw.post(ch, "acknowledged by W")
            gate.set()                                         # M's won marker lands
            wait_for(lambda: "m" in results, timeout=20)       # M gave way, asked W, and got it handed over
    finally:
        gate.set()
        m._stop.set()
        t.join(timeout=30)
    assert results["m"] == "released" and m.epoch == 3
    restore(store, tmp_path / "m")
    assert "acknowledged by W" in _bodies(tmp_path / "m", ch, token)


class DelayedListing(DirStore):
    """The bucket as one instance sees it, with its next listing of claims/ held until the gate opens, as a
    slow Hub call would be. Other instances use the same bucket meanwhile."""

    def __init__(self, root):
        super().__init__(root)
        self.armed = False
        self.reached, self.gate = threading.Event(), threading.Event()

    def list(self, prefix):
        if prefix == "claims/" and self.armed:
            self.armed = False
            self.reached.set()
            assert self.gate.wait(30), "the listing was never let through"
        return super().list(prefix)


def test_an_owner_never_serves_its_board_again_once_a_later_owner_served(tmp_path):
    # Review #1 (Codex) #1: A released the replica to B, and A's next look at the claims was slow. Meanwhile
    # B served, acknowledged a post and handed the replica on to C, which died before it served. A looked
    # only at C, the newest owner, saw no .serving marker, took the replica back and served its board from
    # before B: its next snapshot outranked B's, and B's post was gone. B, whose board has that post, must
    # be the one to take the replica back, and A must never serve again.
    bucket = DirStore(tmp_path / "bucket")
    seen_by_a = DelayedListing(bucket.root)
    token = init_board(tmp_path / "a")
    a = Replicator(seen_by_a, tmp_path / "a", interval=3600, poll=0.02, heartbeat=3600, reclaim_after=0.3)
    time.sleep(0.01)
    b = Replicator(bucket, tmp_path / "b", interval=3600, poll=0.02, heartbeat=3600, reclaim_after=2)
    time.sleep(0.01)
    c = Replicator(bucket, tmp_path / "c", interval=3600, poll=0.02, heartbeat=3600)
    try:
        with ThreadedServer(tmp_path / "a", replicator=a) as sa, Client(sa.url, token, max_retries=0) as ca:
            ch = ca.create_channel("x")["id"]
            ca.post(ch, "before the restarts")
            assert b.take_over(poll=0.02) == "released"
            seen_by_a.armed = True                          # A's next look at the claims is slow
            assert seen_by_a.reached.wait(5)
            restore(bucket, tmp_path / "b")
            with ThreadedServer(tmp_path / "b", replicator=b) as sb, Client(sb.url, token, max_retries=0) as cb:
                cb.post(ch, "acknowledged by B")
                assert c.take_over(poll=0.02) == "released"  # B hands the replica on to C ...
                c.stop(final_push=False)                     # ... which dies before it serves
                seen_by_a.gate.set()                         # A's look at the claims comes back
                wait_for(lambda: not b.handed_over)          # B takes the replica back
                cb.post(ch, "acknowledged by B after C died")
                assert b.push_if_changed()
                claims_of_a = [path for path in bucket.list("claims/") if a.instance in path]
                assert a.handed_over and all(path.startswith("claims/00000001-") for path in claims_of_a), claims_of_a
    finally:
        seen_by_a.gate.set()
        c.stop(final_push=False)
    restore(bucket, tmp_path / "restored")
    assert _bodies(tmp_path / "restored", ch, token) == [
        "before the restarts", "acknowledged by B", "acknowledged by B after C died"]


@pytest.mark.parametrize("later", ["in the same millisecond", "at the same instant, by instance id"])
def test_a_requester_that_started_just_after_the_owner_gets_the_replica(tmp_path, later):
    # Review #1 (Codex) #3: start times were cut to milliseconds before they were compared. A requester that
    # started after the owner within the same millisecond never asked for the replica, and while the owner's
    # heartbeats kept landing it waited for ever. Start times are now compared in full, and the instance id
    # orders two that are equal.
    store = DirStore(tmp_path / "bucket")
    init_board(tmp_path / "a")
    owner = Replicator(store, tmp_path / "a", interval=3600, poll=0.01, heartbeat=0.02)
    with ThreadedServer(tmp_path / "a", replicator=owner):
        if later == "in the same millisecond":
            since, instance = (owner.since + (int(owner.since * 1000) + 1) / 1000) / 2, "requester"
            assert owner.since < since and int(owner.since * 1000) == int(since * 1000)
        else:
            since, instance = owner.since, owner.instance + "0"   # ids are 12 hex digits: this one sorts after
        stop = threading.Event()
        timer = threading.Timer(5, stop.set)
        timer.start()
        try:
            assert request_handover(store, instance, since, stale_after=1, poll=0.01, stop=stop) == "released"
        finally:
            timer.cancel()

