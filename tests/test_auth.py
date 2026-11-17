from __future__ import annotations

import threading
import time

import pytest

from msgboard.client import BoardError, Client


def status(fn, *args, **kwargs) -> int:
    try:
        fn(*args, **kwargs)
    except BoardError as exc:
        return exc.status
    return 200


# ------------------------------------------------------------------ tokens


def test_bad_tokens_are_rejected(env):
    for token in (None, "nonsense", "fbt_0123456789abcdef.AAAA", env.operator_token[:-2] + "xx"):
        with env.http(token) as h:
            assert h.get("/me").status_code == 401
    with env.http(None) as h:
        assert h.get("/health").status_code == 200


def test_sender_identity_comes_from_token(env):
    ch = env.op.create_channel("c")
    ident, token, _ = env.agent("honest")
    with env.http(token) as h:
        r = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "k"},
                   json={"body": "hi", "sender": {"id": 1, "identity_type": "user"}})
        assert r.status_code == 400  # unknown fields rejected; no way to claim a sender
        r = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "k"}, json={"body": "hi"})
    sender = r.json()["sender"]
    assert (sender["id"], sender["external_id"], sender["identity_type"]) == (ident["id"], "honest", "agent")


def test_tokens_do_not_expire_and_people_get_one_per_device(env):
    me, token, alice = env.human("alice")
    assert env.op.me()["token"]["id"] and alice.me()["id"] == me["id"]
    link = alice._request("POST", f"/principals/{me['id']}/signin-link").json()
    second = Client.join(env.url, link["code"], None)
    assert second.me()["id"] == me["id"]
    assert alice.me()["id"] == me["id"]                       # the first device still works
    with pytest.raises(BoardError) as exc:                     # sign-in links are single use
        Client.join(env.url, link["code"], None)
    assert exc.value.status == 410
    second.revoke_token(second.me()["token"]["id"])            # signing out one device...
    assert status(second.me) == 401 and alice.me()["id"] == me["id"]   # ...leaves the other
    second.close()


def test_agent_tokens_rotate_and_revoke(env):
    agent, _, client = env.agent("a")
    fresh = env.op.rotate_token(agent["id"])                   # the owner makes a new token
    assert status(client.me) == 401
    rotated = env.client(fresh["token"])
    assert rotated.me()["id"] == agent["id"]
    assert status(rotated.rotate_token, agent["id"]) == 404    # agents cannot mint their own
    env.op.revoke_token(fresh["token_id"])
    assert status(rotated.me) == 401


# ------------------------------------------------------------------ roles


def test_people_roles(env):
    public = env.op.create_channel("public")
    _, _, member = env.human("mem")
    _, _, viewer = env.human("view", role="viewer")
    assert {c["name"] for c in viewer.list_channels()} == {"public"}
    assert status(viewer.post, public["id"], "x") == 403        # viewers are read-only
    assert status(viewer.create_channel, "v") == 403
    assert status(viewer.create_invite, "agent") == 403
    member.post(public["id"], "hello")
    assert member.create_channel("by-member")["kind"] == "public"
    assert status(member.create_invite, "human") == 403         # only admins invite people
    assert member.create_invite("agent")["code"]                 # members invite agents
    assert {c["name"] for c in viewer.list_channels()} == {"public", "by-member"}  # new public channels too


def test_admin_sees_everything_others_see_their_part(env):
    _, _, alice = env.human("alice")
    _, _, bob = env.human("bob")
    secret = alice.create_channel("secret", private=True)
    alice.post(secret["id"], "just us")
    assert status(bob.read, secret["id"]) == 404                 # private: invisible, not just forbidden
    assert [m["body"] for m in env.op.read(secret["id"])["messages"]] == ["just us"]  # admins see all
    alice.add_member(secret["id"], bob.me()["id"])
    assert [m["body"] for m in bob.read(secret["id"])["messages"]] == ["just us"]


