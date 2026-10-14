from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

from msgboard.client import BoardError, Client
from msgboard.local import ThreadedServer

from .conftest import free_port


def test_retry_with_same_key_is_deduplicated(env):
    ch = env.op.create_channel("work")
    _, token, _ = env.agent("a1", channels=["work"])
    with env.http(token) as h:
        first = h.post(f"/channels/{ch['id']}/messages", json={"body": "hi"}, headers={"Idempotency-Key": "k1"})
        again = h.post(f"/channels/{ch['id']}/messages", json={"body": "hi"}, headers={"Idempotency-Key": "k1"})
        other = h.post(f"/channels/{ch['id']}/messages", json={"body": "changed"}, headers={"Idempotency-Key": "k1"})
        missing = h.post(f"/channels/{ch['id']}/messages", json={"body": "x"})
    assert first.status_code == 201
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert again.json()["id"] == first.json()["id"] and again.json()["seq"] == first.json()["seq"]
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    assert missing.status_code == 400
    assert len(env.op.read(ch["id"])["messages"]) == 1


class DropFirstResponse(httpx.HTTPTransport):
    """Deliver the request, then pretend the response was lost."""

    def __init__(self):
        super().__init__()
        self.dropped = 0

    def handle_request(self, request):
        response = super().handle_request(request)
        if request.method == "POST" and self.dropped == 0:
            self.dropped += 1
            response.read()
            raise httpx.ReadTimeout("simulated lost response", request=request)
        return response


def test_sdk_retry_after_lost_response_posts_once(env):
    ch = env.op.create_channel("work")
    _, token, _ = env.agent("a1", channels=["work"])
    transport = DropFirstResponse()
    with Client(env.url, token, transport=transport, backoff=0.01) as c:
        msg = c.post(ch["id"], "exactly once")
    assert transport.dropped == 1
    history = env.op.read(ch["id"])["messages"]
    assert [m["body"] for m in history] == ["exactly once"]
    assert history[0]["id"] == msg["id"]


def test_concurrent_posts_get_contiguous_ordered_sequences(env):
    ch = env.op.create_channel("busy")
    writers = [env.agent(f"w{i}", channels=["busy"])[2] for i in range(4)]
    acked: dict[int, list[int]] = {i: [] for i in range(4)}
    errors = []

    def run(i: int, client: Client):
        try:
            for n in range(40):
                acked[i].append(client.post(ch["id"], f"{i}-{n}")["seq"])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    all_seqs = sorted(s for seqs in acked.values() for s in seqs)
    assert all_seqs == list(range(1, 161))
    for seqs in acked.values():
        assert seqs == sorted(seqs)  # each writer's acknowledged posts keep their order
    history, cursor = [], 0
    while True:
        page = env.op.read(ch["id"], after=cursor, limit=50)
        history += page["messages"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [m["seq"] for m in history] == list(range(1, 161))


def test_cursor_pagination(env):
    ch = env.op.create_channel("pages")
    for i in range(25):
        env.op.post(ch["id"], f"m{i}")
    p1 = env.op.read(ch["id"], after=0, limit=10)
    assert [m["seq"] for m in p1["messages"]] == list(range(1, 11)) and p1["has_more"]
    p3 = env.op.read(ch["id"], after=20, limit=10)
    assert p3["next_cursor"] == 25 and not p3["has_more"]
    empty = env.op.read(ch["id"], after=25, limit=10)
    assert empty["messages"] == [] and empty["next_cursor"] == 25
    with env.http(env.operator_token) as h:
        assert h.get(f"/channels/{ch['id']}/messages", params={"limit": 501}).status_code == 422
        assert h.get(f"/channels/{ch['id']}/messages", params={"wait": 31}).status_code == 422


def test_long_poll_wakes_on_post_and_times_out(env):
    ch = env.op.create_channel("poll")
    _, _, reader = env.human("reader", role="viewer")
    result = {}

    def poll():
        start = time.monotonic()
        result["page"] = reader.read(ch["id"], after=0, wait=10)
        result["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.3)
    env.op.post(ch["id"], "wake up")
    t.join(timeout=10)
    assert [m["body"] for m in result["page"]["messages"]] == ["wake up"]
    assert result["elapsed"] < 5

    start = time.monotonic()
    page = reader.read(ch["id"], after=1, wait=0.5)
    assert page["messages"] == [] and page["next_cursor"] == 1
    assert 0.4 < time.monotonic() - start < 3


def test_replies_stay_in_channel_and_archive_blocks_posts(env):
    a = env.op.create_channel("a")
    b = env.op.create_channel("b")
    parent = env.op.post(a["id"], "parent")
    reply = env.op.post(a["id"], "child", reply_to=parent["id"])
    assert reply["reply_to"] == parent["id"]
    with pytest.raises(BoardError) as exc:
        env.op.post(b["id"], "cross", reply_to=parent["id"])
    assert exc.value.status == 400
    with env.http(env.operator_token) as h:
        assert h.post(f"/channels/{a['id']}/archive").status_code == 200
    with pytest.raises(BoardError) as exc:
        env.op.post(a["id"], "too late")
    assert exc.value.code == "channel_archived"
    assert len(env.op.read(a["id"])["messages"]) == 2  # history kept


def test_message_size_limits(env):
    ch = env.op.create_channel("big")
    with pytest.raises(BoardError) as exc:
        env.op.post(ch["id"], "x" * (64 * 1024 + 1))
    assert exc.value.status == 413


def test_history_survives_restart(env):
    ch = env.op.create_channel("keep")
    sent = [env.op.post(ch["id"], f"m{i}", request_id=f"r{i}") for i in range(5)]
    env.restart()
    history = env.op.read(ch["id"])["messages"]
    assert [(m["id"], m["seq"], m["body"]) for m in history] == [(m["id"], m["seq"], m["body"]) for m in sent]
    # Request IDs are durable too: a retry after the restart is still a replay.
    assert env.op.post(ch["id"], "m0", request_id="r0")["id"] == sent[0]["id"]
    assert env.op.post(ch["id"], "after restart")["seq"] == 6


def test_acknowledged_posts_survive_sigkill(tmp_path):
    from .conftest import init_board

    data_dir = tmp_path / "board"
    token = init_board(data_dir)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "msgboard", "serve", "--data-dir", str(data_dir), "--port", str(port),
         "--log-level", "warning"],
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        while True:
            try:
                httpx.get(url + "/v1/health", timeout=1)
                break
            except httpx.TransportError:
                assert time.time() < deadline and proc.poll() is None, "server did not start"
                time.sleep(0.1)
        with Client(url, token) as c:
            ch = c.create_channel("crash")
            acked = [c.post(ch["id"], f"m{i}")["seq"] for i in range(50)]
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
    with ThreadedServer(data_dir) as server, Client(server.url, token) as c:
        seqs = [m["seq"] for m in c.read(ch["id"], limit=100)["messages"]]
    assert seqs == acked == list(range(1, 51))


def test_clean_shutdown_with_open_long_poll(env):
    ch = env.op.create_channel("idle")
    t = threading.Thread(target=lambda: env.client(env.operator_token, max_retries=0).read(ch["id"], wait=25))
    t.start()
    time.sleep(0.3)
    start = time.monotonic()
    env.server.stop()
    assert time.monotonic() - start < 5
    t.join(timeout=5)
    env.server = ThreadedServer(env.data_dir).start()  # lock released, restart works
    env.url = env.server.url


def test_messages_have_no_metadata(env):
    ch = env.op.create_channel("plain")
    with env.http(env.operator_token) as h:
        r = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "m1"},
                   json={"body": "hi", "metadata": {"source": "x"}})
        assert r.status_code == 400 and "metadata" in r.json()["error"]["message"]
        ok = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "m2"}, json={"body": "hi"})
    assert ok.status_code == 201 and "metadata" not in ok.json()


