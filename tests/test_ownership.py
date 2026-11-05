"""Agents are owned by people: an agent that another agent invited belongs to the person at the top of the chain."""

import os
import tempfile

import pytest

from msgboard import db
from msgboard.client import BoardError


def profile(env, client, pid):
    return env.http(client.token).get(f"/principals/{pid}/profile").json()


def test_agent_invited_by_an_agent_is_owned_by_its_person(env):
    me = env.op.me()
    mgr_ident, _, mgr = env.agent("mgr", role="admin")                 # an admin agent, owned by the operator
    assert mgr_ident["owner"]["id"] == me["id"]
    worker_ident, _, _ = env.agent("worker", owner=mgr)                 # invited by the agent ...
    assert worker_ident["owner"]["id"] == me["id"]                      # ... but owned by the person
    p = profile(env, env.op, worker_ident["id"])
    assert p["owner"]["id"] == me["id"]
    assert p["created_by"]["id"] == mgr_ident["id"]                     # the inviting agent stays on record
    assert worker_ident["id"] in [a["id"] for a in profile(env, env.op, me["id"])["agents"]]
    assert worker_ident["id"] not in [a["id"] for a in profile(env, env.op, mgr_ident["id"]).get("agents") or []]


def test_owner_check_uses_the_person_at_the_top(env):
    _, _, ana = env.human("ana", role="admin")
    ana_id = ana.me()["id"]
    _, _, mgr = env.agent("ana-mgr", owner=ana, role="admin")
    worker_ident, _, worker = env.agent("ana-worker", owner=mgr, role="admin")   # an admin agent needs an admin owner
    assert worker_ident["owner"]["id"] == ana_id
    worker.me()
    env.op.update_identity(ana_id, role="member")                       # ana is no longer an admin
    with pytest.raises(BoardError) as e:
        worker.me()
    assert e.value.status == 401


def test_long_chains_reach_the_person_and_loops_are_refused():
    from msgboard import store
    from msgboard.errors import ApiError
    path = os.path.join(tempfile.mkdtemp(), "board.db")
    conn = db.connect(path)
    db.migrate(conn, path)
    add = lambda name, kind, owner: conn.execute(
        "INSERT INTO principals (issuer, external_id, identity_type, role, created_at, owner_id) VALUES ('t', ?, ?, 'member', 0, ?)",
        (name, kind, owner)).lastrowid
    row = lambda pid: conn.execute("SELECT * FROM principals WHERE id = ?", (pid,)).fetchone()
    person = prev = add("person", "user", None)
    for i in range(25):
        prev = add(f"deep-{i}", "agent", prev)
    assert store._human_owner(conn, row(prev))["id"] == person
    x = add("loop-x", "agent", None); y = add("loop-y", "agent", x)
    conn.execute("UPDATE principals SET owner_id = ? WHERE id = ?", (y, x))
    with pytest.raises(ApiError):
        store._human_owner(conn, row(x))
    with pytest.raises(ApiError):
        store._human_owner(conn, row(add("top-agent", "agent", None)))
    conn.close()


def test_migration_moves_agent_owned_agents_to_their_person():
    path = os.path.join(tempfile.mkdtemp(), "board.db")
    conn = db.connect(path)
    db.migrate(conn, path)
    add = lambda name, kind, owner: conn.execute(
        "INSERT INTO principals (issuer, external_id, identity_type, role, created_at, owner_id) VALUES ('t', ?, ?, 'member', 0, ?)",
        (name, kind, owner)).lastrowid
    person = add("person", "user", None)
    a = add("agent-a", "agent", person)
    b = add("agent-b", "agent", a)          # owned by an agent
    c = add("agent-c", "agent", b)          # owned by an agent that is owned by an agent
    lone = add("lone", "agent", None)       # no owner at all: left alone
    prev = person                           # a 25-level chain still reaches its person
    for i in range(25):
        prev = add(f"deep-{i}", "agent", prev)
    deep = prev
    x = add("loop-x", "agent", None)        # a loop with no person: left as it is
    y = add("loop-y", "agent", x)
    conn.execute("UPDATE principals SET owner_id = ? WHERE id = ?", (y, x))
    orphan_top = add("orphan-top", "agent", None)
    orphan = add("orphan", "agent", orphan_top)   # ends at an agent: not re-parented to it
    conn.execute("PRAGMA user_version=25")   # replay the v26 ownership migration
    conn.close()
    conn = db.connect(path)
    assert db.migrate(conn, path) == db.SCHEMA_VERSION
    owner = dict(conn.execute("SELECT id, owner_id FROM principals").fetchall())
    assert (owner[a], owner[b], owner[c], owner[lone]) == (person, person, person, None)
    assert owner[deep] == person
    assert (owner[x], owner[y]) == (y, x) and owner[orphan] == orphan_top
    conn.close()
