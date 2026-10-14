from __future__ import annotations

import hashlib

import pytest

from msgboard.client import BoardError


def test_upload_publish_download(env):
    ch = env.op.create_channel("files")
    other = env.op.create_channel("other")
    _, _, alice = env.agent("alice", channels=["files"])
    _, _, bob = env.agent("bob", role="guest", channels=["files"])
    _, _, eve = env.agent("eve", role="guest", channels=["other"])

    att = alice.upload(ch["id"], b"hello file", upload_id="up-1", filename="../../etc/passwd")
    assert att["state"] == "ready" and att["filename"] == "passwd" and not att["published"]
    assert att["sha256"] == hashlib.sha256(b"hello file").hexdigest()

    # Unpublished: only the uploader (and admin) can fetch it.
    assert alice.download(att["id"]) == b"hello file"
    with pytest.raises(BoardError) as exc:
        bob.download(att["id"])
    assert exc.value.status == 404

    # Retrying the same upload ID is safe; different content is a conflict.
    assert alice.upload(ch["id"], b"hello file", upload_id="up-1")["id"] == att["id"]
    with pytest.raises(BoardError) as exc:
        alice.upload(ch["id"], b"other bytes", upload_id="up-1")
    assert exc.value.status == 409
    with pytest.raises(BoardError) as exc:
        alice.upload(other["id"], b"hello file", upload_id="up-1")
    assert exc.value.status in (404, 409)

    msg = alice.post(ch["id"], "see attached", attachments=[att["id"]])
    assert msg["attachments"] == [att["id"]]
    assert bob.download(att["id"]) == b"hello file"
    assert alice.download(att["id"]) == b"hello file"
    with pytest.raises(BoardError) as exc:
        eve.download(att["id"])
    assert exc.value.status == 404

    # Attachments cannot cross channels.
    _, _, both = env.agent("both", channels=["files", "other"])
    with pytest.raises(BoardError) as exc:
        both.post(other["id"], "smuggle", attachments=[att["id"]])
    assert exc.value.status == 400


def test_checksum_mismatch_is_rejected(env):
    ch = env.op.create_channel("files")
    with env.http(env.operator_token) as h:
        r = h.put(f"/channels/{ch['id']}/attachments/u1", content=b"abc",
                  headers={"X-Checksum-Sha256": "0" * 64})
        assert r.status_code == 400
        # The upload stayed pending; a correct retry under the same ID succeeds.
        r = h.put(f"/channels/{ch['id']}/attachments/u1", content=b"abc")
        assert r.status_code == 201
    assert list((env.data_dir / "blobs" / "tmp").iterdir()) == []


def test_unready_or_foreign_attachment_cannot_be_referenced(env):
    ch = env.op.create_channel("files")
    _, _, a = env.agent("a", channels=["files"])
    _, _, b = env.agent("b", channels=["files"])
    att = a.upload(ch["id"], b"draft")
    with pytest.raises(BoardError):
        b.post(ch["id"], "not mine", attachments=[att["id"]])
    with pytest.raises(BoardError):
        a.post(ch["id"], "missing", attachments=["att_doesnotexist"])


def test_attachment_order_is_kept(env):
    ch = env.op.create_channel("ordered")
    ids = [env.op.upload(ch["id"], f"file {i}".encode(), filename=f"f{i}.txt")["id"] for i in range(5)]
    wanted = [ids[3], ids[0], ids[4], ids[1], ids[2]]
    msg = env.op.post(ch["id"], "five files", attachments=wanted, request_id="ordered-1")
    assert msg["attachments"] == wanted
    assert env.op.read(ch["id"])["messages"][0]["attachments"] == wanted
    # a retry listing the same files in another order is the same message, not a conflict
    assert env.op.post(ch["id"], "five files", attachments=sorted(wanted), request_id="ordered-1")["id"] == msg["id"]
