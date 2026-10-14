from __future__ import annotations

import pytest

from msgboard.client import BoardError


def bodies(res):
    return [m["body"] for m in res["messages"]]


def test_search_text_filters_and_visibility(env):
    gen = env.op.create_channel("general")
    builds = env.op.create_channel("builds", description="CI and deploys")
    _, _, ana = env.human("ana", display_name="Ana Lopez")
    _, _, bob = env.human("bob")
    bot_ident, _, bot = env.agent("deployer", channels=["general", "builds"])
    secret = ana.create_channel("secret", private=True)
    ana.post(gen["id"], "The deployment failed on node 3")
    root = bot.post(builds["id"], "Deployed v2.1 to staging, cc @bob")
    ana.post(builds["id"], "nice, thanks", reply_to=root["id"])
    ana.post(secret["id"], "deployment secrets live here")
    bob.post(gen["id"], "lunch at noon? \"exact words\" here")

    r = bob.search("deploy")                                  # prefix match, newest first
    assert bodies(r) == ["Deployed v2.1 to staging, cc @bob", "The deployment failed on node 3"]
    assert r["total"] == 2 and "\x02" in r["messages"][0]["snippet"]
    assert [c["name"] for c in r["channels"]] == ["builds"]   # description matches
    assert [p["external_id"] for p in r["people"]] == ["deployer"]
    assert len(ana.search("deployment")["messages"]) == 2     # ana also sees her private channel

    assert bodies(bob.search("deploy from:@deployer")) == ["Deployed v2.1 to staging, cc @bob"]
    assert bodies(bob.search('from:"Ana Lopez"')) == ["nice, thanks", "The deployment failed on node 3"]
    assert bodies(bob.search("in:#general deploy")) == ["The deployment failed on node 3"]
    assert bodies(bob.search("to:@bob")) == ["Deployed v2.1 to staging, cc @bob"]
    assert bodies(bob.search("is:reply")) == ["nice, thanks"]
    assert bodies(bob.search("has:replies")) == ["Deployed v2.1 to staging, cc @bob"]
    assert bodies(bob.search("by:agent")) == ["Deployed v2.1 to staging, cc @bob"]
    assert bodies(bob.search('"exact words"')) == ['lunch at noon? "exact words" here']
    assert bodies(bob.search("deploy after:today")) and not bob.search("deploy before:2000-01-01")["messages"]
    assert bodies(bob.search("deploy", sort="oldest"))[0] == "The deployment failed on node 3"
    assert bob.search("https://example.com/x")["messages"] == []   # `https:` is text, not a filter
    assert bob.search("")["messages"] == []
    page = bob.search("deploy", limit=1)
    assert page["next_offset"] == 1 and bodies(bob.search("deploy", limit=1, offset=1)) == ["The deployment failed on node 3"]
    with pytest.raises(BoardError) as e:
        bob.search("in:#secret")                            # cannot even confirm a private channel exists
    assert e.value.status == 400


def test_search_follows_edits(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    msg = ana.post(gen["id"], "teh typo")
    ana.edit(gen["id"], msg["id"], "the fix")
    assert ana.search("teh")["messages"] == [] and bodies(ana.search("fix")) == ["the fix"]


def test_join_says_what_to_do_next(env):
    invite = env.op.create_invite("agent")
    r = env.http(None).post("/join", json={"code": invite["code"], "name": "newbie"}).json()
    assert "/v1/guide" in r["next"]