def test_agents_follow_their_owner(env):
    env.op.create_channel("open")
    _, _, owner = env.human("owner")
    _, _, member_agent = env.agent("helper", owner=owner)
    _, _, guest_agent = env.agent("narrow", owner=owner, role="guest")
    _, _, creator = env.agent("maker", owner=owner, can_create_channels=True)
    assert [c["name"] for c in member_agent.list_channels()] == ["open"]   # like its owner
    assert guest_agent.list_channels() == []                                # only channels it is added to
    assert status(member_agent.create_channel, "nope") == 403
    assert status(guest_agent.create_channel, "nope") == 403
    creator.create_channel("agent-made")
    assert status(member_agent.create_invite, "agent") == 403               # agents do not invite
    assert member_agent.me()["owner"]["external_id"] == "owner"
    with pytest.raises(BoardError):                                         # agents are never admins
        owner.create_invite("agent", role="admin")

    # When the owner is demoted to viewer or disabled, their agents stop working.
    owner_id = owner.me()["id"]
    env.op.update_identity(owner_id, role="viewer")
    assert status(member_agent.me) == 401
    env.op.update_identity(owner_id, role="member")
    assert member_agent.me()["role"] == "member"
    env.op.update_identity(owner_id, disabled=True)
    assert status(member_agent.me) == 401 and status(owner.me) == 401


def test_owners_manage_their_agents_only(env):
    _, _, alice = env.human("alice")
    _, _, bob = env.human("bob")
    a_agent, _, _ = env.agent("a1", owner=alice)
    assert alice.update_identity(a_agent["id"], role="guest", display_name="Helper")["role"] == "guest"
    assert status(bob.update_identity, a_agent["id"], disabled=True) == 403
    assert status(bob.rotate_token, a_agent["id"]) == 404
    assert status(alice.update_identity, bob.me()["id"], role="viewer") == 403   # people: admins only
    alice.update_identity(a_agent["id"], disabled=True)
    with pytest.raises(BoardError) as exc:     # a removed agent cannot simply join again
        Client.join(env.url, alice.create_invite("agent")["code"], "a1")
    assert exc.value.code == "identity_disabled"


def test_guest_channels_and_membership(env):
    a = env.op.create_channel("a")
    b = env.op.create_channel("b")
    secret = b["id"]
    env.op.post(secret, "hidden")
    _, _, owner = env.human("owner")
    guest, _, gclient = env.agent("g", owner=owner, role="guest", channels=["a"])
    assert [c["name"] for c in gclient.list_channels()] == ["a"]
    assert status(gclient.read, secret) == 404
    gclient.post(a["id"], "a guest may post where it was added")
    owner.remove_member(a["id"], guest["id"])                    # the person who added it can remove it
    assert gclient.list_channels() == []


def test_last_admin_cannot_be_removed(env):
    me = env.op.me()
    assert status(env.op.update_identity, me["id"], role="member") == 409
    _, _, other = env.human("second", role="admin")
    env.op.update_identity(me["id"], role="member")              # fine once someone else is admin
    assert other.me()["role"] == "admin"


# ------------------------------------------------------------------ invites


def test_invites_limits_and_listing(env):
    once = env.op.create_invite("human", role="viewer", max_uses=1, label="for Dana")
    dana = Client.join(env.url, once["code"], "dana", display_name="Dana")
    assert dana.me()["role"] == "viewer"
    with pytest.raises(BoardError) as exc:
        Client.join(env.url, once["code"], "eve")
    assert exc.value.status == 410 and "used up" in exc.value.message
    expired = env.op.create_invite("human", expires_in_seconds=0.5)
    time.sleep(0.6)
    assert status(Client.join, env.url, expired["code"], "late") == 410
    unlimited = env.op.create_invite("agent", label="launcher")
    for i in range(3):
        Client.join(env.url, unlimited["code"], f"bot{i}").close()
    env.op.revoke_invite(unlimited["id"])
    assert status(Client.join, env.url, unlimited["code"], "bot9") == 410
    listed = {i["label"]: i for i in env.op.invites()}
    assert listed["for Dana"]["status"] == "used up" and [p["external_id"] for p in listed["for Dana"]["joined"]] == ["dana"]
    assert listed["launcher"]["status"] == "revoked" and len(listed["launcher"]["joined"]) == 3
    assert status(Client.join, env.url, "not-a-real-code", "x") == 404
    with env.http(None) as h:
        assert h.get(f"/join/{once['code']}").json()["status"] == "used up"   # the join page can explain
    with pytest.raises(BoardError) as exc:
        Client.join(env.url, env.op.create_invite("human")["code"], "dana")    # names are unique
    assert exc.value.code == "name_taken"
    dana.close()


def test_agent_rejoin_returns_same_identity(env):
    invite = env.op.create_invite("agent", max_uses=2)
    first = Client.join(env.url, invite["code"], "worker-1")
    again = Client.join(env.url, invite["code"], "worker-1")   # same program restarting
    assert again.identity["id"] == first.identity["id"]
    assert status(first.me) == 401 and again.me()["external_id"] == "worker-1"
    assert [i for i in env.op.invites() if i["id"] == invite["id"]][0]["uses"] == 1   # not a new use
    Client.join(env.url, invite["code"], "worker-2").close()
    assert status(Client.join, env.url, invite["code"], "worker-1") == 410   # used up: no re-join either
    first.close(), again.close()


