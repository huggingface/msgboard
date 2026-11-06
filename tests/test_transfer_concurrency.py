import concurrent.futures
import threading

import pytest

from tests.conftest import Env
from tests.test_transfer import export, load


@pytest.fixture
def pair(tmp_path):
    source, target = Env(tmp_path / "source"), Env(tmp_path / "target")
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def pause_backend(monkeypatch, target, method, suffix="/held.txt"):
    backend = target.server.board.storage
    original = getattr(backend, method)
    started, release = threading.Event(), threading.Event()
    paused = False

    def slow(*args):
        nonlocal paused
        result = original(*args)
        key = args[0][0][1] if method == "put" else args[0]
        if key.endswith(suffix) and not paused:
            paused = True
            started.set()
            if not release.wait(10):
                raise TimeoutError("test backend was not released")
        return result
    monkeypatch.setattr(backend, method, slow)
    return started, release


@pytest.mark.parametrize("method", ["put", "get"])
def test_slow_import_bucket_io_leaves_writer_and_other_files_free(pair, monkeypatch, method):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.storage_put("held.txt", b"history storage bytes")
    source.op.post(ch, "[file](/v1/storage/operator/held.txt)")
    dest = target.op.create_channel("imported")["id"]
    other = target.op.create_channel("live")["id"]
    anchor = target.op.post(other, "anchor")
    target.op.storage_put("delete.txt", b"delete independently")
    if method == "get":
        target.op.storage_put("held.txt", b"history storage bytes")
    data = export(source, ch).content
    started, release = pause_backend(monkeypatch, target, method)

    def ordinary_work():
        post = target.op.post(other, "post while import is uploading")
        target.op.react(other, anchor["id"], "👍")
        target.op.mark_read(other, post["seq"])
        target.op.storage_put("independent.txt", b"another path, same owner")
        target.op.storage_delete("delete.txt")
        target.op.upload(other, b"agent attachment", filename="agent.txt")
        return post

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        importing = pool.submit(load, target, dest, data)
        try:
            assert started.wait(3)
            # Completion before release, rather than an elapsed-time benchmark,
            # proves neither writer nor global attachment commit lock is held.
            post = pool.submit(ordinary_work).result(timeout=3)
            assert post["body"] == "post while import is uploading"
            assert not importing.done()
        finally:
            release.set()
        response = importing.result(timeout=10)
    assert response.status_code == 200, response.text
    assert target.op.storage_get("held.txt") == b"history storage bytes"


def test_target_post_during_upload_aborts_import_and_cleans_new_files_ghosts(pair, monkeypatch):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, owner = source.agent("ghost-owner")
    owner.storage_put("held.txt", b"staged bytes")
    source.op.post(ch, "[file](/v1/storage/ghost-owner/held.txt)")
    dest = target.op.create_channel("imported")["id"]
    started, release = pause_backend(monkeypatch, target, "put")
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        importing = pool.submit(load, target, dest, export(source, ch).content)
        try:
            assert started.wait(3)
            post = pool.submit(target.op.post, dest, "keep this live post").result(timeout=3)
        finally:
            release.set()
        response = importing.result(timeout=10)
    assert response.status_code == 409
    assert [m["id"] for m in target.op.read(dest)["messages"]] == [post["id"]]
    assert not any(p["external_id"] == "ghost-owner" for p in target.op.people())
    assert target.server.board.storage.list("storage") == []


def test_large_ordinary_upload_does_not_block_other_paths_or_agent_attachments(pair, monkeypatch):
    _, target = pair
    other = target.op.create_channel("live")["id"]
    started, release = pause_backend(monkeypatch, target, "put", "/folder/held.txt")

    def upload_held():
        with target.http(target.operator_token) as client:
            return client.put("/storage/operator/folder/held.txt", content=b"slow body")

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        uploading = pool.submit(upload_held)
        try:
            assert started.wait(3)
            deleting = pool.submit(target.op.storage_delete, "folder")
            pool.submit(target.op.storage_put, "elsewhere.txt", b"same owner, unrelated path").result(timeout=3)
            pool.submit(target.op.upload, other, b"agent bytes", filename="agent.txt").result(timeout=3)
            pool.submit(target.op.post, other, "ordinary post").result(timeout=3)
            assert not uploading.done() and not deleting.done()
        finally:
            release.set()
        assert uploading.result(timeout=10).status_code == 201
        assert deleting.result(timeout=10)["deleted"] == ["folder/held.txt"]
    assert target.op.storage_get("elsewhere.txt") == b"same owner, unrelated path"


