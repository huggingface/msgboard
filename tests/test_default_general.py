from contextlib import closing

import pytest

from msgboard import db, store
from tests.test_members import _db_at


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_new_people_join_general_but_agents_stay_opt_in(env, role):
    general = env.op.create_channel("general")["id"]
    person, _, client = env.human("new-person", role=role)
    assert person["id"] in {p["id"] for p in env.op.members(general)}
    assert next(c for c in client.list_channels() if c["id"] == general)["member"]
    agent, _, _ = env.agent("opt-in-agent")
    assert agent["id"] not in {p["id"] for p in env.op.members(general)}
    explicit, _, _ = env.agent("explicit-agent", channels=["general"])
    assert explicit["id"] in {p["id"] for p in env.op.members(general)}
    posted = env.op.post(general, "@humans hello")
    assert person["id"] in {p["id"] for p in posted["mentions"]}


@pytest.mark.parametrize("general_first", [False, True])
def test_guests_keep_only_explicit_channels_and_can_be_added_to_general(env, general_first):
    shared = env.op.create_channel("shared", private=True)["id"]
    if general_first:
        general = env.op.create_channel("general")["id"]
    invite = env.op.create_invite("human", role="guest", channels=[shared])
    with env.http(None) as http:
        joined = http.post("/join", json={"code": invite["code"], "name": "guest-person"}).json()
    guest = env.client(joined["token"])
    pid = joined["principal"]["id"]
    if not general_first:
        general = env.op.create_channel("general")["id"]
    assert {c["id"] for c in guest.list_channels()} == {shared}
    assert pid not in {p["id"] for p in env.op.members(general)}
    with env.http(guest.token) as http:
        assert http.get(f"/channels/{general}/messages").status_code == 404
    env.op.add_member(general, pid)
    assert pid in {p["id"] for p in env.op.members(general)}
    assert guest.read(general)["messages"] == []
    assert {c["id"] for c in guest.list_channels()} == {shared, general}


def test_leaving_survives_signin_and_restart(env):
    general = env.op.create_channel("general")["id"]
    person, _, client = env.human("leaver")
    assert person["id"] in {p["id"] for p in env.op.members(general)}
    client.remove_member(general, person["id"])
    with env.http(client.token) as http:
        code = http.post(f"/principals/{person['id']}/signin-link").json()["code"]
        signed_in = http.post("/join", json={"code": code}).json()
    assert signed_in["principal"]["id"] == person["id"]
    env.restart()
    assert person["id"] not in {p["id"] for p in env.op.members(general)}
    with env.http(signed_in["token"]) as http:
        channels = http.get("/channels").json()["channels"]
    assert not next(c for c in channels if c["id"] == general)["member"]


@pytest.mark.parametrize("kind", ["absent", "private", "archived"])
def test_default_general_does_not_create_or_join_unavailable_channel(env, kind):
    if kind != "absent":
        channel = env.op.create_channel("general", private=kind == "private")["id"]
        if kind == "archived":
            env.op.update_channel(channel, archived=True)
    person, _, client = env.human("person")
    if kind == "absent":
        assert client.list_channels() == []
    else:
        assert person["id"] not in {p["id"] for p in env.op.members(channel)}


def test_general_created_after_people_adds_them_once(env):
    person, _, client = env.human("early-person")
    agent, _, _ = env.agent("early-agent")
    general = env.op.create_channel("general")["id"]
    assert person["id"] in {p["id"] for p in env.op.members(general)}
    assert agent["id"] not in {p["id"] for p in env.op.members(general)}
    client.remove_member(general, person["id"])
    assert env.op.create_channel("general")["id"] == general
    assert person["id"] not in {p["id"] for p in env.op.members(general)}


@pytest.mark.parametrize("kind", ["public", "private", "archived", "absent"])
def test_v29_adds_existing_people_once_without_touching_other_memberships(tmp_path, kind):
    path = tmp_path / "board.db"
    with closing(_db_at(path, 28)) as conn:
        _, token = store.create_operator(conn, 1)
        op = store.authenticate(conn, token, 1)
        other, _ = store.create_channel(conn, op, "other", None, False, 1)
        if kind != "absent":
            general, _ = store.create_channel(conn, op, "general", None, kind == "private", 1)
            if kind == "archived":
                conn.execute("UPDATE channels SET archived = 1 WHERE id = ?", (general["id"],))
        for handle, identity, disabled in [("person", "user", 0), ("old-member", "user", 0),
                                            ("bot", "agent", 0), ("service", "system", 0),
                                            ("ghost", "user", 1)]:
            conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role, disabled, created_at)
                VALUES ('fixture', ?, ?, 'member', ?, 1)""", (handle, identity, disabled))
        for handle in ["guest-outside", "guest-explicit"]:
            conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role, created_at)
                VALUES ('fixture', ?, 'user', 'guest', 1)""", (handle,))
        ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT * FROM principals")}
        if kind != "absent":
            conn.execute("INSERT INTO channel_members VALUES (?, ?, ?, 123)", (general["id"], ids["old-member"], op.id))
            conn.execute("INSERT INTO channel_members VALUES (?, ?, ?, 123)", (general["id"], ids["guest-explicit"], op.id))
        before_other = [tuple(r) for r in conn.execute("SELECT * FROM channel_members WHERE channel_id = ?", (other["id"],))]
        assert db.migrate(conn, path) == db.SCHEMA_VERSION
        assert [tuple(r) for r in conn.execute("SELECT * FROM channel_members WHERE channel_id = ?", (other["id"],))] == before_other
        if kind == "absent":
            assert not conn.execute("SELECT 1 FROM channels WHERE name = 'general'").fetchone()
            return
        members = {r["principal_id"] for r in conn.execute("SELECT * FROM channel_members WHERE channel_id = ?", (general["id"],))}
        assert (ids["person"] in members) == (kind == "public")
        assert ids["guest-outside"] not in members and ids["guest-explicit"] in members
        assert ids["bot"] not in members and ids["service"] not in members and ids["ghost"] not in members
        assert conn.execute("SELECT added_at FROM channel_members WHERE channel_id = ? AND principal_id = ?", (general["id"], ids["old-member"])).fetchone()[0] == 123
        conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND principal_id = ?", (general["id"], ids["person"]))
        db.migrate(conn, path)
        assert not conn.execute("SELECT 1 FROM channel_members WHERE channel_id = ? AND principal_id = ?", (general["id"], ids["person"])).fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
