from __future__ import annotations

import threading
import time

import pytest

from msgboard.client import BoardError, Client


def test_activity_cursor_resumes_and_follows_channels(env):
    gen = env.op.create_channel("general")
    side = env.op.create_channel("side")
    _, _, ana = env.human("ana")
    bot_ident, _, bot = env.agent("bot", channels=["general", "side"])
    start = bot.activity_page()["next_cursor"]
    ana.post(gen["id"], "chatter the bot is not told about")
    ana.post(gen["id"], "@bot please look")
    ana.post(side["id"], "side channel news")
    page = bot.activity_page(start)
    assert [(m["body"], m["reason"]) for m in page["messages"]] == [("@bot please look", "mention")]
    page = bot.activity_page(start, channels=[side["id"]])
    assert [m["reason"] for m in page["messages"]] == ["mention", "channel"]
    cursor = page["next_cursor"]
    root = bot.post(gen["id"], "my question")
    ana.post(gen["id"], "an answer", reply_to=root["id"])
    dm = ana.open_dm(bot_ident["id"])
    ana.post(dm["id"], "psst")
    later = bot.activity_page(cursor)                       # resumes exactly after the cursor
    assert [m["reason"] for m in later["messages"]] == ["reply", "dm"]
    assert bot.activity_page(later["next_cursor"])["messages"] == []
    small = bot.activity_page(start, limit=1, channels=[side["id"]])
    assert small["has_more"] and len(small["messages"]) == 1


def test_activity_long_poll_wakes_only_for_me(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bot = env.agent("bot", channels=["general"])
    cursor = bot.activity_page()["next_cursor"]
    out = {}
    t = threading.Thread(target=lambda: out.update(bot.activity_page(cursor, wait=10)))
    t.start()
    time.sleep(0.5)
    ana.post(gen["id"], "not for the bot")              # wakes the poll, which goes back to waiting
    time.sleep(0.5)
    assert t.is_alive()
    ana.post(gen["id"], "@bot now")
    t.join(8)
    assert [m["body"] for m in out["messages"]] == ["@bot now"]


def test_activity_rejects_hidden_channels(env):
    _, _, ana = env.human("ana")
    secret = ana.create_channel("secret", private=True)
    _, _, bot = env.agent("bot")
    with pytest.raises(BoardError) as e:
        bot.activity_page(0, channels=[secret["id"]])
    assert e.value.status == 404


def test_guide_and_guidelines(env):
    public = env.http(None).get(env.url + "/agents")
    assert public.status_code == 200 and "/v1/activity" in public.text and "Workspace guidelines" not in public.text
    _, _, bot = env.agent("bot")
    assert "no guidelines yet" in bot.guide()
    env.op.update_settings(guidelines="Always reply in threads.")
    assert bot.guide().rstrip().endswith("Always reply in threads.")
    with pytest.raises(BoardError) as e:
        bot.update_settings(guidelines="mine now")
    assert e.value.status == 403


def test_analytics_for_admins_only(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bot = env.agent("bot", channels=["general"])
    ana.post(gen["id"], "hi")
    bot.post(gen["id"], "hello")
    bot.post(gen["id"], "again")
    ana.post(ana.open_dm(bot.identity["id"])["id"], "dm")
    a = env.op.analytics(days=7)
    assert len(a["daily"]) == 7 and a["daily"][-1] == {"day": a["daily"][-1]["day"], "people": 2, "agents": 2}
    by = {c["name"]: c for c in a["channels"]}
    assert by["general"]["messages"] == 3 and by["direct messages"]["messages"] == 1 and by["direct messages"]["id"] is None
    assert sorted((x["external_id"], x["messages"]) for x in a["posters"]) == [("ana", 2), ("bot", 2)]
    assert a["totals"]["active_agents_7d"] == 1
    with pytest.raises(BoardError) as e:
        ana.analytics()
    assert e.value.status == 403


def test_activity_follows_joined_channels(env):
    a = env.op.create_channel("alpha"); b = env.op.create_channel("beta"); c = env.op.create_channel("gamma")
    _, _, ana = env.human("ana")
    bot = Client.join(env.url, env.op.create_invite("agent", join_open_channels=True)["code"], "listener")
    env.op.remove_member(c["id"], bot.identity["id"])                  # leaves one channel
    start = bot.activity_page(channels="joined")["next_cursor"]
    ana.post(a["id"], "in alpha"); ana.post(b["id"], "in beta"); ana.post(c["id"], "in gamma")
    ana.post(c["id"], "@listener ping from gamma")
    got = [(m["body"], m["reason"]) for m in bot.activity_page(start, channels="joined")["messages"]]
    assert got == [("in alpha", "channel"), ("in beta", "channel"), ("@listener ping from gamma", "mention")]
