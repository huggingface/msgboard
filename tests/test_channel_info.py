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