def test_members_see_only_their_invites(env):
    _, _, alice = env.human("alice")
    _, _, bob = env.human("bob")
    mine = alice.create_invite("agent", label="alice's")
    bob.create_invite("agent", label="bob's")
    assert [i["label"] for i in alice.invites()] == ["alice's"]
    assert status(bob.revoke_invite, mine["id"]) == 404
    assert {i["label"] for i in env.op.invites()} >= {"alice's", "bob's"}


# ------------------------------------------------------------------ DMs and private channels


def test_direct_messages(env):
    _, _, alice = env.human("alice")
    bob_ident, _, bob = env.human("bob")
    _, _, carol = env.human("carol")
    agent, _, aclient = env.agent("helper", owner=carol)
    dm = alice.open_dm(bob_ident["id"])
    assert alice.open_dm(bob_ident["id"])["id"] == dm["id"]   # one DM per pair
    alice.post(dm["id"], "psst")
    assert [m["body"] for m in bob.read(dm["id"])["messages"]] == ["psst"]
    assert status(carol.read, dm["id"]) == 404                  # others cannot see it
    assert env.op.read(dm["id"])["messages"]                    # admins can
    agent_dm = aclient.open_dm(bob_ident["id"])
    aclient.post(agent_dm["id"], "from the agent")
    assert carol.read(agent_dm["id"])["messages"]               # the agent's owner can read its DMs
    assert status(carol.post, agent_dm["id"], "x") == 403       # but not write in them
    _, _, viewer = env.human("v", role="viewer")
    assert status(viewer.open_dm, bob_ident["id"]) == 403


def test_private_channels_can_be_switched_off(env):
    _, _, alice = env.human("alice")
    assert env.op.settings()["allow_private_channels"] is True
    assert status(alice.update_settings, allow_private_channels=False) == 403
    env.op.update_settings(allow_private_channels=False)
    assert status(alice.create_channel, "p", private=True) == 403
    alice.create_channel("still-public")


# ------------------------------------------------------------------ live checks


def test_revocation_applies_to_held_long_poll(env):
    ch = env.op.create_channel("c")
    _, token, reader = env.agent("r")
    token_id = reader.me()["token"]["id"]
    result = {}

    def poll():
        with env.http(token) as h:
            result["resp"] = h.get(f"/channels/{ch['id']}/messages", params={"wait": 10})

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.3)
    env.op.revoke_token(token_id)
    env.op.post(ch["id"], "you should not see this")
    t.join(timeout=10)
    assert result["resp"].status_code == 401


def test_admin_only_operations(env):
    _, token, _ = env.human("m")
    with env.http(token) as h:
        assert h.get("/metrics").status_code == 403
    with env.http(env.operator_token) as h:
        m = h.get("/metrics").json()
    assert {"open_polls", "pending_writes", "db_bytes", "wal_bytes", "disk_free_bytes"} <= set(m)


def test_invite_validation(env):
    with env.http(env.operator_token) as h:
        for bad in [
            {"kind": "robot"}, {"kind": "human", "role": "reader"},
            {"kind": "agent", "role": "viewer"}, {"kind": "agent", "identity_type": "user"},
            {"kind": "agent", "role": "guest", "can_create_channels": True},
            {"kind": "agent", "max_uses": 0},
        ]:
            assert h.post("/invites", json=bad).status_code == 400, bad


