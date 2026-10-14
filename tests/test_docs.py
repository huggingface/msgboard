from __future__ import annotations

import pytest

from msgboard.client import BoardError


def test_doc_versions_and_conflicts(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    _, _, bot = env.agent("bot", channels=["general"])
    _, _, vic = env.human("vic", role="viewer")
    empty = ana.doc(gen["id"])
    assert (empty["version"], empty["body"], empty["can_edit"]) == (0, "", True)
    v1 = ana.save_doc(gen["id"], "# Plan\\n- ship search", base_version=0)
    assert (v1["version"], v1["updated_by"]["external_id"]) == (1, "ana")
    v2 = bot.save_doc(gen["id"], v1["body"] + "\\n- ship docs", base_version=1)
    with pytest.raises(BoardError) as e:                       # bob edited v1 too: no silent overwrite
        bob.save_doc(gen["id"], "# Bob's plan", base_version=1)
    assert e.value.status == 409 and e.value.code == "doc_conflict"
    assert ana.save_doc(gen["id"], v2["body"], base_version=2)["version"] == 2   # unchanged: no new version
    versions = env.http(bob.token).get(f"/channels/{gen['id']}/doc/versions").json()["versions"]
    assert [(v["version"], v["author"]["external_id"]) for v in versions] == [(2, "bot"), (1, "ana")]
    assert env.http(bob.token).get(f"/channels/{gen['id']}/doc/versions/1").json()["body"] == "# Plan\\n- ship search"
    conflict = env.http(bob.token).put(f"/channels/{gen['id']}/doc", json={"body": "x", "base_version": 1}).json()
    assert conflict["error"]["details"]["current"]["version"] == 2
    assert vic.doc(gen["id"])["can_edit"] is False
    with pytest.raises(BoardError) as e:
        vic.save_doc(gen["id"], "nope", base_version=2)
    assert e.value.status == 403
    assert ana.list_channels()[0]["doc_version"] == 2


def test_message_by_id_respects_visibility(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    secret = ana.create_channel("secret", private=True)
    gen = env.op.create_channel("general")
    m = ana.post(gen["id"], "linkable")
    h = ana.post(secret["id"], "hidden")
    assert env.http(bob.token).get(f"/messages/{m['id']}").json()["channel"]["name"] == "general"
    assert env.http(bob.token).get(f"/messages/{h['id']}").status_code == 404


def test_docs_can_be_switched_off(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    ana.save_doc(gen["id"], "kept", base_version=0)
    env.op.update_settings(channel_docs=False)
    assert ana.me()["settings"]["channel_docs"] is False
    with pytest.raises(BoardError) as e:
        ana.doc(gen["id"])
    assert e.value.status == 403 and "switched off" in e.value.message
    with pytest.raises(BoardError):
        ana.save_doc(gen["id"], "x", base_version=1)
    assert "## Channel doc" not in ana.guide()
    with pytest.raises(BoardError) as e:
        ana.update_settings(channel_docs=True)              # admins only
    assert e.value.status == 403
    env.op.update_settings(channel_docs=True)
    assert ana.doc(gen["id"])["body"] == "kept" and "## Channel doc" in ana.guide()
