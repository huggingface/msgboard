from __future__ import annotations

import pytest

from msgboard.client import BoardError, Client


def test_storage_folders(env):
    _, _, ana = env.human("ana")
    bot = Client.join(env.url, ana.create_invite("agent")["code"], "trainer")
    _, _, bob = env.human("bob")
    guest = Client.join(env.url, env.op.create_invite("agent", role="guest")["code"], "visitor")
    out = bot.storage_put("runs/1/loss.png", b"PNG-fake-1", content_type="image/png")
    assert (out["path"], out["size"], out["url"]) == ("runs/1/loss.png", 10, "/v1/storage/trainer/runs/1/loss.png")
    bot.storage_put("runs/1/metrics.json", b'{"acc": 0.9}')
    bot.storage_put("notes.md", b"# notes")
    assert bob.storage_get("runs/1/loss.png", handle="trainer") == b"PNG-fake-1"     # others can read
    listing = bob.storage_list(handle="trainer", prefix="runs")
    assert [f["path"] for f in listing["files"]] == ["runs/1/loss.png", "runs/1/metrics.json"]
    assert listing["used_bytes"] == 10 + 12 + 7
    with pytest.raises(BoardError) as e:
        bob.storage_put("evil.txt", b"x", handle="trainer")                                # only the owner side writes
    assert e.value.status == 403
    ana.storage_put("from-owner.txt", b"hi", handle="trainer")                              # the agent's owner may
    with pytest.raises(BoardError) as e:
        guest.storage_get("notes.md", handle="trainer")                                    # guests see only their own
    assert e.value.status == 404
    from msgboard import store
    from msgboard.errors import ApiError
    for bad in ["../x", "a//b", ".", "a/./b", "a/../../b", "x" * 600, "a\x01b"]:   # the server's own check
        with pytest.raises(ApiError):
            store.storage_path(bad)
    assert store.storage_path("/runs/1/loss.png/") == "runs/1/loss.png"
    with pytest.raises(BoardError) as e:
        bot.storage_put("../x", b"x")              # over HTTP the client may normalise `..` away; never written
    assert e.value.status in (400, 404, 405)
    with pytest.raises(BoardError) as e:
        bot.storage_put("runs/1", b"x")                                                     # a folder, not a file
    assert e.value.status == 409
    assert sorted(bot.storage_delete("runs")["deleted"]) == ["runs/1/loss.png", "runs/1/metrics.json"]
    assert [f["path"] for f in bot.storage_list()["files"]] == ["from-owner.txt", "notes.md"]
    raw = env.http(bob.token).get("/storage/trainer/notes.md")
    assert raw.headers["content-type"] == "application/octet-stream" and "sandbox" in raw.headers["content-security-policy"]


def test_storage_limits_are_admin_settings(env):
    _, _, ana = env.human("ana")
    bot = Client.join(env.url, ana.create_invite("agent")["code"], "bot")
    env.op.update_settings(storage_max_file_mb=1, storage_quota_mb=2)
    assert bot.storage_list()["max_file_bytes"] == 1024 ** 2
    with pytest.raises(BoardError) as e:
        bot.storage_put("big.bin", b"x" * (1024 ** 2 + 1))
    assert e.value.status == 413 and "1 MB" in e.value.message
    bot.storage_put("a.bin", b"x" * 1024 ** 2); bot.storage_put("b.bin", b"x" * 1024 ** 2)
    with pytest.raises(BoardError) as e:
        bot.storage_put("c.bin", b"x")
    assert e.value.status == 413 and "2 MB" in e.value.message
    with pytest.raises(BoardError) as e:
        ana.update_settings(storage_quota_mb=999)                  # admins only
    assert e.value.status == 403
    with pytest.raises(BoardError) as e:
        env.op.update_settings(storage_max_file_mb=0)
    assert e.value.status == 400
