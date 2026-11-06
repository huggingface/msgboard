import concurrent.futures
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from msgboard import db
from msgboard.client import BoardError
from tests.conftest import Env


@pytest.fixture
def pair(tmp_path):
    source, target = Env(tmp_path / "source"), Env(tmp_path / "target")
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def export(env, channel, token=None):
    with env.http(token or env.operator_token) as c:
        return c.get(f"/channels/{channel}/export")


def load(env, channel, data, dry=False, token=None, keep_target_doc=False):
    with env.http(token or env.operator_token) as c:
        return c.post(f"/channels/{channel}/import", params={"dry_run": str(dry).lower(), "keep_target_doc": str(keep_target_doc).lower()},
                      content=data, headers={"Content-Type": "application/zip"})


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return json.loads(z.read("manifest.json")), {n: z.read(n) for n in z.namelist() if n != "manifest.json"}


def pack(manifest, blobs):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in blobs.items():
            z.writestr(name, data)
    return output.getvalue()


def fixture_history(source):
    ch = source.op.create_channel("history")["id"]
    human, _, alice = source.human("alice", display_name="Alice")
    _, _, ghost = source.agent("missing-agent", display_name="Missing agent")
    root = alice.post(ch, "original")
    # An edited human post, an attachment, an image linked only from Markdown,
    # nested replies, reactions by an otherwise absent account, and a doc.
    att = ghost.upload(ch, b"attachment bytes", filename="trace.log")
    image = alice.upload(ch, b"image bytes", filename="screen.png", content_type="image/png")
    alice.storage_put("design.png", b"storage image", content_type="image/png")
    body = f"latest @all @operator msg 123 ![screen](/v1/attachments/{image['id']}) [storage](/v1/storage/alice/design.png)"
    alice.edit(ch, root["id"], body)
    reply = ghost.post(ch, "reply", reply_to=root["id"], attachments=[att["id"]])
    nested = alice.post(ch, "nested", reply_to=reply["id"])
    _, _, reactor = source.human("reactor", display_name="Reaction only")
    reactor.react(ch, root["id"], "👍")
    source.op.save_doc(ch, "Current doc", base_version=0)
    # Timestamp ordering differs from insertion order but parent relationships
    # still hold; the importer must assign chronological channel sequences.
    conn = db.connect(source.data_dir / "board.db")
    try:
        for mid, stamp in [(root["id"], 100.0), (reply["id"], 300.0), (nested["id"], 200.0)]:
            conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (stamp, mid))
    finally:
        conn.close()
    return ch, human, root, reply, nested