def test_reader_and_admin_agents(env):
    _, _, alice = env.human("alice")
    _, _, bob = env.human("bob")
    secret = alice.create_channel("secret", private=True)
    alice.post(secret["id"], "private note")
    dm = alice.open_dm(bob.me()["id"])
    alice.post(dm["id"], "just between us")

    # only admins can create them
    assert status(alice.create_invite, "agent", role="reader") == 403
    assert status(alice.create_invite, "agent", role="admin") == 403
    reader_ident, _, reader = env.agent("archiver", role="reader")
    admin_ident, _, robot = env.agent("ops-bot", role="admin")

    # a reader sees everything (private channels, DMs) but never posts
    names = {c["name"] for c in reader.list_channels()}
    assert "secret" in names and dm["name"] in names
    assert [m["body"] for m in reader.read(secret["id"])["messages"]] == ["private note"]
    assert [m["body"] for m in reader.read(dm["id"])["messages"]] == ["just between us"]
    assert status(reader.post, secret["id"], "x") == 403
    assert status(reader.open_dm, bob.me()["id"]) == 403
    assert status(reader.create_channel, "nope") == 403

    # an admin agent has admin rights
    assert robot.me()["permissions"]["manage"] is True
    assert robot.read(dm["id"])["messages"]
    people_invite = robot.create_invite("human", role="member")
    assert people_invite["code"]
    assert robot.update_identity(bob.me()["id"], role="viewer")["role"] == "viewer"

    # a member cannot turn its own agent into an admin or reader
    mine, _, _ = env.agent("mine", owner=alice)
    assert status(alice.update_identity, mine["id"], role="admin") == 403
    assert status(alice.update_identity, mine["id"], role="reader") == 403

    # both stop working when their owner is no longer an admin; admin agents do not count as "last admin"
    me = env.op.me()
    assert status(env.op.update_identity, me["id"], role="member") == 409
    _, _, other_admin = env.human("second-admin", role="admin")
    env.op.update_identity(me["id"], role="member")
    assert status(reader.me) == 401 and status(robot.me) == 401
    other_admin.update_identity(me["id"], role="admin")
    assert reader.me()["role"] == "reader"


def test_invite_channels(env):
    pub = env.op.create_channel("pub")
    other = env.op.create_channel("other")
    _, _, alice = env.human("alice")
    secret = alice.create_channel("secret", private=True)
    invite = alice.create_invite("agent", role="guest", channels=["pub", secret["id"]], label="runners")
    assert [c["name"] for c in invite["channels"]] == ["pub", "secret"]
    with env.http(None) as h:
        assert h.get(f"/join/{invite['code']}").json()["channels"] == ["pub", "secret"]
    guest = Client.join(env.url, invite["code"], "runner-1")
    assert {c["name"] for c in guest.list_channels()} == {"pub", "secret"}   # exactly the invite's channels
    assert status(guest.read, other["id"]) == 404
    guest.post(secret["id"], "in the private channel from the start")
    # a member agent can be put into a private channel the same way
    member = Client.join(env.url, alice.create_invite("agent", channels=["secret"])["code"], "helper")
    assert "secret" in {c["name"] for c in member.list_channels()}
    # you cannot hand out channels you could not add someone to yourself
    _, _, bob = env.human("bob")
    assert status(bob.create_invite, "agent", role="guest", channels=["secret"]) == 404
    dm = alice.open_dm(bob.me()["id"])
    assert status(alice.create_invite, "agent", channels=[dm["id"]]) == 400
    assert status(alice.create_invite, "agent", channels=["no-such-channel"]) == 404
    guest.close(), member.close()


def test_named_agent_invite(env):
    _, _, ana = env.human("ana")
    inv = ana.create_invite("agent", agent_name="Eval Runner 2")
    assert (inv["agent_name"], inv["agent_display"]) == ("eval-runner-2", "Eval Runner 2")
    info = env.http(None).get(f"/join/{inv['code']}").json()
    assert info["agent_name"] == "eval-runner-2"
    first = Client.join(env.url, inv["code"])                  # no name needed
    assert (first.identity["external_id"], first.identity["display_name"]) == ("eval-runner-2", "Eval Runner 2")
    again = Client.join(env.url, inv["code"], "eval-runner-2")  # same agent, new token
    assert again.identity["id"] == first.identity["id"]
    assert status(Client.join, env.url, inv["code"], "someone-else") == 400


def test_agents_register_their_runtime(env):
    _, _, ana = env.human("ana")
    bot = Client.join(env.url, ana.create_invite("agent")["code"], "runner")
    out = bot.set_runtime("pi", "hf:Qwen/Qwen3.8-27B", tools=["bash", "read", "bash"], host="hf-job cpu-basic")
    assert out["runtime"] == {"harness": "pi", "model": "hf:Qwen/Qwen3.8-27B", "tools": ["bash", "read"], "host": "hf-job cpu-basic"}
    seen = [p for p in ana.people() if p["external_id"] == "runner"][0]
    assert seen["runtime"]["harness"] == "pi" and seen["runtime_at"]
    assert status(bot.update_identity, bot.identity["id"], runtime={"secret": "x"}) == 400
    assert status(ana.update_identity, ana.identity["id"], runtime={"harness": "me"}) == 403   # people have no runtime
    assert ana.update_identity(bot.identity["id"], runtime=None)["runtime"] is None              # the owner can clear it
    again = Client(env.url, bot.token)                                                           # made from a token, not join()
    assert again.set_runtime("codex")["runtime"] == {"harness": "codex"}
