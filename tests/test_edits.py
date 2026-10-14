from __future__ import annotations

import threading

import pytest

from msgboard.client import BoardError


def test_edit_own_message(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    msg = ana.post(gen["id"], "teh plan", request_id="plan-1")
    assert msg["edited_at"] is None
    edited = ana.edit(gen["id"], msg["id"], "the plan, cc @bob")
    assert (edited["seq"], edited["body"]) == (msg["seq"], "the plan, cc @bob")
    assert edited["edited_at"] is not None
    assert [m["external_id"] for m in edited["mentions"]] == ["bob"]   # mentions follow the new text
    assert bob.read(gen["id"])["messages"][0]["body"] == "the plan, cc @bob"
    assert ana.edit(gen["id"], msg["id"], "the plan, cc @bob")["edited_at"] == edited["edited_at"]  # no-op
    # a retried post with the original body still replays (and returns the edited message)
    assert ana.post(gen["id"], "teh plan", request_id="plan-1")["body"] == "the plan, cc @bob"


def test_only_the_sender_can_edit(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    msg = ana.post(gen["id"], "mine")
    other = env.op.create_channel("other")
    for client, channel, code in [(bob, gen["id"], 403), (env.op, gen["id"], 403), (ana, other["id"], 404)]:
        with pytest.raises(BoardError) as e:
            client.edit(channel, msg["id"], "hijacked")
        assert e.value.status == code
    with pytest.raises(BoardError) as e:
        ana.edit(gen["id"], msg["id"], "  ")
    assert e.value.status == 400


def test_long_poll_wakes_on_edit(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    msg = ana.post(gen["id"], "first draft")
    start = bob.list_channels()[0]
    assert start["edit_rev"] == 0
    http = env.http(bob.token)
    url = f"/channels/{gen['id']}/messages"
    assert http.get(url, params={"after": msg["seq"], "edits_after": 0}).json()["edits"] == []
    out = {}
    t = threading.Thread(target=lambda: out.update(http.get(
        url, params={"after": msg["seq"], "edits_after": 0, "wait": 10}, timeout=15).json()))
    t.start()
    ana.edit(gen["id"], msg["id"], "final draft")
    t.join(12)
    assert out["messages"] == [] and [m["body"] for m in out["edits"]] == ["final draft"]
    assert out["edit_cursor"] == 1
    again = http.get(url, params={"after": msg["seq"], "edits_after": 1}).json()
    assert again["edits"] == [] and again["edit_cursor"] == 1


def test_edits_keep_every_version(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    msg = ana.post(gen["id"], "v1")
    assert [v["body"] for v in bob.revisions(gen["id"], msg["id"])] == ["v1"]
    ana.edit(gen["id"], msg["id"], "v2")
    ana.edit(gen["id"], msg["id"], "v2")                 # unchanged: no new version
    ana.edit(gen["id"], msg["id"], "v3")
    versions = bob.revisions(gen["id"], msg["id"])
    assert [(v["version"], v["body"]) for v in versions] == [(1, "v1"), (2, "v2"), (3, "v3")]
    assert versions[0]["written_at"] == msg["created_at"] and versions[-1]["replaced_at"] is None
    assert versions[0]["replaced_at"] == versions[1]["written_at"]


def test_agents_cannot_edit(env):
    gen = env.op.create_channel("general")
    _, _, bot = env.agent("bot", channels=["general"])
    msg = bot.post(gen["id"], "a result")
    with pytest.raises(BoardError) as e:
        bot.edit(gen["id"], msg["id"], "a better result")
    assert e.value.status == 403 and "follow-up" in str(e.value)
    assert bot.revisions(gen["id"], msg["id"])[-1]["body"] == "a result"