def test_transfer_roundtrip_quiet_search_files_ghosts_and_replay(pair):
    source, target = pair
    ch, _human, root, reply, nested = fixture_history(source)
    _, _, alice = target.human("alice", display_name="Target Alice")
    dest = target.op.create_channel("imported")["id"]
    data = export(source, ch).content
    manifest, blobs = unpack(data)
    assert len(blobs) == 3
    before = target.op.people()
    dry = load(target, dest, data, dry=True)
    assert dry.status_code == 200, dry.text
    report = dry.json()
    assert report["can_import"] and report["counts"]["messages"] == 3
    assert report["counts"]["ghosts"] == 2  # missing-agent, reaction-only person
    assert next(a for a in report["authors"] if a["handle"] == "alice")["target_id"] == alice.identity["id"]
    assert target.op.people() == before
    assert target.op.read(dest)["messages"] == []
    assert not list((target.data_dir / "blobs" / "objects").rglob("att_*"))
    result = load(target, dest, data)
    assert result.status_code == 200, result.text
    messages = target.op.read(dest)["messages"]
    assert [m["created_at"] for m in messages] == [100.0, 200.0, 300.0]
    assert all(m["history"] and not m["mentions"] for m in messages)
    assert all(m["imported_from"]["board_url"] == source.url for m in messages)
    mapping = {m["imported_from"]["message_id"]: m for m in messages}
    assert mapping[nested["id"]]["reply_to"] == mapping[reply["id"]]["id"]
    assert mapping[reply["id"]]["thread_id"] == mapping[root["id"]]["id"]
    assert mapping[nested["id"]]["thread_id"] == mapping[root["id"]]["id"]
    assert "latest" in mapping[root["id"]]["body"] and "msg 123" in mapping[root["id"]]["body"]
    assert "/v1/storage/alice/design.png" in mapping[root["id"]]["body"]
    assert mapping[root["id"]]["edited_at"] is not None
    assert mapping[root["id"]]["reactions"][0]["by"][0]["external_id"] == "reactor"
    assert target.op.doc(dest)["body"] == "Current doc"
    assert alice.storage_get("design.png") == b"storage image"
    new_att = mapping[reply["id"]]["attachments"][0]
    assert target.op.download(new_att) == b"attachment bytes"
    image_record = next(a for a in manifest["attachments"] if a["filename"] == "screen.png")
    assert image_record["id"] not in mapping[root["id"]]["body"]
    conn = db.connect(target.data_dir / "board.db")
    try:
        ghost = conn.execute("SELECT * FROM principals WHERE external_id = 'missing-agent'").fetchone()
        assert ghost["disabled"] and ghost["display_name"] == "Missing agent"
        assert not conn.execute("SELECT 1 FROM credentials WHERE principal_id = ?", (ghost["id"],)).fetchone()
        assert not conn.execute("SELECT 1 FROM message_mentions").fetchone()
        assert not conn.execute("SELECT 1 FROM message_revisions").fetchone()
        image_id = conn.execute("SELECT id FROM attachments WHERE filename = 'screen.png'").fetchone()[0]
        assert target.op.download(image_id) == b"image bytes"
    finally:
        conn.close()
    for client in [target.op, alice]:
        assert client.activity_page(channels="joined")["messages"] == []
        with target.http(client.token) as http:
            assert http.get("/activity", params={"reactions":"true"}).json().get("reactions", []) == []
        listing = next(c for c in client.list_channels() if c["id"] == dest)
        assert listing["unread"] == listing["for_me"] == listing["threads_for_me"] == 0
    assert target.op.search("latest", channel=dest)["total"] == 1
    # A listener already following all target-channel traffic never sees history.
    assert alice.activity_page(after=0, channels=[dest])["messages"] == []
    replay = load(target, dest, data).json()
    assert replay["already_imported"] and all(n == 0 for n in replay["would_create"].values())
    assert len(target.op.read(dest)["messages"]) == 3
    # Live posts still work and appear after history, even if their clocks differ.
    live = target.op.post(dest, "live @alice")
    assert live["seq"] > max(m["seq"] for m in messages)
    assert [m["id"] for m in alice.activity_page(after=0, channels=[dest])["messages"]] == [live["id"]]
    assert load(target, dest, data).json()["already_imported"]


def test_import_does_not_wake_activity_notifier(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "@all quiet")
    dest = target.op.create_channel("imported")["id"]
    _, _, listener = target.human("listener")
    ready = threading.Event()
    def poll():
        ready.set()
        start = time.monotonic()
        result = listener.activity_page(after=0, channels=[dest], wait=1.5)
        return time.monotonic() - start, result
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(poll)
        assert ready.wait(1)
        time.sleep(0.15)
        assert load(target, dest, export(source, ch).content).status_code == 200
        elapsed, activity = waiting.result(timeout=5)
    assert elapsed >= 1.3 and activity["messages"] == []


