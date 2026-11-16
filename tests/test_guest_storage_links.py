"""Guest downloads follow a readable sharing message, not access to a whole folder."""

from urllib.parse import quote

import pytest


def request(env, token, method, path, **kwargs):
    with env.http(token) as http:
        return http.request(method, path, **kwargs)


def download_link(env, token, handle, path):
    response = request(env, token, "POST", "/downloads", json={"file_id": f"storage:{handle}/{path}"})
    assert response.status_code == 200, response.text
    return response.json()["url"].removeprefix("v1")


@pytest.mark.parametrize("kind", ["agent", "human"])
def test_guest_downloads_shared_files_cold_cached_metadata_and_ranges(env, kind):
    channel = env.op.create_channel("files")["id"]
    _, _, author = env.human("author")
    identity, token, guest = getattr(env, kind)("guest", role="guest")
    env.op.add_member(channel, identity["id"])
    name = "files/screen (1) #é.png"
    author.storage_put(name, b"0123456789", content_type="image/png")
    author.storage_put(name + ".private", b"not shared")
    encoded = quote(name, safe="/")
    author.post(channel, f"![screen](/v1/storage/author/{encoded})")
    # A real cache miss must use the file-scoped permission on both lookups.
    for cached in (env.data_dir / "storage-cache").iterdir():
        cached.unlink()
    path = f"/storage/author/{encoded}"
    response = request(env, token, "GET", path)
    assert response.content == b"0123456789"
    response = request(env, token, "GET", path)
    assert response.content == b"0123456789"
    info = request(env, token, "GET", path, params={"info": True})
    assert info.status_code == 200 and info.json()["size"] == 10
    assert guest.storage_get(name, handle="author") == b"0123456789"
    link = download_link(env, token, "author", name)
    response = request(env, None, "HEAD", link)
    assert response.headers["content-length"] == "10"
    part = request(env, None, "GET", link, headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.content == b"2345"
    for hidden in ["/storage/author", "/storage/author/", "/storage/author/files/", path + ".private"]:
        response = request(env, token, "GET", hidden)
        assert response.status_code in (400, 404)
    response = request(env, token, "PUT", path, content=b"overwrite")
    assert response.status_code == 403
    response = request(env, token, "DELETE", path)
    assert response.status_code == 403


def test_sharing_requires_readable_message_and_cannot_be_forged_by_guest(env):
    visible = env.op.create_channel("visible")["id"]
    hidden = env.op.create_channel("hidden", private=True)["id"]
    _, _, owner = env.human("owner")
    identity, token, guest = env.agent("guest", role="guest")
    env.op.add_member(visible, identity["id"])
    owner.storage_put("shared.png", b"image")
    owner.storage_put("private.png", b"private")
    owner.storage_put("shared.png.extra", b"neighbour")
    env.op.add_member(hidden, owner.identity["id"])
    owner.post(hidden, "![private](/v1/storage/owner/private.png)")
    guest.post(visible, "![guess](/v1/storage/owner/private.png)")
    response = request(env, token, "GET", "/storage/owner/private.png")
    assert response.status_code == 404
    # A guess in an otherwise readable DM must not grant access either.
    dm = guest.open_dm(env.op.me()["id"])["id"]
    guest.post(dm, "[guess](/v1/storage/owner/shared.png)")
    response = request(env, token, "GET", "/storage/owner/shared.png")
    assert response.status_code == 404
    owner.post(visible, "![shared](/v1/storage/owner/shared.png.extra)")
    response = request(env, token, "GET", "/storage/owner/shared.png")
    assert response.status_code == 404
    # The actual sharing message can be a thread reply.
    root = owner.post(visible, "discussion")
    owner.post(visible, "![shared](/v1/storage/owner/shared.png)", reply_to=root["id"])
    assert guest.storage_get("shared.png", handle="owner") == b"image"
    response = request(env, token, "GET", "/storage/owner/private.png")
    assert response.status_code == 404


def test_guest_shares_own_file_but_reference_does_not_expose_siblings(env):
    channel = env.op.create_channel("shared")["id"]
    author_id, _, author = env.human("author", role="guest")
    reader_id, token, reader = env.agent("reader", role="guest")
    env.op.add_member(channel, author_id["id"])
    env.op.add_member(channel, reader_id["id"])
    author.storage_put("result.txt", b"result")
    author.storage_put("notes.txt", b"notes")
    author.post(channel, "[result](/v1/storage/author/result.txt)")
    assert reader.storage_get("result.txt", handle="author") == b"result"
    response = request(env, token, "GET", "/storage/author/notes.txt")
    assert response.status_code == 404
    response = request(env, token, "GET", "/storage/author")
    assert response.status_code == 404


def test_link_removal_deletion_and_membership_revoke_download_capabilities(env):
    channel = env.op.create_channel("files")["id"]
    _, _, owner = env.human("owner")
    identity, token, guest = env.agent("guest", role="guest")
    env.op.add_member(channel, identity["id"])
    owner.storage_put("data.txt", b"data")
    first = owner.post(channel, "[data](/v1/storage/owner/data.txt)")
    link = download_link(env, token, "owner", "data.txt")
    owner.edit(channel, first["id"], "link removed")
    response = request(env, None, "GET", link)
    assert response.status_code == 404
    response = request(env, token, "GET", "/storage/owner/data.txt")
    assert response.status_code == 404
    second = owner.post(channel, "[data](/v1/storage/owner/data.txt)")
    assert guest.storage_get("data.txt", handle="owner") == b"data"
    link = download_link(env, token, "owner", "data.txt")
    env.op.delete_message(channel, second["id"])
    response = request(env, None, "GET", link)
    assert response.status_code == 404
    owner.edit(channel, first["id"], "[data](/v1/storage/owner/data.txt)")
    link = download_link(env, token, "owner", "data.txt")
    env.op.remove_member(channel, identity["id"])
    response = request(env, None, "GET", link)
    assert response.status_code == 404
    response = request(env, token, "GET", "/storage/owner/data.txt")
    assert response.status_code == 404
    # A historical message works immediately after joining again; no post/backfill is needed.
    env.op.add_member(channel, identity["id"])
    assert guest.storage_get("data.txt", handle="owner") == b"data"


@pytest.mark.parametrize("body", [
    "`[example](/v1/storage/owner/private.txt)`",
    "```\n![example](/v1/storage/owner/private.txt)\n```",
    "~~~\n![example](/v1/storage/owner/private.txt)\n~~~",
    "[external](https://elsewhere.example/v1/storage/owner/private.txt)",
    "[external](https://elsewhere.example/?next=(/v1/storage/owner/private.txt))",
    "```\n![example](/v1/storage/owner/private.txt)",
    "    [example](/v1/storage/owner/private.txt)",
    "[external](//elsewhere.example/v1/storage/owner/private.txt)",
    "[external](ftp://elsewhere.example/v1/storage/owner/private.txt)",
    "[other path](/archive/v1/storage/owner/private.txt)",
    "[other file](/v1/storage/owner/private.txt.bak)",
    "[bad path](/v1/storage/owner/../private.txt)",
])
def test_nonsharing_references_do_not_grant_file_access(env, body):
    channel = env.op.create_channel("files")["id"]
    identity, token, _ = env.agent("guest", role="guest")
    env.op.add_member(channel, identity["id"])
    env.op.storage_put("private.txt", b"private", handle="operator")
    # Keep the same target handle as the fixture, without adding a real author account per case.
    body = body.replace("/owner/", "/operator/")
    env.op.post(channel, body)
    response = request(env, token, "GET", "/storage/operator/private.txt")
    assert response.status_code == 404
