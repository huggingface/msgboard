from __future__ import annotations

import pytest

from msgboard.client import BoardError


def test_reactions_roundtrip(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    _, _, bot = env.agent("bot", channels=["general"])
    msg = ana.post(gen["id"], "lunch: pizza or sushi?")
    assert msg["reactions"] == []
    bob.react(gen["id"], msg["id"], "🍕")
    bob.react(gen["id"], msg["id"], "🍕")                      # idempotent
    bot.react(gen["id"], msg["id"], "🍕")
    out = ana.react(gen["id"], msg["id"], "🍣")
    assert [(r["emoji"], r["count"], r["me"]) for r in out["reactions"]] == [("🍕", 2, False), ("🍣", 1, True)]
    assert [b["external_id"] for b in out["reactions"][0]["by"]] == ["bob", "bot"]
    seen_by_bob = bob.read(gen["id"])["messages"][0]["reactions"]
    assert [(r["emoji"], r["me"]) for r in seen_by_bob] == [("🍕", True), ("🍣", False)]
    bob.unreact(gen["id"], msg["id"], "🍕"); bob.unreact(gen["id"], msg["id"], "🍕")
    assert [(r["emoji"], r["count"]) for r in ana.read(gen["id"])["messages"][0]["reactions"]] == [("🍕", 1), ("🍣", 1)]
    assert ana.read(gen["id"])["messages"][0]["edited_at"] is None   # a reaction is not an edit
    for bad in ["thumbsup", ":+1:", "a b", ""]:
        with pytest.raises(BoardError) as e:
            ana.react(gen["id"], msg["id"], bad)
        assert e.value.status in (400, 404, 405)


def test_reactions_need_write_access_and_reach_followers(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, viewer = env.human("vic", role="viewer")
    msg = ana.post(gen["id"], "ship it?")
    with pytest.raises(BoardError) as e:
        viewer.react(gen["id"], msg["id"], "👍")
    assert e.value.status == 403
    http = env.http(ana.token)
    start = ana.list_channels()[0]["edit_rev"]
    env.op.react(gen["id"], msg["id"], "✅")
    page = http.get(f"/channels/{gen['id']}/messages", params={"after": msg["seq"], "edits_after": start}).json()
    assert [r["emoji"] for r in page["edits"][0]["reactions"]] == ["✅"]


def test_reactions_to_my_messages_show_in_activity(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    _, _, bot = env.agent("bot", channels=["general"])
    mine = ana.post(gen["id"], "proposal: ship friday")
    other = bob.post(gen["id"], "not ana's")
    bob.react(gen["id"], mine["id"], "👍"); bot.react(gen["id"], mine["id"], "✅")
    ana.react(gen["id"], mine["id"], "🎉")          # my own reaction is not activity
    bob.react(gen["id"], other["id"], "👀")         # not my message
    r = env.http(ana.token).get("/activity", params={"reactions": "true"}).json()["reactions"]
    assert len(r) == 1 and r[0]["message"]["body"] == "proposal: ship friday"
    assert [(x["emoji"], x["by"]["external_id"]) for x in r[0]["reactions"]] == [("✅", "bot"), ("👍", "bob")]
    assert "reactions" not in env.http(ana.token).get("/activity").json()