def test_blocked_nonempty_doc_changed_bundle_and_handle_conflict(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "one")
    data = export(source, ch).content
    dest = target.op.create_channel("occupied")["id"]
    target.op.post(dest, "keep me")
    report = load(target, dest, data, dry=True).json()
    assert not report["can_import"] and "empty" in report["blockers"][0]
    assert load(target, dest, data).status_code == 409
    assert not load(target, dest, data, dry=True, keep_target_doc=True).json()["can_import"]
    assert load(target, dest, data, keep_target_doc=True).status_code == 409
    assert target.op.read(dest)["messages"][0]["body"] == "keep me"
    documented = target.op.create_channel("documented")["id"]
    target.op.save_doc(documented, "do not replace", base_version=0)
    assert load(target, documented, data).status_code == 409
    empty = target.op.create_channel("empty")["id"]
    assert load(target, empty, data).status_code == 200
    source.op.post(ch, "newer snapshot")
    assert load(target, empty, export(source, ch).content).status_code == 409
    # A handle cannot silently map a human author to an agent account.
    other = source.op.create_channel("authors")["id"]
    _, _, person = source.human("collision")
    person.post(other, "human")
    target.agent("collision")
    clean = target.op.create_channel("clean")["id"]
    assert not load(target, clean, export(source, other).content, dry=True).json()["can_import"]


@pytest.mark.parametrize("has_target_doc", [False, True])
def test_keep_target_doc_preserves_versions_and_replay(pair, has_target_doc):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "history message")
    source.op.storage_put("old-doc.txt", b"old document file")
    source.op.save_doc(ch, "[old](/v1/storage/operator/old-doc.txt)", base_version=0)
    data = export(source, ch).content
    dest = target.op.create_channel("target")["id"]
    if has_target_doc:
        target.op.save_doc(dest, "earlier summary", base_version=0)
        target.op.save_doc(dest, "new target summary", base_version=1)
    before = target.op.doc(dest)
    dry = load(target, dest, data, dry=True, keep_target_doc=True).json()
    assert dry["can_import"], dry
    assert dry["document"] == {"action": "keep_target", "target_version": before["version"]}
    assert dry["counts"]["doc"] == 1 and dry["would_create"]["doc"] == 0
    assert target.op.doc(dest) == before
    assert target.op.read(dest)["messages"] == []
    result = load(target, dest, data, keep_target_doc=True)
    assert result.status_code == 200, result.text
    assert target.op.doc(dest) == before
    assert target.op.storage_get("old-doc.txt") == b"old document file"
    assert len(target.op.read(dest)["messages"]) == 1
    # A retry without the option must not import a previously skipped doc.
    replay = load(target, dest, data).json()
    assert replay["already_imported"] and replay["would_create"]["doc"] == 0
    assert replay["document"]["action"] == "keep_target"
    assert target.op.doc(dest) == before
    if has_target_doc:
        with target.http(target.operator_token) as client:
            versions = client.get(f"/channels/{dest}/doc/versions").json()["versions"]
        assert [v["version"] for v in versions] == [2, 1]


def test_admin_public_only_and_restart_remains_quiet(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "@all @operator secret old mention")
    _, token, _ = source.human("nonadmin")
    assert export(source, ch, token).status_code == 403
    private = source.op.create_channel("private", private=True)["id"]
    assert export(source, private).status_code == 400
    data = export(source, ch).content
    dest = target.op.create_channel("target")["id"]
    _, token, _ = target.human("nonadmin")
    assert load(target, dest, data, token=token).status_code == 403
    private = target.op.create_channel("private", private=True)["id"]
    assert load(target, private, data).status_code == 400
    assert load(target, dest, data).status_code == 200
    # Re-running maintenance cannot backfill mentions for imported messages.
    conn = db.connect(target.data_dir / "board.db")
    conn.execute("DELETE FROM settings WHERE key = 'mentions_v2'")
    conn.close()
    target.restart()
    assert target.op.activity_page(channels=[dest])["messages"] == []
    conn = db.connect(target.data_dir / "board.db")
    assert not conn.execute("SELECT 1 FROM message_mentions").fetchone()
    conn.close()


