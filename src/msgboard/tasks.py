"""Personal task operations, run on the existing reader or single writer.

Answers and notifications are ordinary messages. Functions do not open or commit
transactions: the server runs each mutation in one Database.write transaction.
Live references become NULL on deletion; original IDs retain destination intent.
"""

from dataclasses import replace
from contextlib import contextmanager
import hashlib
import json
import time

from . import auth, store
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized


NOTICE_DELAY = 5.0
MAX_SOURCES = 20
MAX_OPTIONS = 20
MAX_BLOCKS = 40


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _integer(value, name, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise bad_request(f"{name} must be a positive integer")
    return value


def _object(payload, allowed):
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise bad_request("unsupported task fields or invalid request body")


def _text(value, name, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise bad_request(f"{name} must be {'at most' if empty else 'nonempty and at most'} {limit} characters")
    return value


def _current(conn, principal_id):
    row = conn.execute("SELECT * FROM principals WHERE id = ?", (principal_id,)).fetchone()
    if row is None or row["disabled"]:
        raise unauthorized("identity is no longer active")
    p = store._principal_obj(row)
    if not p.is_human:
        owner = conn.execute("SELECT * FROM principals WHERE id = ?", (p.owner_id,)).fetchone()
        # Ownership is normalized to a person by the existing identity migration.
        # Refuse malformed chains rather than allowing an agent to own a task.
        if (owner is None or owner["identity_type"] != "user" or owner["disabled"]
                or owner["role"] not in auth.owner_roles_for(p.role)):
            raise unauthorized("agent no longer has an active human owner")
    return p


def _principal(conn, p, now, *, write=False):
    cred = conn.execute("SELECT * FROM credentials WHERE id = ? AND principal_id = ?",
                        (p.credential_id, p.id)).fetchone()
    if (cred is None or cred["revoked_at"] is not None
            or (cred["expires_at"] is not None and cred["expires_at"] <= now)):
        raise unauthorized("credential is no longer active")
    fresh = replace(_current(conn, p.id), credential_id=p.credential_id)
    if write and not fresh.is_human and not fresh.may_post:
        raise forbidden("read-only agents cannot change tasks")
    return fresh


def _owner(p):
    return p.id if p.is_human else p.owner_id


def _channel_visible(conn, p, channel_id):
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return ch is not None and store.can_see(conn, p, ch)


def _sources(conn, item_id):
    return conn.execute("SELECT * FROM task_sources WHERE item_id = ? ORDER BY position", (item_id,)).fetchall()


def _visible(conn, p, row):
    if row["owner_id"] != _owner(p):
        return False
    if row["channel_id"] is not None and not _channel_visible(conn, p, row["channel_id"]):
        return False
    if not p.is_human and row["original_channel_id"] is not None and row["channel_id"] is None:
        return False
    for source in _sources(conn, row["id"]):
        if source["channel_id"] is not None and not _channel_visible(conn, p, source["channel_id"]):
            return False
        # Only the owner retains a card with deleted evidence, to close it safely.
        if not p.is_human and (source["message_id"] is None or source["channel_id"] is None):
            return False
    return True


def _row(conn, p, item_id):
    _integer(item_id, "item id")
    row = conn.execute("SELECT * FROM task_items WHERE id = ? AND owner_id = ?",
                       (item_id, _owner(p))).fetchone()
    if row is None or not _visible(conn, p, row):
        raise not_found("task not found")
    return row


def _message(conn, p, message_id):
    if message_id is None:
        return None
    row = conn.execute(store.MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()
    if row is None or not _channel_visible(conn, p, row["channel_id"]):
        return None
    out = store._message_json(row)
    store._attach_mentions(conn, [out])
    store._attach_reactions(conn, [out], p.id)
    return out


def _item(conn, p, row):
    refs = []
    for source in _sources(conn, row["id"]):
        message = _message(conn, p, source["message_id"])
        refs.append({"message_id": source["original_message_id"],
                     "channel_id": source["original_channel_id"], "message": message,
                     "unavailable": message is None})
    channel = None
    if row["channel_id"] is not None:
        ch = conn.execute("SELECT id, name, kind, archived FROM channels WHERE id = ?",
                          (row["channel_id"],)).fetchone()
        if ch is not None:
            channel = dict(ch)
    specified = row["original_channel_id"] is not None
    unavailable = specified and (row["channel_id"] is None
                                  or (row["original_anchor_id"] is not None and row["anchor_id"] is None))
    if specified and channel is not None and not store.can_post(conn, p, channel):
        unavailable = True
    if not specified:
        try:
            agent = _current(conn, row["agent_id"])
            unavailable = agent.is_human or agent.owner_id != row["owner_id"] or not agent.may_post
        except ApiError:
            unavailable = True
    out = {"id": row["id"], "key": row["item_key"], "title": row["title"],
           "description": row["description"], "blocks": json.loads(row["blocks"]),
           "agent": store._brief(conn, row["agent_id"]),
           "channel_id": row["original_channel_id"], "anchor_id": row["original_anchor_id"],
           "channel": channel, "sources": refs, "options": json.loads(row["options"]),
           "multiple": bool(row["multiple"]), "version": row["version"], "state": row["state"],
           "unread": row["state"] == "open" and not row["seen"],
           "closed_reason": row["closed_reason"], "closed_at": row["closed_at"],
           "answer": _message(conn, p, row["answer_id"]),
           "answer_message_id": row["original_answer_id"],
           "created_at": row["created_at"], "updated_at": row["updated_at"], "rank": row["rank"],
           "destination_unavailable": bool(unavailable),
           "destination": {"kind": "thread" if row["original_anchor_id"] is not None else "channel" if specified else "dm",
                           "channel_id": row["original_channel_id"], "anchor_id": row["original_anchor_id"],
                           "agent_id": row["agent_id"]}}
    return out


def _order_version(conn, owner_id):
    row = conn.execute("SELECT version FROM task_orders WHERE owner_id = ?", (owner_id,)).fetchone()
    return row[0] if row is not None else 0


def _bump_order(conn, owner_id):
    conn.execute("""INSERT INTO task_orders(owner_id, version) VALUES (?, 1)
                    ON CONFLICT(owner_id) DO UPDATE SET version = version + 1""", (owner_id,))


def _listing(conn, p, include_archived, sort, direction):
    if not isinstance(include_archived, bool):
        raise bad_request("include_archived must be a boolean")
    if sort not in ("manual", "latest", "channel", "agent") or direction not in ("asc", "desc"):
        raise bad_request("sort must be manual/latest/channel/agent; direction must be asc/desc")
    rows = conn.execute("SELECT * FROM task_items WHERE owner_id = ? ORDER BY rank, id", (_owner(p),)).fetchall()
    visible = [r for r in rows if _visible(conn, p, r)]
    counts = {"open": sum(r["state"] == "open" for r in visible),
              "archived": sum(r["state"] == "closed" for r in visible)}
    items = [_item(conn, p, r) for r in visible if include_archived or r["state"] == "open"]
    if sort == "manual":
        key = lambda i: (i["rank"], i["id"])
    elif sort == "latest":
        key = lambda i: (i["created_at"], i["id"])
    elif sort == "channel":
        key = lambda i: ((i["channel"] or {}).get("name", "").casefold(), i["rank"], i["id"])
    else:
        key = lambda i: ((i["agent"] or {}).get("display_name") or (i["agent"] or {}).get("external_id", ""),
                        i["rank"], i["id"])
    items.sort(key=key, reverse=direction == "desc")
    return {"items": items, "counts": counts, "order_version": _order_version(conn, _owner(p))}


@contextmanager
def _snapshot(conn):
    # SQLite autocommit would otherwise pair an old title/options row with a
    # newer set of public source references during an agent's concurrent edit.
    # Visibility and the content it protects must use the same read snapshot.
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if own:
            conn.execute("ROLLBACK")


def list_items(conn, p, include_archived=False, sort="manual", direction="asc"):
    with _snapshot(conn):
        return _listing(conn, _principal(conn, p, time.time()), include_archived, sort, direction)


def get_item(conn, p, item_id):
    with _snapshot(conn):
        p = _principal(conn, p, time.time())
        return _item(conn, p, _row(conn, p, item_id))


def _unread(conn, p):
    rows = conn.execute("SELECT * FROM task_items WHERE owner_id = ? AND state = 'open' AND seen = 0",
                        (_owner(p),)).fetchall()
    return {"unread": sum(_visible(conn, p, row) for row in rows)}


def unread(conn, p):
    with _snapshot(conn):
        return _unread(conn, _principal(conn, p, time.time()))


def mark_read(conn, p, payload, now):
    p = _principal(conn, p, now)
    if not p.is_human:
        raise forbidden("only the human can mark tasks read")
    _object(payload, {"ids"})
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > 200:
        raise bad_request("ids must be at most 200 task IDs")
    ids = list(dict.fromkeys(_integer(i, "task id") for i in ids))
    # Validate the complete batch before changing it. A concurrent new item is
    # never acknowledged by a timestamp or a list-wide high-water mark.
    for item_id in ids:
        _row(conn, p, item_id)
    if ids:
        marks = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE task_items SET seen = 1 WHERE owner_id = ? AND id IN ({marks})", (p.id, *ids))
    return _unread(conn, p)


def _notice(conn, owner_id, agent_id, now):
    pending = conn.execute("SELECT id FROM task_notices WHERE owner_id = ? AND agent_id = ? AND state = 'pending'",
                           (owner_id, agent_id)).fetchone()
    if pending is None:
        conn.execute("INSERT INTO task_notices(owner_id, agent_id, due_at, updated_at) VALUES (?, ?, ?, ?)",
                     (owner_id, agent_id, now + NOTICE_DELAY, now))
    else:
        conn.execute("UPDATE task_notices SET changes = changes + 1, updated_at = ? WHERE id = ?", (now, pending[0]))


def _content(conn, p, payload):
    _object(payload, {"title", "description", "blocks", "channel_id", "anchor_id", "sources", "options", "multiple", "base_version"})
    if "base_version" in payload:
        _integer(payload["base_version"], "base_version")
    title = _text(payload.get("title"), "title", 240)
    description = _text(payload.get("description", ""), "description", 16000, empty=True)
    channel_id = _integer(payload.get("channel_id"), "channel_id", optional=True)
    anchor_id = _integer(payload.get("anchor_id"), "anchor_id", optional=True)
    owner = _current(conn, p.owner_id)
    if anchor_id is not None:
        anchor = conn.execute("SELECT channel_id, thread_id FROM messages WHERE id = ?", (anchor_id,)).fetchone()
        if anchor is None:
            raise bad_request("anchor_id must reference an existing message")
        if channel_id is not None and channel_id != anchor["channel_id"]:
            raise bad_request("anchor_id must belong to channel_id")
        channel_id = anchor["channel_id"]
        # Keep the exact source anchor. Normal post_message resolves its root.
    if channel_id is not None:
        store.channel_for(conn, p, channel_id)
        store.channel_for(conn, owner, channel_id)
    sources = payload.get("sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise bad_request(f"sources must be at most {MAX_SOURCES} message IDs")
    sources = list(dict.fromkeys(_integer(s, "source message id") for s in sources))
    blocks = payload.get("blocks", [])
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise bad_request(f"blocks must be a list of at most {MAX_BLOCKS} text or quote blocks")
    ordered, text_length = [], len(description)
    for block in blocks:
        if not isinstance(block, dict):
            raise bad_request("blocks must contain text or quote objects")
        if block.get("type") == "text":
            _object(block, {"type", "text"})
            text = _text(block.get("text"), "block text", 16000, empty=True)
            text_length += len(text)
            ordered.append({"type": "text", "text": text})
        elif block.get("type") == "quote":
            _object(block, {"type", "message_id"})
            message_id = _integer(block.get("message_id"), "quote message_id")
            ordered.append({"type": "quote", "message_id": message_id})
            if message_id not in sources:
                sources.append(message_id)
        else:
            raise bad_request("block type must be text or quote")
    if text_length > 16000:
        raise bad_request("description and text blocks together must be at most 16000 characters")
    if len(sources) > MAX_SOURCES:
        raise bad_request(f"sources and quotes must reference at most {MAX_SOURCES} unique messages")
    refs = []
    for message_id in sources:
        source = conn.execute("SELECT channel_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if source is None:
            raise bad_request("sources must reference existing messages")
        store.channel_for(conn, p, source["channel_id"])
        store.channel_for(conn, owner, source["channel_id"])
        refs.append((message_id, source["channel_id"]))
    options = payload.get("options", [])
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise bad_request(f"options must be at most {MAX_OPTIONS} choices")
    choices, seen = [], set()
    for option in options:
        _object(option, {"id", "label"})
        option_id = option.get("id")
        if not isinstance(option_id, str) or not store.CLIENT_ID.fullmatch(option_id) or option_id in seen:
            raise bad_request("options need unique valid string IDs")
        choices.append({"id": option_id, "label": _text(option.get("label"), "option label", 8000)})
        seen.add(option_id)
    multiple = payload.get("multiple", False)
    if not isinstance(multiple, bool):
        raise bad_request("multiple must be a boolean")
    content = {"title": title, "description": description, "channel_id": channel_id,
               "anchor_id": anchor_id, "sources": sources, "options": choices, "multiple": multiple}
    # Omitting empty blocks preserves the exact v30 content fingerprint, so an
    # old stable-key retry remains a no-op after the feature/table rename.
    if ordered:
        content["blocks"] = ordered
    return content, refs


def put_item(conn, p, key, payload, now):
    p = _principal(conn, p, now, write=True)
    if p.is_human or p.identity_type not in ("agent", "system"):
        raise forbidden("only your own agents can add tasks")
    if not isinstance(key, str) or not store.CLIENT_ID.fullmatch(key):
        raise bad_request("task key must be a valid client ID")
    content, refs = _content(conn, p, payload)
    digest = _digest(content)
    row = conn.execute("SELECT * FROM task_items WHERE owner_id = ? AND agent_id = ? AND item_key = ?",
                       (p.owner_id, p.id, key)).fetchone()
    if row is not None and row["content_hash"] == digest:
        # Exact retry is safe, including a retry after the human closed the item.
        return _item(conn, p, _row(conn, p, row["id"]))
    if row is not None:
        _row(conn, p, row["id"])
        base = _integer(payload.get("base_version"), "base_version")
        if base != row["version"]:
            raise conflict("task_version_conflict", "task changed; reload before updating")
        conn.execute("""UPDATE task_items SET title = ?, description = ?, channel_id = ?, original_channel_id = ?,
                        anchor_id = ?, original_anchor_id = ?, options = ?, multiple = ?, content_hash = ?, blocks = ?,
                        version = version + 1, updated_at = ? WHERE id = ?""",
                     (content["title"], content["description"], content["channel_id"], content["channel_id"],
                      content["anchor_id"], content["anchor_id"], json.dumps(content["options"]), int(content["multiple"]),
                      digest, json.dumps(content.get("blocks", [])), now, row["id"]))
        item_id = row["id"]
        conn.execute("DELETE FROM task_sources WHERE item_id = ?", (item_id,))
    else:
        if "base_version" in payload:
            raise conflict("task_version_conflict", "task does not exist; omit base_version to create")
        rank = conn.execute("SELECT COALESCE(MAX(rank), 0) + 1 FROM task_items WHERE owner_id = ?", (p.owner_id,)).fetchone()[0]
        item_id = conn.execute("""INSERT INTO task_items
            (owner_id, agent_id, item_key, title, description, channel_id, original_channel_id, anchor_id,
             original_anchor_id, options, multiple, content_hash, blocks, created_at, updated_at, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.owner_id, p.id, key, content["title"], content["description"], content["channel_id"], content["channel_id"],
             content["anchor_id"], content["anchor_id"], json.dumps(content["options"]), int(content["multiple"]), digest,
             json.dumps(content.get("blocks", [])), now, now, rank)).lastrowid
        _bump_order(conn, p.owner_id)
    conn.executemany("""INSERT INTO task_sources(item_id, position, message_id, original_message_id, channel_id,
                        original_channel_id) VALUES (?, ?, ?, ?, ?, ?)""",
                     [(item_id, i, mid, mid, cid, cid) for i, (mid, cid) in enumerate(refs)])
    _notice(conn, p.owner_id, p.id, now)
    store.audit(conn, p.id, "task.put", f"task:{item_id}", {"key": key}, now)
    return _item(conn, p, _row(conn, p, item_id))


def _answer_destination(conn, p, row, now):
    if row["original_channel_id"] is not None:
        if row["channel_id"] is None or (row["original_anchor_id"] is not None and row["anchor_id"] is None):
            raise conflict("task_destination_unavailable", "the specified destination was deleted")
        return row["channel_id"], row["anchor_id"]
    try:
        agent = _current(conn, row["agent_id"])
    except ApiError:
        raise conflict("task_destination_unavailable", "the proposing agent is no longer active")
    if agent.is_human or agent.owner_id != p.id or not agent.may_post:
        raise conflict("task_destination_unavailable", "the proposing agent is no longer available to you")
    channel, _ = store.open_dm(conn, p, agent.id, now)
    return channel["id"], None


def _replay(conn, p, receipt):
    row = _row(conn, p, receipt["item_id"])
    recorded = json.loads(receipt["result"])
    item = _item(conn, p, row)
    # Preserve the operation's outcome but never replay cached source/message text.
    for name in ("version", "state", "closed_reason", "closed_at"):
        item[name] = recorded[name]
    message = _message(conn, p, recorded["message_id"])
    if recorded["action"] == "answer":
        item["answer"] = message
        item["answer_message_id"] = recorded["message_id"]
    return {"item": item, "message": message}, None, True


def action(conn, p, item_id, payload, request_key, now):
    p = _principal(conn, p, now, write=True)
    _integer(item_id, "item id")
    _object(payload, {"action", "version", "body", "option_ids"})
    kind = payload.get("action")
    if kind not in ("answer", "done", "dismiss", "restore"):
        raise bad_request("action must be answer/done/dismiss/restore")
    version = _integer(payload.get("version"), "version")
    if not isinstance(request_key, str) or not store.CLIENT_ID.fullmatch(request_key):
        raise bad_request("a valid Idempotency-Key is required")
    if kind == "answer" and not p.is_human:
        raise forbidden("only the human owner can answer a task")
    if kind != "answer" and ("body" in payload or "option_ids" in payload):
        raise bad_request("body and option_ids are only valid for answer")
    fingerprint = _digest({"item_id": item_id, "payload": payload})
    prior = conn.execute("SELECT * FROM task_receipts WHERE actor_id = ? AND request_key = ?",
                         (p.id, request_key)).fetchone()
    if prior is not None:
        if prior["payload_hash"] != fingerprint:
            raise conflict("idempotency_conflict", "this key was already used with different content")
        return _replay(conn, p, prior)
    row = _row(conn, p, item_id)
    if version != row["version"]:
        raise conflict("task_version_conflict", "task changed; reload before acting")
    if kind == "restore":
        if row["state"] != "closed":
            raise conflict("task_state_conflict", "task is already open")
    elif row["state"] != "open":
        raise conflict("task_closed", "task is already closed")
    message, created_message = None, None
    if kind == "answer":
        body = _text(payload.get("body", ""), "body", 65536, empty=True)
        selected = payload.get("option_ids", [])
        if (not isinstance(selected, list) or len(selected) > MAX_OPTIONS
                or any(not isinstance(s, str) for s in selected) or len(set(selected)) != len(selected)):
            raise bad_request("option_ids must be a list of distinct option IDs")
        choices = {o["id"]: o["label"] for o in json.loads(row["options"])}
        if any(s not in choices for s in selected) or (not row["multiple"] and len(selected) > 1):
            raise bad_request("unknown option or multiple selections are not allowed")
        parts = [choices[s] for s in selected]
        if body:
            parts.append(body)
        text = "\n".join(parts)
        if not text.strip():
            raise bad_request("choose an answer or write a message")
        cid, anchor = _answer_destination(conn, p, row, now)
        normalized, digest = store.normalize_payload(cid, {"body": text, "reply_to": anchor})
        # Legacy internal prefixes are stable retry identities, not product labels.
        # Keep them across the Tasks rename to preserve existing message receipts.
        internal_key = "#queue-answer:" + str(p.id) + ":" + request_key
        message, created = store.post_message(conn, p, cid, internal_key, normalized, digest, now)
        if created:
            created_message = message
        conn.execute("UPDATE task_items SET answer_id = ?, original_answer_id = ? WHERE id = ?",
                     (message["id"], message["id"], item_id))
    state = "open" if kind == "restore" else "closed"
    reason = None if kind == "restore" else "answered" if kind == "answer" else "dismissed" if kind == "dismiss" else "done"
    closed_at = None if kind == "restore" else now
    conn.execute("""UPDATE task_items SET state = ?, closed_reason = ?, closed_at = ?,
                    version = version + 1, updated_at = ?, seen = 1 WHERE id = ?""", (state, reason, closed_at, now, item_id))
    row = _row(conn, p, item_id)
    recorded = {"action": kind, "version": row["version"], "state": state, "closed_reason": reason,
                "closed_at": closed_at, "message_id": message["id"] if message else None}
    conn.execute("""INSERT INTO task_receipts(actor_id, request_key, item_id, payload_hash, result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""", (p.id, request_key, item_id, fingerprint, json.dumps(recorded), now))
    store.audit(conn, p.id, "task." + kind, f"task:{item_id}", {"message_id": recorded["message_id"]}, now)
    if not p.is_human:
        _notice(conn, row["owner_id"], p.id, now)
    return {"item": _item(conn, p, row), "message": message}, created_message, False


def move_item(conn, p, item_id, payload, now):
    p = _principal(conn, p, now, write=True)
    row = _row(conn, p, item_id)
    _object(payload, {"order_version", "before_id", "after_id"})
    version = payload.get("order_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise bad_request("order_version must be a nonnegative integer")
    if version != _order_version(conn, _owner(p)):
        raise conflict("task_order_conflict", "task order changed; reload before moving")
    before = _integer(payload.get("before_id"), "before_id", optional=True)
    after = _integer(payload.get("after_id"), "after_id", optional=True)
    if before is not None and after is not None:
        raise bad_request("specify only before_id or after_id")
    neighbor = before if before is not None else after
    if neighbor == item_id:
        raise bad_request("an item cannot be its own neighbor")
    if neighbor is not None:
        _row(conn, p, neighbor)
    ids = [r[0] for r in conn.execute("SELECT id FROM task_items WHERE owner_id = ? ORDER BY rank, id", (_owner(p),))]
    old = list(ids)
    ids.remove(item_id)
    index = len(ids) if neighbor is None else ids.index(neighbor) + (1 if after is not None else 0)
    ids.insert(index, item_id)
    if ids != old:
        conn.executemany("UPDATE task_items SET rank = ? WHERE id = ?", [(i + 1, mid) for i, mid in enumerate(ids)])
        _bump_order(conn, _owner(p))
        store.audit(conn, p.id, "task.move", f"task:{row['id']}", {"before_id": before, "after_id": after}, now)
    return _listing(conn, p, True, "manual", "asc")


def notice_due(conn, now):
    return conn.execute("SELECT 1 FROM task_notices WHERE state = 'pending' AND due_at <= ? LIMIT 1", (now,)).fetchone() is not None


def deliver_notice(conn, now):
    notice = conn.execute("SELECT * FROM task_notices WHERE state = 'pending' AND due_at <= ? ORDER BY due_at, id LIMIT 1",
                          (now,)).fetchone()
    if notice is None:
        return None
    try:
        agent = _current(conn, notice["agent_id"])
        owner = _current(conn, notice["owner_id"])
        if agent.is_human or not owner.is_human or agent.owner_id != owner.id or not agent.may_post:
            raise forbidden("task notice is no longer authorized")
    except ApiError:
        conn.execute("UPDATE task_notices SET state = 'suppressed' WHERE id = ?", (notice["id"],))
        return None
    # A delivery error must not leave a newly created empty DM committed. Unexpected
    # failures propagate, rolling back the outer write; expected access failures
    # roll back this savepoint and suppress the permanently invalid batch.
    conn.execute("SAVEPOINT task_notice_delivery")
    try:
        channel, _ = store.open_dm(conn, agent, owner.id, now)
        n = notice["changes"]
        body = f"I updated your tasks ({n} {'change' if n == 1 else 'changes'}). [Open Tasks](#tasks)"
        normalized, digest = store.normalize_payload(channel["id"], {"body": body})
        # Retain the historical internal idempotency prefix for existing batches.
        message, created = store.post_message(conn, agent, channel["id"], "#queue-notice:" + str(notice["id"]),
                                               normalized, digest, now)
        conn.execute("UPDATE task_notices SET state = 'sent', message_id = ? WHERE id = ?",
                     (message["id"], notice["id"]))
        conn.execute("RELEASE task_notice_delivery")
        return message if created else None
    except ApiError as exc:
        conn.execute("ROLLBACK TO task_notice_delivery")
        conn.execute("RELEASE task_notice_delivery")
        if exc.code == "channel_archived":
            # Archival is reversible; retain the durable batch and avoid a hot
            # retry loop while leaving other agents' batches free to deliver.
            conn.execute("UPDATE task_notices SET due_at = ? WHERE id = ?", (now + NOTICE_DELAY, notice["id"]))
            return None
        if exc.status in (401, 403, 404):
            conn.execute("UPDATE task_notices SET state = 'suppressed' WHERE id = ?", (notice["id"],))
            return None
        raise
