from __future__ import annotations

import pytest

from msgboard.client import BoardError, Client


def _err(fn):
    with pytest.raises(BoardError) as e:
        fn()
    return e.value.status


def test_guest_person_sees_only_their_channels_and_their_agents_inherit_them(env):
    a = env.op.create_channel("alpha"); b = env.op.create_channel("beta")
    p = env.op.create_channel("pvt", private=True)
    inv = env.op.create_invite("human", role="guest", channels=[a["id"], p["id"]])
    gus = Client.join(env.url, inv["code"], "gus")
    assert gus.me()["role"] == "guest"
    assert sorted(c["name"] for c in gus.list_channels() if c["kind"] != "dm") == ["alpha", "pvt"]
    assert _err(lambda: gus.read(b["id"])) == 404
    gus.post(a["id"], "hi from a guest")

    # guests invite guest agents only, only into their channels, never "all open channels"
    assert _err(lambda: gus.create_invite("agent", role="member")) == 403
    assert _err(lambda: gus.create_invite("agent", role="guest", join_open_channels=True)) == 403
    assert _err(lambda: gus.create_invite("agent", role="guest", channels=[b["id"]])) == 404
    bot = Client.join(env.url, gus.create_invite("agent", role="guest", channels=[a["id"]])["code"], "gus-bot")
    assert [c["name"] for c in bot.list_channels() if c["kind"] != "dm"] == ["alpha"]
    bot.post(a["id"], "hi from the guest's agent")
    assert _err(lambda: gus.update_identity(bot.me()["id"], role="member")) == 403

    # a guest adds only their own agents; their agents leave with them
    _, _, ana = env.human("ana")
    assert _err(lambda: gus.add_member(p["id"], ana.me()["id"])) == 404   # not even visible to a guest
    ana.post(a["id"], "hello gus")                                         # now gus can see ana...
    assert _err(lambda: gus.add_member(p["id"], ana.me()["id"])) == 403   # ...but still cannot add her
    gus.add_member(p["id"], bot.me()["id"])
    env.op.remove_member(a["id"], gus.me()["id"])
    assert sorted(c["name"] for c in bot.list_channels() if c["kind"] != "dm") == ["pvt"]


def test_member_who_becomes_guest_takes_member_agents_along(env):
    _, _, ana = env.human("ana")
    bot = Client.join(env.url, ana.create_invite("agent", role="member")["code"], "ana-bot")
    env.op.update_identity(ana.me()["id"], role="guest")
    assert bot.me()["role"] == "guest"                 # still works, now as a guest
    assert ana.me()["permissions"]["invite_agents"] is True
