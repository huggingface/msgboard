"""Admin-only, one-channel history transfer. ZIP members are read, never extracted.

The database transaction owns the import decision and publication. Immutable blob
files are installed before their rows commit; a failed transaction may leave an
unreferenced object, but never publishes a row whose bytes are incomplete.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from . import store
from .config import LIMITS
from .errors import ApiError, bad_request, conflict, forbidden

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_ATTACHMENTS = 1_000
HASH = re.compile(r"^[0-9a-f]{64}$")
ATT_LINK = re.compile(r"(?:/v1/attachments/|#file/)(att_[A-Za-z0-9_-]+)")


def require_admin(p):
    if not p.is_admin:
        raise forbidden("channel export/import requires an admin")


def public_channel(conn, p, channel_id):
    require_admin(p)
    ch = store.channel_for(conn, p, channel_id)
    if ch["kind"] != "public":
        raise bad_request("only public channels can be exported or imported")
    return ch


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def export_manifest(conn, p, channel_id: int, board_url: str):
    # Reader connections otherwise run in autocommit mode: use one snapshot for
    # authors, messages, reactions, doc and attachment metadata.
    conn.execute("BEGIN")
    try:
        ch = public_channel(conn, p, channel_id)
        rows = conn.execute("SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at, seq, id",
                            (channel_id,)).fetchall()
        if len(rows) > MAX_MESSAGES:
            raise ApiError(413, "too_large", f"a channel bundle supports at most {MAX_MESSAGES} messages")
        messages = [{k: r[k] for k in ("id", "seq", "sender_id", "body", "reply_to", "thread_id",
                                      "created_at", "edited_at")} | {"attachments": json.loads(r["attachments"] or "[]")}
                    for r in rows]
        reactions = [dict(r) for r in conn.execute("""SELECT r.* FROM message_reactions r
            JOIN messages m ON m.id = r.message_id WHERE m.channel_id = ? ORDER BY r.created_at, r.rowid""", (channel_id,))]
        doc_row = conn.execute("SELECT body, author_id, created_at FROM channel_doc_versions WHERE channel_id = ? AND version = ?",
                               (channel_id, ch["doc_version"])).fetchone()
        doc = dict(doc_row) if doc_row else None
        attachment_ids = {a for m in messages for a in m["attachments"]}
        for body in [m["body"] for m in messages] + ([doc["body"]] if doc else []):
            # Only this channel's attachments are copied. Links to other channels
            # retain their original meaning/access rules, like storage links.
            for aid in ATT_LINK.findall(body):
                if conn.execute("SELECT 1 FROM attachments WHERE id = ? AND channel_id = ? AND state = 'ready'",
                                (aid, channel_id)).fetchone():
                    attachment_ids.add(aid)
        if len(attachment_ids) > MAX_ATTACHMENTS:
            raise ApiError(413, "too_large", "too many attachments for one bundle")
        attachments = []
        for aid in sorted(attachment_ids):
            row = conn.execute("SELECT * FROM attachments WHERE id = ? AND channel_id = ? AND state = 'ready'",
                               (aid, channel_id)).fetchone()
            if not row:
                raise conflict("export_changed", "an attachment is missing; export again after checking the channel")
            attachments.append(dict(row))
        people = {m["sender_id"] for m in messages} | {r["principal_id"] for r in reactions}
        people |= {a["uploader_id"] for a in attachments}
        if doc:
            people.add(doc["author_id"])
        principals = [dict(conn.execute("SELECT id, external_id, display_name, identity_type FROM principals WHERE id = ?",
                                       (pid,)).fetchone()) for pid in sorted(people)]
        manifest = {"format": "msgboard-channel", "version": 1,
                    "source": {"board_id": conn.execute("SELECT value FROM settings WHERE key = 'transfer_board_id'").fetchone()[0],
                               "board_url": board_url.rstrip("/"), "channel_id": channel_id, "channel_name": ch["name"]},
                    "principals": principals, "messages": messages, "reactions": reactions,
                    "doc": doc, "attachments": [{k: a[k] for k in ("id", "uploader_id", "filename", "content_type",
                                                                 "size", "sha256", "created_at")} for a in attachments]}
        return manifest, {a["sha256"]: a["storage_key"] for a in attachments}
    finally:
        conn.execute("ROLLBACK")


def build_bundle(manifest, keys, blobs, path: Path):
    validate_manifest(manifest)
    data = canonical(manifest)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ApiError(413, "too_large", "channel manifest is too large")
    total = len(data)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", data)
        for sha, key in keys.items():
            digest = hashlib.sha256()
            try:
                with blobs.path_for(key).open("rb") as src, z.open("blobs/" + sha, "w") as dst:
                    while chunk := src.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_BUNDLE_BYTES:
                            raise ApiError(413, "too_large", "channel bundle exceeds 512 MiB")
                        digest.update(chunk)
                        dst.write(chunk)
            except FileNotFoundError:
                raise conflict("export_changed", "attachment bytes disappeared; retry the export")
            if digest.hexdigest() != sha:
                raise conflict("export_changed", "attachment bytes do not match their recorded checksum")


def _int(value, what):
    if type(value) is not int or value < 1:
        raise bad_request(f"{what} must be a positive integer")
    return value


def _time(value, what):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise bad_request(f"{what} must be a finite UTC timestamp")


def _text(value, what, max_bytes=LIMITS.max_body_bytes):
    if not isinstance(value, str) or len(value.encode()) > max_bytes:
        raise bad_request(f"invalid or oversized {what}")


def validate_manifest(m):
    if not isinstance(m, dict) or m.get("format") != "msgboard-channel" or m.get("version") != 1:
        raise bad_request("expected a version 1 msgboard-channel bundle")
    source = m.get("source")
    if not isinstance(source, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(source.get("board_id", ""))):
        raise bad_request("invalid source board ID")
    _int(source.get("channel_id"), "source channel ID")
    _text(source.get("board_url"), "source URL", 2048)
    url = urlsplit(source["board_url"])
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise bad_request("source URL must be an HTTP(S) board address")
    for field, limit in [("principals", MAX_MESSAGES + MAX_ATTACHMENTS), ("messages", MAX_MESSAGES),
                         ("reactions", MAX_MESSAGES * 100), ("attachments", MAX_ATTACHMENTS)]:
        if not isinstance(m.get(field), list) or len(m[field]) > limit or any(not isinstance(x, dict) for x in m[field]):
            raise bad_request(f"invalid or oversized {field} list")
    people = {}
    handles = set()
    for p in m["principals"]:
        p.setdefault("display_name", None)
        pid = _int(p.get("id"), "author ID")
        handle = p.get("external_id")
        if not isinstance(handle, str) or not store.EXTERNAL_ID.fullmatch(handle) or handle.lower() in handles or pid in people:
            raise bad_request("duplicate or invalid author handle/ID")
        if p.get("identity_type") not in ("user", "agent", "system"):
            raise bad_request("invalid author type")
        if p.get("display_name") is not None:
            _text(p["display_name"], "display name", 1024)
        people[pid] = p
        handles.add(handle.lower())
    attachments = {}
    for a in m["attachments"]:
        a.setdefault("filename", None)
        a.setdefault("content_type", None)
        aid = a.get("id")
        if not isinstance(aid, str) or not re.fullmatch(r"att_[A-Za-z0-9_-]{1,120}", aid) or aid in attachments:
            raise bad_request("duplicate or invalid attachment ID")
        if a.get("uploader_id") not in people or not HASH.fullmatch(str(a.get("sha256", ""))):
            raise bad_request("invalid attachment author or checksum")
        if type(a.get("size")) is not int or not 0 <= a["size"] <= LIMITS.max_upload_bytes:
            raise bad_request("invalid attachment size")
        for field, limit in [("filename", 1024), ("content_type", 128)]:
            if a.get(field) is not None:
                _text(a[field], field, limit)
        _time(a.get("created_at"), "attachment timestamp")
        attachments[aid] = a
    messages = {}
    seqs = set()
    for msg in m["messages"]:
        msg.setdefault("reply_to", None)
        msg.setdefault("edited_at", None)
        mid = _int(msg.get("id"), "message ID")
        seq = _int(msg.get("seq"), "message sequence")
        if mid in messages or seq in seqs or msg.get("sender_id") not in people:
            raise bad_request("duplicate message ID/sequence or missing author")
        _text(msg.get("body"), "message body")
        _time(msg.get("created_at"), "message timestamp")
        if msg.get("edited_at") is not None:
            _time(msg["edited_at"], "edit timestamp")
        if not isinstance(msg.get("attachments"), list) or len(msg["attachments"]) > LIMITS.max_attachments_per_message:
            raise bad_request("invalid message attachments")
        if any(not isinstance(aid, str) or aid not in attachments for aid in msg["attachments"]):
            raise bad_request("message refers to a missing attachment")
        messages[mid] = msg
        seqs.add(seq)
    # Parent links, not untrusted thread_id, define the thread. Validate the whole
    # graph before any database writes, including parents with later timestamps.
    for mid, msg in messages.items():
        seen = {mid}
        parent = msg.get("reply_to")
        while parent is not None:
            if type(parent) is not int or parent not in messages or parent in seen:
                raise bad_request("missing parent or cyclic thread")
            seen.add(parent)
            parent = messages[parent].get("reply_to")
    reactions = set()
    for r in m["reactions"]:
        if r.get("message_id") not in messages or r.get("principal_id") not in people:
            raise bad_request("reaction refers to a missing message or author")
        store._valid_emoji(r.get("emoji"))
        _time(r.get("created_at"), "reaction timestamp")
        key = (r["message_id"], r["principal_id"], r["emoji"])
        if key in reactions:
            raise bad_request("duplicate reaction")
        reactions.add(key)
    doc = m.setdefault("doc", None)
    if doc is not None:
        if not isinstance(doc, dict) or doc.get("author_id") not in people:
            raise bad_request("invalid channel doc author")
        _text(doc.get("body"), "channel doc", store.MAX_DOC_BYTES)
        _time(doc.get("created_at"), "doc timestamp")
    return m


def read_bundle(path: Path, stage: Path):
    try:
        with zipfile.ZipFile(path) as z:
            entries = z.infolist()
            names = [e.filename for e in entries]
            if len(names) != len(set(names)) or len(names) > MAX_ATTACHMENTS + 1:
                raise bad_request("duplicate ZIP members or too many files")
            if sum(e.file_size for e in entries) > MAX_BUNDLE_BYTES:
                raise ApiError(413, "too_large", "expanded bundle exceeds 512 MiB")
            if z.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise ApiError(413, "too_large", "manifest exceeds 32 MiB")
            m = validate_manifest(json.loads(z.read("manifest.json")))
            hashes = {a["sha256"] for a in m["attachments"]}
            if set(names) != {"manifest.json"} | {"blobs/" + sha for sha in hashes}:
                raise bad_request("ZIP members must exactly match the manifest's blobs")
            files = {}
            for sha in hashes:
                expected = {a["size"] for a in m["attachments"] if a["sha256"] == sha}
                info = z.getinfo("blobs/" + sha)
                if expected != {info.file_size}:
                    raise bad_request("attachment length does not match manifest")
                dest = stage / sha
                h = hashlib.sha256()
                with z.open(info) as src, dest.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        h.update(chunk)
                        dst.write(chunk)
                if h.hexdigest() != sha:
                    raise bad_request("attachment checksum does not match manifest")
                files[sha] = dest
            return m, hashlib.sha256(canonical(m)).hexdigest(), files
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, UnicodeError, RuntimeError) as exc:
        raise bad_request("invalid channel bundle") from exc


def plan_import(conn, p, channel_id, m, digest):
    ch = public_channel(conn, p, channel_id)
    existing = conn.execute("SELECT * FROM channel_imports WHERE channel_id = ?", (channel_id,)).fetchone()
    blockers, mappings = [], []
    replay = bool(existing and existing["digest"] == digest)
    if existing and not replay:
        blockers.append("target already holds a different import; incremental imports are not supported")
    if not existing:
        if conn.execute("SELECT 1 FROM messages WHERE channel_id = ? LIMIT 1", (channel_id,)).fetchone():
            blockers.append("initial import requires an empty target channel")
        if ch["doc_version"]:
            blockers.append("initial import requires a target with no channel doc")
    if ch["archived"]:
        blockers.append("target channel is archived")
    for author in m["principals"]:
        rows = conn.execute("SELECT * FROM principals WHERE lower(external_id) = lower(?)", (author["external_id"],)).fetchall()
        if len(rows) > 1 or (rows and rows[0]["identity_type"] != author["identity_type"]):
            blockers.append(f"ambiguous or different-type handle: {author['external_id']}")
        mappings.append({"source_id": author["id"], "handle": author["external_id"],
                         "display_name": author["display_name"], "target_id": rows[0]["id"] if len(rows) == 1 else None,
                         "action": "existing" if rows else "ghost"})
    counts = {"messages": len(m["messages"]), "attachments": len(m["attachments"]),
              "bytes": sum(a["size"] for a in m["attachments"]), "reactions": len(m["reactions"]),
              "ghosts": sum(a["action"] == "ghost" for a in mappings), "doc": int(m["doc"] is not None)}
    return {"source": m["source"], "target_channel_id": channel_id, "digest": digest,
            "already_imported": replay, "can_import": not blockers, "blockers": blockers,
            "counts": counts, "would_create": {k: 0 if replay else v for k, v in counts.items()}, "authors": mappings}


def _rewrite_attachments(body, source_url, ids):
    # Absolute links to this source become target-local links. Foreign/storage
    # links and old textual "msg N" references are deliberately untouched.
    for old, new in ids.items():
        body = body.replace(source_url.rstrip("/") + "/v1/attachments/" + old, "/v1/attachments/" + new)
        body = re.sub(r"(?<![A-Za-z0-9_./:-])/v1/attachments/" + re.escape(old) + r"(?![A-Za-z0-9_-])",
                      "/v1/attachments/" + new, body)
        body = re.sub(r"#file/" + re.escape(old) + r"(?![A-Za-z0-9_-])", "#file/" + new, body)
    return body


def import_channel(conn, p, channel_id, m, digest, files, blobs, now):
    report = plan_import(conn, p, channel_id, m, digest)
    if not report["can_import"]:
        raise ApiError(409, "import_blocked", "channel import is blocked", details=report)
    if report["already_imported"]:
        return report
    people = {}
    for mapping in report["authors"]:
        if mapping["target_id"] is None:
            author = next(a for a in m["principals"] if a["id"] == mapping["source_id"])
            cursor = conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role,
                display_name, disabled, created_at, created_by, joined_via)
                VALUES ('import', ?, ?, ?, ?, 1, ?, ?, 'import')""",
                (author["external_id"], author["identity_type"], "viewer" if author["identity_type"] == "user" else "guest",
                 author["display_name"], now, p.id))
            mapping["target_id"] = cursor.lastrowid
        people[mapping["source_id"]] = mapping["target_id"]
    source = m["source"]
    aid_map = {}
    for a in m["attachments"]:
        key = canonical([channel_id, source["board_id"], source["channel_id"], a["id"]])
        aid = "att_" + hashlib.sha256(key).hexdigest()[:32]
        aid_map[a["id"]] = aid
        storage_key = f"{aid[4:6]}/{aid}"
        temp, fh = blobs.open_temp()
        try:
            with files[a["sha256"]].open("rb") as src:
                shutil.copyfileobj(src, fh)
            blobs.finish_temp(fh)
            blobs.commit(temp, storage_key)
        finally:
            if not fh.closed:
                fh.close()
            blobs.discard(temp)
        conn.execute("""INSERT INTO attachments (id, channel_id, uploader_id, upload_id, storage_key,
            filename, content_type, size, sha256, state, published, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 1, ?)""",
            (aid, channel_id, people[a["uploader_id"]], "import:" + aid, storage_key, a["filename"],
             a["content_type"], a["size"], a["sha256"], a["created_at"]))
    seq = conn.execute("SELECT next_seq FROM channels WHERE id = ?", (channel_id,)).fetchone()[0]
    ids = {}
    ordered = sorted(m["messages"], key=lambda msg: (msg["created_at"], msg["seq"], msg["id"]))
    for msg in ordered:
        origin = {"board_url": source["board_url"], "channel_id": source["channel_id"], "message_id": msg["id"]}
        cursor = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, attachments,
            request_id, payload_hash, created_at, edited_at, imported, import_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (channel_id, seq, people[msg["sender_id"]], _rewrite_attachments(msg["body"], source["board_url"], aid_map),
             json.dumps([aid_map[a] for a in msg["attachments"]]),
             "import:" + hashlib.sha256(canonical([channel_id, source["board_id"], source["channel_id"], msg["id"]])).hexdigest(),
             digest, msg["created_at"], msg["edited_at"], json.dumps(origin)))
        ids[msg["id"]] = cursor.lastrowid
        seq += 1
    by_id = {msg["id"]: msg for msg in ordered}
    for msg in ordered:
        parent = msg["reply_to"]
        if parent is not None:
            root = parent
            while by_id[root]["reply_to"] is not None:
                root = by_id[root]["reply_to"]
            conn.execute("UPDATE messages SET reply_to = ?, thread_id = ? WHERE id = ?",
                         (ids[parent], ids[root], ids[msg["id"]]))
    for r in m["reactions"]:
        conn.execute("INSERT INTO message_reactions (message_id, principal_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                     (ids[r["message_id"]], people[r["principal_id"]], r["emoji"], r["created_at"]))
    if m["doc"] is not None:
        doc = m["doc"]
        conn.execute("INSERT INTO channel_doc_versions (channel_id, version, body, author_id, created_at) VALUES (?, 1, ?, ?, ?)",
                     (channel_id, _rewrite_attachments(doc["body"], source["board_url"], aid_map), people[doc["author_id"]], doc["created_at"]))
        conn.execute("UPDATE channels SET doc_version = 1 WHERE id = ?", (channel_id,))
    conn.execute("UPDATE channels SET next_seq = ? WHERE id = ?", (seq, channel_id))
    conn.execute("""INSERT INTO channel_reads (principal_id, channel_id, last_read_seq, thread_floor, updated_at)
        SELECT id, ?, ?, ?, ? FROM principals WHERE true
        ON CONFLICT (principal_id, channel_id) DO UPDATE SET
        last_read_seq = max(last_read_seq, excluded.last_read_seq), thread_floor = max(thread_floor, excluded.thread_floor)""",
                 (channel_id, seq - 1, seq - 1, now))
    conn.execute("INSERT INTO channel_imports VALUES (?, ?, ?, ?, ?, ?)",
                 (channel_id, source["board_id"], source["channel_id"], digest, json.dumps(report), now))
    store.audit(conn, p.id, "channel.import", f"channel:{channel_id}", {"source": source, "digest": digest, "counts": report["counts"]}, now)
    return report
