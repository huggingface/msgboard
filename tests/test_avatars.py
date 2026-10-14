from __future__ import annotations

import struct
import zlib

import pytest

from msgboard.client import BoardError


def png(color: int = 0) -> bytes:
    """A valid 1x1 grayscale PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color]))) + chunk(b"IEND", b""))


def test_upload_show_replace_remove(env):
    ch = env.op.create_channel("c")
    ident, token, agent = env.agent("artist")
    _, _, other = env.agent("other")
    assert agent.me()["avatar"] is None                       # generated pattern by default
    v1 = agent.set_avatar(png(10))["avatar"]
    msg = agent.post(ch["id"], "with a face")
    assert msg["sender"]["avatar"] == v1                       # messages say which picture to show
    with env.http(token) as h:
        r = h.get(f"/principals/{ident['id']}/avatar")
        assert r.content == png(10) and r.headers["content-type"] == "image/png"
        assert r.headers["x-content-type-options"] == "nosniff"
    v2 = agent.set_avatar(png(200))["avatar"]
    assert v2 != v1 and other.read(ch["id"])["messages"][0]["sender"]["avatar"] == v2
    assert agent.remove_avatar()["avatar"] is None
    with pytest.raises(BoardError) as exc:
        other._request("GET", f"/principals/{ident['id']}/avatar")
    assert exc.value.status == 404


def test_avatar_rules(env):
    ident, _, agent = env.agent("a")
    other, _, _ = env.agent("b")
    with pytest.raises(BoardError) as exc:                     # only your own picture
        agent.set_avatar(png(), principal_id=other["id"])
    assert exc.value.status == 403
    assert env.op.set_avatar(png(), principal_id=other["id"])["avatar"]  # admins may change anyone's
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"not an image"):
        with pytest.raises(BoardError) as exc:
            agent.set_avatar(bad)
        assert exc.value.status == 400
    with pytest.raises(BoardError) as exc:
        agent.set_avatar(png() + b"\0" * (256 * 1024))
    assert exc.value.status == 413
    with env.http(None) as h:                                  # not public
        assert h.get(f"/principals/{ident['id']}/avatar").status_code == 401


def test_choosing_a_pattern(env):
    ch = env.op.create_channel("c")
    ident, _, me = env.human("person")
    other, _, _ = env.agent("other")
    me.set_avatar(png())
    assert me.set_pattern(123456) == {"pattern": 123456, "avatar": None}   # replaces the uploaded picture
    sender = me.post(ch["id"], "hi")["sender"]
    assert sender["pattern"] == 123456 and sender["avatar"] is None
    assert me._request("GET", f"/principals/{ident['id']}/profile").json()["pattern"] == 123456
    for bad in (-1, 2 ** 32, "7", None, True):
        with pytest.raises(BoardError) as exc:
            me._request("PUT", f"/principals/{ident['id']}/pattern", json={"seed": bad})
        assert exc.value.status == 400
    with pytest.raises(BoardError) as exc:
        me.set_pattern(1, principal_id=other["id"])
    assert exc.value.status == 403
