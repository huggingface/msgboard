import time

import pytest

from msgboard.auth import new_token
from msgboard.auth import DownloadLinks
from msgboard.errors import ApiError


def prepare(env, token, file_id):
    with env.http(token) as h:
        return h.post("/downloads", json={"file_id": file_id})


def test_native_download_streams_and_supports_ranges_without_a_board_token_in_the_url(env):
    _, token, person = env.human("downloader")
    person.storage_put("movie.mp4", b"0123456789", content_type="video/mp4")
    prepared = prepare(env, token, "storage:downloader/movie.mp4")
    assert prepared.status_code == 200
    link = prepared.json()
    secret_hidden = token not in prepared.text
    assert secret_hidden and link["expires_in"] == 120 and link["filename"] == "movie.mp4"
    path = link["url"].removeprefix("/v1")
    with env.http(None) as browser:
        response = browser.get(path)
        assert response.status_code == 200 and response.content == b"0123456789"
        assert "attachment;" in response.headers["content-disposition"]
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert browser.head(path).headers["content-length"] == "10"
        part = browser.get(path, headers={"Range": "bytes=2-5"})
        assert part.status_code == 206 and part.content == b"2345"
        assert part.headers["content-range"] == "bytes 2-5/10"
        assert browser.get(path[:-1] + "!").status_code == 404
        assert browser.get("/storage/downloader/movie.mp4").status_code == 401
    assert prepare(env, None, "storage:downloader/movie.mp4").status_code == 401


def test_download_links_preserve_attachment_and_storage_permissions(env):
    private = env.op.create_channel("private-files", private=True)
    _, owner_token, owner = env.agent("owner", channels=["private-files"])
    guest_id, guest_token, guest = env.agent("guest", role="guest")
    att = owner.upload(private["id"], b"private bytes", filename="private.txt")
    assert prepare(env, guest_token, att["id"]).status_code == 404
    owner.post(private["id"], "published", attachments=[att["id"]])
    assert prepare(env, guest_token, att["id"]).status_code == 404
    env.op.add_member(private["id"], guest_id["id"])
    path = prepare(env, guest_token, att["id"]).json()["url"].removeprefix("/v1")
    with env.http(None) as browser:
        assert browser.get(path).content == b"private bytes"
        env.op.remove_member(private["id"], guest_id["id"])
        assert browser.get(path).status_code == 404
    owner.storage_put("private-to-guests.txt", b"storage")
    assert prepare(env, guest_token, "storage:owner/private-to-guests.txt").status_code == 404
    assert prepare(env, owner_token, "storage:owner/").status_code == 400


def test_download_links_recheck_credentials_and_storage_version(env):
    ident, token, agent = env.agent("changing")
    agent.storage_put("file.txt", b"first")
    path = prepare(env, token, "storage:changing/file.txt").json()["url"].removeprefix("/v1")
    with env.http(None) as browser:
        agent.storage_put("file.txt", b"second")
        assert browser.get(path).status_code == 409
        fresh = prepare(env, token, "storage:changing/file.txt").json()["url"].removeprefix("/v1")
        assert browser.get(fresh).content == b"second"
        env.op.rotate_token(ident["id"])
        assert browser.get(fresh).status_code == 401


def test_download_link_stops_working_when_an_agents_owner_is_suspended(env):
    person, _, owner = env.human("parent")
    _, token, agent = env.agent("child", owner=owner)
    agent.storage_put("file.txt", b"owned bytes")
    path = prepare(env, token, "storage:child/file.txt").json()["url"].removeprefix("/v1")
    with env.http(None) as browser:
        assert browser.get(path).content == b"owned bytes"
        env.op.suspend(person["id"])
        assert browser.get(path).status_code == 401
        env.op.unsuspend(person["id"])
        assert browser.get(path).content == b"owned bytes"


def test_download_preparation_preserves_checksum_validation(env):
    ident, token, agent = env.agent("checksum")
    agent.storage_put("file.txt", b"original")
    (env.data_dir / "storage" / str(ident["id"]) / "file.txt").write_bytes(b"tampered")
    # Model a cold cache using actual files, as on a restored board.
    for path in (env.data_dir / "storage-cache").iterdir():
        path.unlink()
    response = prepare(env, token, "storage:checksum/file.txt")
    assert response.status_code == 409 and response.json()["error"]["code"] == "storage_mismatch"
    assert list((env.data_dir / "storage-cache").iterdir()) == []


def test_download_link_expiration_and_capacity_are_bounded():
    links = DownloadLinks(ttl_seconds=.2, max_links=1)
    token = new_token()[1]
    key = links.issue(1, token, "att_file", "checksum")
    secret_hidden = token not in repr(links.get(key))
    assert secret_hidden
    with pytest.raises(ApiError) as full:
        links.issue(2, token, "att_file", "checksum")
    assert full.value.status == 429
    time.sleep(.21)
    with pytest.raises(ApiError) as expired:
        links.get(key)
    assert expired.value.status == 404
    assert links.issue(1, token, "att_file", "checksum")
