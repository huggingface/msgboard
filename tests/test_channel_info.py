from __future__ import annotations

import pytest

from msgboard.client import BoardError


def test_channel_info_and_rename(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    ch = ana.create_channel("plans", description="old")
    ana.post(ch["id"], "one")
    root = bob.post(ch["id"], "two")
    ana.post(ch["id"], "three", reply_to=root["id"])
    info = bob.channel_info(ch["id"])
    assert (info["messages"], info["threads"], info["created_by"]["external_id"]) == (3, 1, "ana")
    assert [(x["external_id"], x["messages"]) for x in info["posters"]] == [("ana", 2), ("bob", 1)]
    assert info["audience"] == "everyone" and not info["can_edit"] and not info["can_archive"]
    with pytest.raises(BoardError) as e:
        bob.update_channel(ch["id"], name="mine")
    assert e.value.status == 403
    out = ana.update_channel(ch["id"], name="roadmap", description="  where we go  ")
    assert (out["name"], out["description"]) == ("roadmap", "where we go")
    env.op.create_channel("taken")
    with pytest.raises(BoardError) as e:
        ana.update_channel(ch["id"], name="taken")
    assert e.value.status == 409
    with pytest.raises(BoardError) as e:
        bob.update_channel(ch["id"], archived=True)        # only admins and the creator archive
    assert e.value.status == 403
    assert env.op.update_channel(ch["id"], archived=True)["archived"] is True
    assert env.op.update_channel(ch["id"], archived=False)["archived"] is False


def test_creators_archive_and_admins_delete(env, tmp_path):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    ch = ana.create_channel("scratch")
    assert ana.channel_info(ch["id"])["can_archive"] and not ana.channel_info(ch["id"])["can_delete"]
    assert ana.update_channel(ch["id"], archived=True)["archived"] is True       # the creator can archive
    with pytest.raises(BoardError) as e:
        bob.update_channel(ch["id"], archived=False)
    assert e.value.status == 403
    ana.update_channel(ch["id"], archived=False)
    root = ana.post(ch["id"], "findable needle")
    bob.post(ch["id"], "reply", reply_to=root["id"])
    bob.react(ch["id"], root["id"], "👍")
    ana.save_doc(ch["id"], "doc", base_version=0)
    att = ana.upload(ch["id"], b"hello", filename="a.txt", content_type="text/plain")
    ana.post(ch["id"], "file", attachments=[att["id"]])
    inv = env.op.create_invite("agent", role="guest", channels=[ch["id"]])
    assert len(ana.search("needle")["messages"]) == 1
    with pytest.raises(BoardError) as e:
        ana.delete_channel(ch["id"], "scratch")                                     # admins only
    assert e.value.status == 403
    with pytest.raises(BoardError) as e:
        env.op.delete_channel(ch["id"], "wrong name")
    assert e.value.status == 400
    out = env.op.delete_channel(ch["id"], "scratch")
    assert (out["deleted"], out["messages"], out["files"]) == ("scratch", 3, 1)
    assert all(c["name"] != "scratch" for c in ana.list_channels())
    assert ana.search("needle")["messages"] == []
    assert env.http(ana.token).get(f"/messages/{root['id']}").status_code == 404
    assert not list((env.data_dir / "blobs" / "objects").rglob(att["id"] + "*"))
    assert [i for i in env.op.invites() if i["id"] == inv["id"]][0]["channels"] == []
    assert env.op.create_channel("scratch")["name"] == "scratch"                    # the name is free again


def test_channel_order_preference(env):
    _, _, ana = env.human("ana")
    a = env.op.create_channel("alpha"); b = env.op.create_channel("beta")
    http = env.http(ana.token)
    assert ana.me()["prefs"] == {}
    assert http.put("/me/prefs", json={"channel_order": [b["id"], a["id"], b["id"]]}).json() == {"channel_order": [b["id"], a["id"]]}
    assert ana.me()["prefs"]["channel_order"] == [b["id"], a["id"]]            # follows the account, any device
    assert http.put("/me/prefs", json={"channel_order": "nope"}).status_code == 400
    assert http.put("/me/prefs", json={"theme": "dark"}).status_code == 400
    assert http.put("/me/prefs", json={"channel_order": []}).json() == {}


def test_member_flag_and_show_private_preference(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    pub = env.op.create_channel("open")
    priv = ana.create_channel("secret", private=True)
    dm = ana.open_dm(bob.me()["id"])
    seen = {c["id"]: c["member"] for c in env.op.list_channels()}
    assert seen[priv["id"]] is False                                          # admin sees it, but is not in it
    assert {c["id"]: c["member"] for c in ana.list_channels()}[priv["id"]] is True
    assert seen[dm["id"]] is False
    http = env.http(env.op.token)
    assert http.put("/me/prefs", json={"show_private": True}).json() == {"show_private": True}
    assert http.put("/me/prefs", json={"show_private": "yes"}).status_code == 400
    assert http.put("/me/prefs", json={"show_private": False}).json() == {}
    assert pub["id"] in seen


def test_mark_all_read_clears_channels_dms_and_threads(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    ch = ana.create_channel("busy")
    root = bob.post(ch["id"], "a question")
    ana.post(ch["id"], "an answer", reply_to=root["id"])            # bob's thread now has a reply for him
    ana.post(ch["id"], "@bob look")
    dm = ana.open_dm(bob.me()["id"]); ana.post(dm["id"], "hi bob")
    before = {c["id"]: c for c in bob.list_channels()}
    assert before[ch["id"]]["for_me"] >= 2 and before[dm["id"]]["for_me"] == 1
    assert bob.mark_all_read()["channels"] >= 2
    after = {c["id"]: c for c in bob.list_channels()}
    for cid in (ch["id"], dm["id"]):
        assert (after[cid]["unread"], after[cid]["for_me"], after[cid]["threads_for_me"]) == (0, 0, 0)
    assert not any(m["unread"] for m in bob.activity_page()["messages"])
    ana.post(ch["id"], "@bob one more")                              # new messages count again
    assert {c["id"]: c for c in bob.list_channels()}[ch["id"]]["for_me"] == 1