@pytest.mark.parametrize("change", ["checksum", "length", "missing_blob", "extra_path", "duplicate_member", "cycle", "missing_parent", "nan", "author", "version"])
def test_invalid_bundle_has_no_effect(pair, change):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    att = source.op.upload(ch, b"file bytes", filename="file.txt")
    source.op.post(ch, "body", attachments=[att["id"]])
    manifest, blobs = unpack(export(source, ch).content)
    if change == "checksum":
        blobs[next(iter(blobs))] = b"wrongbytes"
    elif change == "length":
        manifest["attachments"][0]["size"] += 1
    elif change == "missing_blob":
        blobs.clear()
    elif change == "extra_path":
        blobs["../escape"] = b"not extracted"
    elif change == "cycle":
        manifest["messages"][0]["reply_to"] = manifest["messages"][0]["id"]
    elif change == "missing_parent":
        manifest["messages"][0]["reply_to"] = 99999
    elif change == "nan":
        manifest["messages"][0]["created_at"] = float("nan")
    elif change == "author":
        manifest["messages"][0]["sender_id"] = 987
    elif change == "version":
        manifest["version"] = 99
    data = pack(manifest, blobs)
    if change == "duplicate_member":
        out = io.BytesIO(data)
        with zipfile.ZipFile(out, "a") as z, pytest.warns(UserWarning):
            z.writestr("manifest.json", json.dumps(manifest))
        data = out.getvalue()
    dest = target.op.create_channel("target")["id"]
    before = target.op.people()
    result = load(target, dest, data)
    assert result.status_code == 400, result.text
    assert target.op.read(dest)["messages"] == []
    assert target.op.people() == before


def test_copy_failure_rolls_back_and_retry_is_safe(pair, monkeypatch):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, agent = source.agent("ghost")
    att = agent.upload(ch, b"file bytes", filename="file.txt")
    agent.post(ch, "body", attachments=[att["id"]])
    data = export(source, ch).content
    dest = target.op.create_channel("target")["id"]
    from msgboard.blobs import LocalBlobStore
    original = LocalBlobStore.commit
    def broken(self, *args):
        raise OSError("review disk failure")
    with monkeypatch.context() as patch:
        patch.setattr(LocalBlobStore, "commit", broken)
        assert load(target, dest, data).status_code == 500
    assert target.op.read(dest)["messages"] == []
    assert not any(p["external_id"] == "ghost" for p in target.op.people())
    assert load(target, dest, data).status_code == 200
    assert LocalBlobStore.commit == original


def test_concurrent_imports_publish_once(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "only once")
    dest = target.op.create_channel("target")["id"]
    data = export(source, ch).content
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: load(target, dest, data), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert sorted(r.json()["already_imported"] for r in results) == [False, True]
    assert len(target.op.read(dest)["messages"]) == 1
    target.op.delete_channel(dest, "target")