def test_retry_of_message_stored_before_metadata_removal_still_replays(tmp_path):
    """Messages stored under schema v3 hashed {.., "metadata": null}; a retry after upgrading must replay."""
    import hashlib
    import json

    from msgboard import db as dbmod
    from msgboard import store

    path = tmp_path / "board.db"
    conn = dbmod.connect(path)
    for version, script in enumerate(dbmod.MIGRATIONS[:3]):
        conn.execute("BEGIN IMMEDIATE")
        for statement in dbmod._split(script):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={version + 1}")
        conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")
    pid, token = store.create_operator(conn, time.time())
    conn.execute("INSERT INTO channels (name, created_by, created_at, next_seq) VALUES ('c', ?, 0, 2)", (pid,))
    old = {"channel_id": 1, "body": "before", "metadata": None, "reply_to": None, "attachments": []}
    old_hash = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
                    VALUES (1, 1, ?, 'before', 'old-key', ?, 0)""", (pid, old_hash))
    conn.execute("COMMIT")
    assert dbmod.migrate(conn, path) == dbmod.SCHEMA_VERSION
    principal = store.authenticate(conn, token, time.time())
    normalized, digest = store.normalize_payload(1, {"body": "before"})
    conn.execute("BEGIN IMMEDIATE")
    message, created = store.post_message(conn, principal, 1, "old-key", normalized, digest, time.time())
    conn.execute("COMMIT")
    assert not created and message["body"] == "before" and "metadata" not in message


def test_admins_delete_messages_with_their_threads(env):
    from msgboard.client import BoardError
    import pytest
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    root = ana.post(gen["id"], "placeholder root needle")
    reply = ana.post(gen["id"], "placeholder reply", reply_to=root["id"])
    keep = ana.post(gen["id"], "a real message")
    ana.react(gen["id"], root["id"], "👍")
    with pytest.raises(BoardError) as e:
        ana.delete_message(gen["id"], keep["id"])
    assert e.value.status == 403
    out = env.op.delete_message(gen["id"], root["id"])
    assert sorted(out["deleted"]) == sorted([root["id"], reply["id"]])
    assert [m["body"] for m in ana.read(gen["id"])["messages"]] == ["a real message"]
    assert ana.search("needle")["messages"] == []
    single = env.op.delete_message(gen["id"], keep["id"])
    assert single["deleted"] == [keep["id"]]