def test_late_quota_failure_restores_replaced_bucket_bytes(pair, monkeypatch):
    _, target = pair
    target.op.update_settings(storage_max_file_mb=1, storage_quota_mb=1)
    target.op.storage_put("held.txt", b"old" * (32 * 1024))
    started, release = pause_backend(monkeypatch, target, "put")

    def replace():
        with target.http(target.operator_token) as client:
            return client.put("/storage/operator/held.txt", content=b"x" * (600 * 1024))

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replacement = pool.submit(replace)
        try:
            assert started.wait(3)
            pool.submit(target.op.storage_put, "other.bin", b"y" * (600 * 1024)).result(timeout=3)
        finally:
            release.set()
        assert replacement.result(timeout=10).status_code == 413
    raw = target.data_dir / "storage" / str(target.op.me()["id"]) / "held.txt"
    assert raw.read_bytes() == b"old" * (32 * 1024)
    assert target.op.storage_get("held.txt") == raw.read_bytes()


def test_parentheses_storage_links_copy_and_rewrite(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, owner = source.agent("Owner")
    target.agent("owner")
    owner.storage_put("plot (2).png", b"image bytes")
    source.op.post(ch, f"![plot]({source.url}/v1/storage/Owner/plot%20(2).png)")
    dest = target.op.create_channel("imported")["id"]
    data = export(source, ch)
    assert data.status_code == 200, data.text
    response = load(target, dest, data.content)
    assert response.status_code == 200, response.text
    assert target.op.read(dest)["messages"][0]["body"] == "![plot](/v1/storage/owner/plot%20%282%29.png)"
    assert target.op.storage_get("plot (2).png", handle="owner") == b"image bytes"


def test_uncached_read_waits_only_for_its_replacement_path(pair, monkeypatch):
    _, target = pair
    target.op.storage_put("held.txt", b"old bytes")
    target.op.storage_put("other.txt", b"independent read")
    for path in target.server.board.storage_cache.iterdir():
        path.unlink()
    started, release = pause_backend(monkeypatch, target, "put")
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        replacing = pool.submit(target.op.storage_put, "held.txt", b"new bytes")
        try:
            assert started.wait(3)
            reading = pool.submit(target.op.storage_get, "held.txt")
            assert pool.submit(target.op.storage_get, "other.txt").result(timeout=3) == b"independent read"
            assert not reading.done()
        finally:
            release.set()
        replacing.result(timeout=10)
        assert reading.result(timeout=10) == b"new bytes"


@pytest.mark.parametrize("punctuation", [".", ",", ":"])
def test_storage_link_markdown_boundary_and_code_examples(pair, punctuation):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.storage_put("report (2).txt", b"report bytes")
    code = (f"\n`{source.url}/v1/storage/operator/example.txt`\n"
            "```text\n/v1/storage/nobody/not-a-file.txt\n```\n"
            "~~~text\n/v1/storage/nobody/another-example.txt\n~~~")
    source.op.post(ch, f"See [report]({source.url}/v1/storage/operator/report%20(2).txt){punctuation}" + code)
    exported = export(source, ch)
    assert exported.status_code == 200, exported.text
    dest = target.op.create_channel("imported")["id"]
    response = load(target, dest, exported.content)
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["storage_files"] == 1
    assert target.op.read(dest)["messages"][0]["body"] == (
        "See [report](/v1/storage/operator/report%20%282%29.txt)" + punctuation + code)
    assert target.op.storage_get("report (2).txt") == b"report bytes"