@pytest.mark.parametrize("keep_target_doc", [False, True])
def test_copy_script_defaults_to_dry_run(pair, tmp_path, keep_target_doc):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "CLI history")
    source.op.save_doc(ch, "source doc", base_version=0)
    dest = target.op.create_channel("target")["id"]
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/transfer_channel.py"),
               "--source", source.url, "--source-channel", str(ch),
               "--source-token-file", str(source.data_dir / "operator.token"),
               "--target", target.url, "--target-channel", str(dest),
               "--target-token-file", str(target.data_dir / "operator.token"), "--bundle"]
    if keep_target_doc:
        target.op.save_doc(dest, "target doc", base_version=0)
        command.insert(-1, "--keep-target-doc")
    result = subprocess.run([*command, str(tmp_path / "dry.zip")], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"]}, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"]
    assert target.op.read(dest)["messages"] == []
    source.op.post(ch, "written after the dry-run; must not enter the approved import")
    result = subprocess.run([*command, str(tmp_path / "dry.zip"), "--apply"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"]}, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert not json.loads(result.stdout)["dry_run"]
    assert target.op.read(dest)["messages"][0]["body"] == "CLI history"
    assert len(target.op.read(dest)["messages"]) == 1
    assert target.op.doc(dest)["body"] == ("target doc" if keep_target_doc else "source doc")


def test_history_is_read_for_new_people_even_with_future_source_clock(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, source_alice = source.human("alice")
    root = source_alice.post(ch, "future root")
    source.op.post(ch, "future reply", reply_to=root["id"])
    conn = db.connect(source.data_dir / "board.db")
    conn.execute("UPDATE messages SET created_at = ?", (time.time() + 86400,))
    conn.close()
    _, _, alice = target.human("alice")
    dest = target.op.create_channel("target")["id"]
    assert load(target, dest, export(source, ch).content).status_code == 200
    _, _, newcomer = target.human("newcomer")
    for client in [alice, newcomer]:
        listing = next(c for c in client.list_channels() if c["id"] == dest)
        assert listing["unread"] == listing["for_me"] == 0
    imported_root = target.op.read(dest)["messages"][0]
    live = target.op.post(dest, "new reply", reply_to=imported_root["id"])
    assert [m["id"] for m in alice.activity_page(after=0)["messages"]] == [live["id"]]
    assert alice.read(dest)["messages"][0]["replies"]["for_me_unread"] == 1


def test_v28_migrates_real_v27_and_preserves_messages_and_board_id(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    with monkeypatch.context() as old:
        old.setattr(db, "SCHEMA_VERSION", 27)
        old.setattr(db, "MIGRATIONS", db.MIGRATIONS[:27])
        db.migrate(conn, path)
    from msgboard import store
    _, token = store.create_operator(conn, 0)
    p = store.authenticate(conn, token, 1)
    ch, _ = store.create_channel(conn, p, "legacy", None, False, 1)
    mid = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
        VALUES (?, 1, ?, 'old live message', 'old', '', 123)""", (ch["id"], p.id)).lastrowid
    assert db.migrate(conn, path) == 28
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    assert row["body"] == "old live message" and row["created_at"] == 123
    assert row["imported"] == 0 and row["import_source"] is None
    board_id = conn.execute("SELECT value FROM settings WHERE key = 'transfer_board_id'").fetchone()[0]
    assert len(board_id) == 32
    db.migrate(conn, path)
    assert conn.execute("SELECT value FROM settings WHERE key = 'transfer_board_id'").fetchone()[0] == board_id
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_storage_links_copy_ghost_owner_doc_absolute_and_encoded_paths(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, owner = source.agent("file-owner", display_name="File owner")
    owner.storage_put("design/a b.png", b"image", content_type="image/png")
    owner.storage_put("notes/doc.txt", b"doc file")
    relative = "/v1/storage/file-owner/design/a%20b.png"
    foreign = "https://other.example/v1/storage/file-owner/external.png"
    source.op.post(ch, f"![relative]({relative}) [absolute]({source.url}{relative}) [external]({foreign})")
    source.op.save_doc(ch, f"[doc]({source.url}/v1/storage/file-owner/notes/doc.txt)", base_version=0)
    data = export(source, ch).content
    manifest, files = unpack(data)
    assert len(manifest["storage_files"]) == len(files) == 2
    dest = target.op.create_channel("target")["id"]
    dry = load(target, dest, data, dry=True).json()
    assert dry["counts"]["ghosts"] == 1 and dry["would_create"]["storage_files"] == 2
    assert not any(p["external_id"] == "file-owner" for p in target.op.people())
    result = load(target, dest, data)
    assert result.status_code == 200, result.text
    assert target.op.storage_get("design/a b.png", handle="file-owner") == b"image"
    assert target.op.storage_get("notes/doc.txt", handle="file-owner") == b"doc file"
    body = target.op.read(dest)["messages"][0]["body"]
    assert body.count(relative) == 2 and source.url not in body and foreign in body
    assert target.op.doc(dest)["body"] == "[doc](/v1/storage/file-owner/notes/doc.txt)"
    ghost = next(p for p in target.op.people() if p["external_id"] == "file-owner")
    assert ghost["disabled"]
    token = target.op.rotate_token(ghost["id"])["token"]
    with pytest.raises(BoardError) as exc:
        target.client(token).me()
    assert exc.value.status == 401
    assert load(target, dest, data).json()["already_imported"]


@pytest.mark.parametrize("existing", ["different", "identical", "corrupt"])
def test_storage_collision_reuse_and_corrupt_target(pair, existing):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.storage_put("shared.txt", b"source bytes")
    source.op.post(ch, "[file](/v1/storage/operator/shared.txt)")
    target.op.storage_put("shared.txt", b"other bytes" if existing == "different" else b"source bytes")
    if existing == "corrupt":
        (target.data_dir / "storage" / str(target.op.me()["id"]) / "shared.txt").write_bytes(b"bad bucket bytes")
    dest = target.op.create_channel("target")["id"]
    data = export(source, ch).content
    dry = load(target, dest, data, dry=True).json()
    assert dry["can_import"] == (existing == "identical")
    applied = load(target, dest, data)
    if existing == "identical":
        assert applied.status_code == 200 and dry["would_create"]["storage_files"] == 0
        assert dry["storage_files"][0]["action"] == "reuse"
    else:
        assert applied.status_code == 409
        assert target.op.read(dest)["messages"] == []


def test_storage_missing_source_and_changed_source_bytes_fail_export(pair):
    source, _ = pair
    ch = source.op.create_channel("history")["id"]
    source.op.post(ch, "[missing](/v1/storage/operator/missing.txt)")
    assert export(source, ch).status_code == 409
    source.op.storage_put("missing.txt", b"original")
    path = source.data_dir / "storage" / str(source.op.me()["id"]) / "missing.txt"
    path.write_bytes(b"changed behind the index")
    assert export(source, ch).status_code == 409


def test_storage_aggregate_quota_blocks_import_and_parallel_uploads(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    for name in ["a.bin", "b.bin"]:
        source.op.storage_put(name, b"x" * (600 * 1024))
        source.op.post(ch, f"[file](/v1/storage/operator/{name})")
    target.op.update_settings(storage_max_file_mb=1, storage_quota_mb=1)
    dest = target.op.create_channel("target")["id"]
    dry = load(target, dest, export(source, ch).content, dry=True).json()
    assert not dry["can_import"] and any("quota" in b for b in dry["blockers"])
    assert target.op.storage_list()["used_bytes"] == 0
    def upload(name):
        with target.http(target.operator_token) as client:
            return client.put(f"/storage/operator/{name}", content=b"x" * (600 * 1024)).status_code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(upload, ["first.bin", "second.bin"]))
    assert sorted(statuses) == [201, 413]
    assert target.op.storage_list()["used_bytes"] == 600 * 1024


def test_storage_links_follow_mapped_handle_case(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    _, _, owner = source.agent("Owner")
    target.agent("owner")
    owner.storage_put("file.txt", b"mapped owner bytes")
    source.op.post(ch, "[file](/v1/storage/Owner/file.txt)")
    data = export(source, ch).content
    dest = target.op.create_channel("target")["id"]
    assert load(target, dest, data).status_code == 200
    assert target.op.read(dest)["messages"][0]["body"] == "[file](/v1/storage/owner/file.txt)"
    assert target.op.storage_get("file.txt", handle="owner") == b"mapped owner bytes"


def test_bundle_cannot_omit_linked_storage_data(pair):
    source, target = pair
    ch = source.op.create_channel("history")["id"]
    source.op.storage_put("file.txt", b"bytes")
    source.op.post(ch, "[file](/v1/storage/operator/file.txt)")
    manifest, _ = unpack(export(source, ch).content)
    manifest["storage_files"] = []
    dest = target.op.create_channel("target")["id"]
    assert load(target, dest, pack(manifest, {})).status_code == 400
    assert target.op.read(dest)["messages"] == []
