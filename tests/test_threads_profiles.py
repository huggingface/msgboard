from __future__ import annotations

import time

import pytest

from msgboard import db as dbmod
from msgboard import store
from msgboard.client import BoardError


def test_threads_summaries_and_filters(env):
    ch = env.op.create_channel("t")
    _, _, alice = env.agent("alice", channels=["t"], display_name="Alice")
    _, _, bob = env.agent("bob", channels=["t"])
    root = alice.post(ch["id"], "root")
    r1 = bob.post(ch["id"], "first reply", reply_to=root["id"])
    r2 = alice.post(ch["id"], "reply to the reply", reply_to=r1["id"])
    alone = bob.post(ch["id"], "unrelated")
    assert root["thread_id"] is None and r1["thread_id"] == root["id"] and r2["thread_id"] == root["id"]

    with env.http(env.operator_token) as h:
        top = h.get(f"/channels/{ch['id']}/messages", params={"top_level": "true"}).json()
        assert [m["body"] for m in top["messages"]] == ["root", "unrelated"]
        summary = top["messages"][0]["replies"]
        assert summary["count"] == 2 and summary["last_at"] == r2["created_at"]
        assert [p["external_id"] for p in summary["participants"]] == ["alice", "bob"]  # most recent first
        assert "replies" not in top["messages"][1]
        thread = h.get(f"/channels/{ch['id']}/messages", params={"thread": root["id"]}).json()
        assert [m["body"] for m in thread["messages"]] == ["root", "first reply", "reply to the reply"]
        latest = h.get(f"/channels/{ch['id']}/messages", params={"before": alone["seq"] + 1, "limit": 2}).json()
        assert [m["body"] for m in latest["messages"]] == ["reply to the reply", "unrelated"] and latest["has_more"]
        older = h.get(f"/channels/{ch['id']}/messages", params={"before": latest["messages"][0]["seq"]}).json()
        assert [m["body"] for m in older["messages"]] == ["root", "first reply"] and not older["has_more"]
        assert h.get(f"/channels/{ch['id']}/messages", params={"top_level": "true", "wait": 1}).status_code == 400


def test_profile_counts_only_readable_channels(env):
    a = env.op.create_channel("a")
    b = env.op.create_channel("b")
    ident, _, writer = env.agent("writer", channels=["a", "b"], display_name="Writer")
    _, _, a_only = env.agent("reader-a", role="guest", channels=["a"])
    _, _, stranger = env.agent("stranger", role="guest", channels=["b"])
    _, _, member = env.human("member")
    for i in range(3):
        writer.post(a["id"], f"a{i}")
    writer.post(b["id"], "b0")

    full = member._request("GET", f"/principals/{ident['id']}/profile").json()
    assert full["messages"]["count"] == 4
    assert [(c["name"], c["count"]) for c in full["per_channel"]] == [("a", 3), ("b", 1)]
    assert full["created_by"]["external_id"] == "operator"
    assert len(full["daily"]) == 30 and full["daily"][-1]["count"] == 4
    assert full["messages"]["first_at"] <= full["messages"]["last_at"]

    partial = a_only._request("GET", f"/principals/{ident['id']}/profile").json()
    assert partial["messages"]["count"] == 3 and [c["name"] for c in partial["per_channel"]] == ["a"]

    me = a_only.me()
    with pytest.raises(BoardError) as exc:                       # never wrote where `stranger` can read
        stranger._request("GET", f"/principals/{me['id']}/profile")
    assert exc.value.status == 404
    assert stranger._request("GET", f"/principals/{stranger.me()['id']}/profile").json()["messages"]["count"] == 0


def test_profile_says_who_invited_and_who_owns(env):
    invite = env.op.create_invite("human", role="member", label="spring cohort")
    from msgboard.client import Client
    ana = Client.join(env.url, invite["code"], "ana", display_name="Ana")
    helper, _, _ = env.agent("helper", owner=ana)
    prof = env.op._request("GET", f"/principals/{ana.identity['id']}/profile").json()
    assert prof["created_by"]["external_id"] == "operator" and prof["invite"]["label"] == "spring cohort"
    assert [a["external_id"] for a in prof["agents"]] == ["helper"]
    agent_prof = ana._request("GET", f"/principals/{helper['id']}/profile").json()
    assert agent_prof["owner"]["external_id"] == "ana" and agent_prof["created_by"]["external_id"] == "ana"
    ana.close()


def test_v3_migration_backfills_threads_and_creators(tmp_path):
    path = tmp_path / "board.db"
    conn = dbmod.connect(path)
    for version, script in enumerate(dbmod.MIGRATIONS[:2]):
        conn.execute("BEGIN IMMEDIATE")
        for statement in dbmod._split(script):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={version + 1}")
        conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")
    pid = conn.execute("INSERT INTO principals (issuer, external_id, identity_type, role, created_at) "
                       "VALUES ('local', 'operator', 'user', 'admin', 0)").lastrowid
    conn.execute("INSERT INTO principals (issuer, external_id, identity_type, role, created_at) "
                 "VALUES ('admin', 'w', 'agent', 'worker', 0)")
    store.audit(conn, pid, "agent.create", "principal:2", None, time.time())
    conn.execute("INSERT INTO channels (name, created_by, created_at) VALUES ('c', 1, 0)")
    for i, reply_to in enumerate([None, 1, 2, None, 4], start=1):
        conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, reply_to, request_id, payload_hash,
                        created_at) VALUES (1, ?, 1, 'x', ?, ?, 'h', 0)""", (i, reply_to, f"r{i}"))
    conn.execute("COMMIT")
    assert dbmod.migrate(conn, path) == dbmod.SCHEMA_VERSION
    assert [r[0] for r in conn.execute("SELECT thread_id FROM messages ORDER BY id")] == [None, 1, 1, None, 4]
    assert conn.execute("SELECT created_by FROM principals WHERE id = 2").fetchone()[0] == pid
