"""All SQL lives here. Functions take a connection and do not manage transactions.

Read functions are called on a reader connection; write functions are called by
the single writer inside BEGIN IMMEDIATE ... COMMIT (see server.Database).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from typing import Any

from . import auth
from .auth import Principal
from .config import LIMITS
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized

CHANNEL_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


# --------------------------------------------------------------------------- auth


def authenticate(conn: sqlite3.Connection, token: str | None, now: float) -> Principal:
    if not token:
        raise unauthorized()
    parsed = auth.parse_token(token)
    if parsed is None:
        raise unauthorized()
    cred_id, secret = parsed
    row = conn.execute(
        """SELECT c.secret_hash, c.expires_at, c.revoked_at, p.*
           FROM credentials c JOIN principals p ON p.id = c.principal_id
           WHERE c.id = ?""",
        (cred_id,),
    ).fetchone()
    if row is None or not auth.secret_matches(secret, row["secret_hash"]):
        raise unauthorized()
    if row["revoked_at"] is not None:
        raise unauthorized("token revoked")
    if row["expires_at"] is not None and row["expires_at"] <= now:
        raise unauthorized("token expired")
    if row["disabled"]:
        raise unauthorized("identity disabled")
    return Principal(
        id=row["id"],
        issuer=row["issuer"],
        external_id=row["external_id"],
        identity_type=row["identity_type"],
        role=row["role"],
        display_name=row["display_name"],
        credential_id=cred_id,
    )


def is_admin(conn: sqlite3.Connection, principal_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM grants WHERE principal_id = ? AND channel_id = 0 AND action = 'admin'",
        (principal_id,),
    ).fetchone() is not None


def has_action(conn: sqlite3.Connection, principal_id: int, action: str, channel_id: int = 0) -> bool:
    row = conn.execute(
        """SELECT 1 FROM grants WHERE principal_id = ?
           AND ((channel_id = ? AND action = ?) OR (channel_id = 0 AND action = 'admin'))
           LIMIT 1""",
        (principal_id, channel_id, action),
    ).fetchone()
    return row is not None


def channel_for(conn: sqlite3.Connection, p: Principal, channel_id: int, action: str) -> sqlite3.Row:
    """Fetch a channel the principal may use for `action`.

    No read access means 404 so channels outside a principal's grants are invisible.
    """
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None or not has_action(conn, p.id, "messages:read", channel_id):
        raise not_found("channel not found")
    if action != "messages:read" and not has_action(conn, p.id, action, channel_id):
        raise forbidden(f"missing permission {action} on this channel")
    return row


def audit(conn, actor_id: int | None, action: str, target: str, detail: dict | None, now: float) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor_id, action, target, detail) VALUES (?, ?, ?, ?, ?)",
        (now, actor_id, action, target, json.dumps(detail or {}, sort_keys=True)),
    )


def _set_role_grants(conn, principal_id: int, role: str, channel_ids: list[int]) -> None:
    conn.execute("DELETE FROM grants WHERE principal_id = ? AND source = 'role'", (principal_id,))
    rows = [(principal_id, 0, a) for a in auth.BOARD_ACTIONS[role]]
    rows += [(principal_id, cid, a) for cid in channel_ids for a in auth.CHANNEL_ACTIONS[role]]
    conn.executemany(
        "INSERT OR IGNORE INTO grants (principal_id, channel_id, action, source) VALUES (?, ?, ?, 'role')",
        rows,
    )


def _role_channel_ids(conn, principal_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT channel_id FROM grants WHERE principal_id = ? AND source = 'role' AND channel_id != 0",
        (principal_id,),
    ).fetchall()
    return sorted(r[0] for r in rows)


def _new_credential(conn, principal_id: int, issued_by: int | None, expires_at: float | None, now: float):
    """Atomically replace the identity's active token with a new one."""
    conn.execute(
        "UPDATE credentials SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
        (now, principal_id),
    )
    cred_id, token, digest = auth.new_token()
    conn.execute(
        """INSERT INTO credentials (id, principal_id, secret_hash, issued_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cred_id, principal_id, digest, issued_by, now, expires_at),
    )
    return cred_id, token


# --------------------------------------------------------------- bootstrap admin


def admin_exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM principals WHERE issuer = 'local' AND external_id = 'operator'").fetchone() is not None


def create_operator(conn, now: float) -> tuple[int, str]:
    cur = conn.execute(
        """INSERT INTO principals (issuer, external_id, identity_type, role, display_name, created_at)
           VALUES ('local', 'operator', 'user', 'admin', 'operator', ?)""",
        (now,),
    )
    pid = cur.lastrowid
    _set_role_grants(conn, pid, "admin", [])
    _, token = _new_credential(conn, pid, None, None, now)
    audit(conn, None, "operator.create", f"principal:{pid}", None, now)
    return pid, token


def rotate_operator(conn, now: float) -> str:
    row = conn.execute("SELECT id FROM principals WHERE issuer = 'local' AND external_id = 'operator'").fetchone()
    if row is None:
        raise RuntimeError("board has no operator; run init first")
    _, token = _new_credential(conn, row["id"], None, None, now)
    audit(conn, None, "operator.rotate", f"principal:{row['id']}", None, now)
    return token


# ------------------------------------------------------------------ provisioning


def _principal_json(conn, row) -> dict:
    out = {
        "id": row["id"],
        "external_id": row["external_id"],
        "issuer": row["issuer"],
        "identity_type": row["identity_type"],
        "role": row["role"],
        "display_name": row["display_name"],
        "disabled": bool(row["disabled"]),
        "channels": _channel_names(conn, _role_channel_ids(conn, row["id"])),
    }
    if row["role"] == "provisioner":
        out["policy"] = _policy(conn, row["id"])
    return out


def _channel_names(conn, ids: list[int]) -> list[str]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return sorted(r[0] for r in conn.execute(f"SELECT name FROM channels WHERE id IN ({marks})", ids))


def _resolve_channels(conn, names: list[str]) -> list[int]:
    ids = []
    for name in names:
        row = conn.execute("SELECT id FROM channels WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise not_found(f"unknown channel {name!r}")
        ids.append(row["id"])
    return sorted(set(ids))


def _policy(conn, principal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM provisioner_policies WHERE principal_id = ?", (principal_id,)).fetchone()
    if row is None:
        return None
    channels = json.loads(row["channels"])
    return {
        "roles": json.loads(row["roles"]),
        "channels": channels if channels == ["*"] else _channel_names(conn, channels),
        "max_ttl_seconds": row["max_ttl_seconds"],
        "max_agents": row["max_agents"],
    }


def _validate_policy(conn, policy: dict | None) -> tuple[list[str], list, int, int]:
    policy = policy or {}
    roles = policy.get("roles", list(auth.DEFAULT_PROVISIONER_ROLES))
    if not isinstance(roles, list) or not roles or any(r not in auth.PROVISIONABLE_ROLES for r in roles):
        raise bad_request(f"policy.roles must be a non-empty subset of {list(auth.PROVISIONABLE_ROLES)}")
    channels = policy.get("channels", [])
    if channels == ["*"] or channels == "*":
        channel_ids: list = ["*"]
    elif isinstance(channels, list) and all(isinstance(c, str) for c in channels):
        channel_ids = _resolve_channels(conn, channels)
    else:
        raise bad_request("policy.channels must be a list of channel names or ['*']")
    max_ttl = policy.get("max_ttl_seconds", LIMITS.default_token_ttl)
    max_agents = policy.get("max_agents", 1000)
    for key, value in (("max_ttl_seconds", max_ttl), ("max_agents", max_agents)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise bad_request(f"policy.{key} must be a positive integer")
    if max_ttl > LIMITS.max_admin_token_ttl:
        raise bad_request("policy.max_ttl_seconds too large")
    return sorted(set(roles)), channel_ids, max_ttl, max_agents


def _load_policy_raw(conn, principal_id: int):
    row = conn.execute("SELECT * FROM provisioner_policies WHERE principal_id = ?", (principal_id,)).fetchone()
    if row is None:
        raise forbidden("provisioner has no policy")
    return json.loads(row["roles"]), json.loads(row["channels"]), row["max_ttl_seconds"], row["max_agents"]


def provision_agent(conn, actor: Principal, req: dict, now: float) -> tuple[dict, bool]:
    external_id = req.get("external_id")
    identity_type = req.get("identity_type", "agent")
    role = req.get("role", "worker")
    channels = req.get("channels", [])
    display_name = req.get("display_name")
    update = bool(req.get("update", False))
    policy_req = req.get("policy")

    if not isinstance(external_id, str) or not EXTERNAL_ID.match(external_id):
        raise bad_request("external_id must match " + EXTERNAL_ID.pattern)
    if identity_type not in auth.IDENTITY_TYPES:
        raise bad_request(f"identity_type must be one of {list(auth.IDENTITY_TYPES)}")
    if role not in auth.ROLES:
        raise bad_request(f"role must be one of {list(auth.ROLES)}")
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise bad_request("channels must be a list of channel names")
    if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 128):
        raise bad_request("display_name must be a string of at most 128 characters")
    if policy_req is not None and role != "provisioner":
        raise bad_request("policy is only valid for role 'provisioner'")

    actor_is_admin = is_admin(conn, actor.id)
    if actor_is_admin:
        issuer = "admin"
    elif has_action(conn, actor.id, "agents:provision"):
        issuer = f"p:{actor.id}"
    else:
        raise forbidden("not allowed to provision agents")

    channel_ids = _resolve_channels(conn, channels)

    if not actor_is_admin:
        allowed_roles, allowed_channels, _, max_agents = _load_policy_raw(conn, actor.id)
        if role not in allowed_roles or role not in auth.PROVISIONABLE_ROLES:
            raise forbidden(f"provisioning policy does not allow role {role!r}")
        if allowed_channels != ["*"] and not set(channel_ids) <= set(allowed_channels):
            raise forbidden("provisioning policy does not allow one or more channels")

    existing = conn.execute(
        "SELECT * FROM principals WHERE issuer = ? AND external_id = ?", (issuer, external_id)
    ).fetchone()

    if existing is None:
        if not actor_is_admin:
            count = conn.execute("SELECT COUNT(*) FROM principals WHERE issuer = ?", (issuer,)).fetchone()[0]
            if count >= max_agents:
                raise forbidden("provisioning policy agent limit reached")
        cur = conn.execute(
            """INSERT INTO principals (issuer, external_id, identity_type, role, display_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (issuer, external_id, identity_type, role, display_name, now),
        )
        pid = cur.lastrowid
        _set_role_grants(conn, pid, role, channel_ids)
        if role == "provisioner":
            roles, pch, ttl, max_n = _validate_policy(conn, policy_req)
            conn.execute(
                "INSERT INTO provisioner_policies VALUES (?, ?, ?, ?, ?)",
                (pid, json.dumps(roles), json.dumps(pch), ttl, max_n),
            )
        audit(conn, actor.id, "agent.create", f"principal:{pid}",
              {"role": role, "channels": channels, "issuer": issuer}, now)
        row = conn.execute("SELECT * FROM principals WHERE id = ?", (pid,)).fetchone()
        return _principal_json(conn, row), True

    # Same registration repeated: return the same identity.
    same = (
        existing["identity_type"] == identity_type
        and existing["role"] == role
        and _role_channel_ids(conn, existing["id"]) == channel_ids
        and (display_name is None or display_name == existing["display_name"])
        and (role != "provisioner" or policy_req is None or _policy_matches(conn, existing["id"], policy_req))
    )
    if same:
        return _principal_json(conn, existing), False
    if not update:
        raise conflict("registration_conflict",
                       "agent exists with different grants; pass update=true to change them")
    if existing["identity_type"] != identity_type:
        raise conflict("registration_conflict", "identity_type cannot be changed")
    pid = existing["id"]
    conn.execute(
        "UPDATE principals SET role = ?, display_name = COALESCE(?, display_name) WHERE id = ?",
        (role, display_name, pid),
    )
    _set_role_grants(conn, pid, role, channel_ids)
    conn.execute("DELETE FROM provisioner_policies WHERE principal_id = ?", (pid,))
    if role == "provisioner":
        roles, pch, ttl, max_n = _validate_policy(conn, policy_req)
        conn.execute("INSERT INTO provisioner_policies VALUES (?, ?, ?, ?, ?)",
                     (pid, json.dumps(roles), json.dumps(pch), ttl, max_n))
    audit(conn, actor.id, "agent.update", f"principal:{pid}", {"role": role, "channels": channels}, now)
    row = conn.execute("SELECT * FROM principals WHERE id = ?", (pid,)).fetchone()
    return _principal_json(conn, row), False


def _policy_matches(conn, pid: int, policy_req: dict) -> bool:
    roles, pch, ttl, max_n = _validate_policy(conn, policy_req)
    return _load_policy_raw(conn, pid) == (roles, pch, ttl, max_n)


def _target_for_token_admin(conn, actor: Principal, target_id: int):
    """Return (target row, max ttl) if actor may manage target's tokens, else raise 404."""
    target = conn.execute("SELECT * FROM principals WHERE id = ?", (target_id,)).fetchone()
    if target is None:
        raise not_found("agent not found")
    if is_admin(conn, actor.id):
        return target, LIMITS.max_admin_token_ttl
    if has_action(conn, actor.id, "agents:provision") and target["issuer"] == f"p:{actor.id}":
        roles, _, max_ttl, _ = _load_policy_raw(conn, actor.id)
        if target["role"] not in roles:
            raise forbidden("provisioning policy no longer covers this agent's role")
        return target, max_ttl
    raise not_found("agent not found")


def issue_token(conn, actor: Principal, target_id: int, ttl: int | None, now: float) -> dict:
    target, max_ttl = _target_for_token_admin(conn, actor, target_id)
    if target["disabled"]:
        raise conflict("agent_disabled", "agent is disabled")
    if ttl is None:
        ttl = min(LIMITS.default_token_ttl, max_ttl)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
        raise bad_request("ttl_seconds must be a positive integer")
    if ttl > max_ttl:
        raise forbidden(f"ttl_seconds exceeds the allowed maximum of {max_ttl}")
    expires_at = now + ttl
    cred_id, token = _new_credential(conn, target["id"], actor.id, expires_at, now)
    audit(conn, actor.id, "token.issue", f"principal:{target['id']}", {"token_id": cred_id, "ttl": ttl}, now)
    return {"agent_id": target["id"], "token_id": cred_id, "token": token, "expires_at": expires_at}


def revoke_token(conn, actor: Principal, cred_id: str, now: float) -> dict:
    row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
    if row is None:
        raise not_found("token not found")
    if row["principal_id"] != actor.id:
        _target_for_token_admin(conn, actor, row["principal_id"])
    if row["revoked_at"] is None:
        conn.execute("UPDATE credentials SET revoked_at = ? WHERE id = ?", (now, cred_id))
        audit(conn, actor.id, "token.revoke", f"token:{cred_id}", None, now)
    return {"token_id": cred_id, "revoked": True}


def get_agent(conn, actor: Principal, target_id: int) -> dict:
    if target_id != actor.id:
        target, _ = _target_for_token_admin(conn, actor, target_id)
    else:
        target = conn.execute("SELECT * FROM principals WHERE id = ?", (target_id,)).fetchone()
    return _principal_json(conn, target)


def me(conn, p: Principal) -> dict:
    row = conn.execute("SELECT * FROM principals WHERE id = ?", (p.id,)).fetchone()
    out = _principal_json(conn, row)
    out["board_actions"] = sorted(
        r[0] for r in conn.execute("SELECT action FROM grants WHERE principal_id = ? AND channel_id = 0", (p.id,))
    )
    cred = conn.execute("SELECT expires_at FROM credentials WHERE id = ?", (p.credential_id,)).fetchone()
    out["token"] = {"id": p.credential_id, "expires_at": cred["expires_at"]}
    return out


# ---------------------------------------------------------------------- channels


def _channel_json(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "archived": bool(row["archived"]),
        "last_seq": row["next_seq"] - 1,
        "created_at": row["created_at"],
    }


def list_channels(conn, p: Principal, name: str | None = None) -> list[dict]:
    sql = "SELECT * FROM channels"
    args: list[Any] = []
    if not is_admin(conn, p.id):
        sql += " WHERE id IN (SELECT channel_id FROM grants WHERE principal_id = ? AND action = 'messages:read')"
        args.append(p.id)
    if name is not None:
        sql += (" AND" if args else " WHERE") + " name = ?"
        args.append(name)
    return [_channel_json(r) for r in conn.execute(sql + " ORDER BY id", args)]


def create_channel(conn, p: Principal, name, description, now: float) -> tuple[dict, bool]:
    if not has_action(conn, p.id, "channels:create"):
        raise forbidden("not allowed to create channels")
    if not isinstance(name, str) or not CHANNEL_NAME.match(name):
        raise bad_request("name must match " + CHANNEL_NAME.pattern)
    if description is not None and (not isinstance(description, str) or len(description) > 1024):
        raise bad_request("description must be a string of at most 1024 characters")
    existing = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        # Retry-safe for the original creator; everyone else just learns the name is taken.
        if existing["created_by"] == p.id:
            return _channel_json(existing), False
        raise conflict("channel_exists", "a channel with this name already exists")
    cur = conn.execute(
        "INSERT INTO channels (name, description, created_by, created_at) VALUES (?, ?, ?, ?)",
        (name, description, p.id, now),
    )
    cid = cur.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO grants (principal_id, channel_id, action, source) VALUES (?, ?, ?, 'creator')",
        [(p.id, cid, a) for a in auth.CREATOR_ACTIONS],
    )
    audit(conn, p.id, "channel.create", f"channel:{cid}", {"name": name}, now)
    return _channel_json(conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()), True


def archive_channel(conn, p: Principal, channel_id: int, now: float) -> dict:
    if not is_admin(conn, p.id):
        raise forbidden("only admins can archive channels")
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise not_found("channel not found")
    conn.execute("UPDATE channels SET archived = 1 WHERE id = ?", (channel_id,))
    audit(conn, p.id, "channel.archive", f"channel:{channel_id}", None, now)
    return _channel_json(conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())


# ---------------------------------------------------------------------- messages

MESSAGE_SELECT = """
SELECT m.*, p.external_id AS s_external_id, p.identity_type AS s_identity_type,
       p.display_name AS s_display_name, p.role AS s_role
FROM messages m JOIN principals p ON p.id = m.sender_id
"""


def _message_json(row) -> dict:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "seq": row["seq"],
        "sender": {
            "id": row["sender_id"],
            "external_id": row["s_external_id"],
            "identity_type": row["s_identity_type"],
            "display_name": row["s_display_name"],
        },
        "body": row["body"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
        "reply_to": row["reply_to"],
        "attachments": json.loads(row["attachments"]) if row["attachments"] else [],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


def normalize_payload(channel_id: int, payload: Any) -> tuple[dict, str]:
    if not isinstance(payload, dict):
        raise bad_request("request body must be a JSON object")
    unknown = set(payload) - {"body", "metadata", "reply_to", "attachments"}
    if unknown:
        raise bad_request(f"unknown fields: {sorted(unknown)}")
    body = payload.get("body")
    metadata = payload.get("metadata")
    reply_to = payload.get("reply_to")
    attachments = payload.get("attachments") or []
    if not isinstance(body, str):
        raise bad_request("body must be a string")
    if len(body.encode()) > LIMITS.max_body_bytes:
        raise ApiError(413, "too_large", f"body exceeds {LIMITS.max_body_bytes} bytes")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise bad_request("metadata must be a JSON object")
        if len(json.dumps(metadata).encode()) > LIMITS.max_metadata_bytes:
            raise ApiError(413, "too_large", f"metadata exceeds {LIMITS.max_metadata_bytes} bytes")
    if reply_to is not None and (not isinstance(reply_to, int) or isinstance(reply_to, bool)):
        raise bad_request("reply_to must be a message id")
    if (not isinstance(attachments, list) or not all(isinstance(a, str) for a in attachments)
            or len(attachments) > LIMITS.max_attachments_per_message):
        raise bad_request(f"attachments must be a list of at most {LIMITS.max_attachments_per_message} ids")
    normalized = {
        "channel_id": channel_id,
        "body": body,
        "metadata": metadata,
        "reply_to": reply_to,
        "attachments": sorted(set(attachments)),
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return normalized, digest


def post_message(conn, p: Principal, channel_id: int, request_id: str, normalized: dict,
                 payload_hash: str, now: float) -> tuple[dict, bool]:
    channel = channel_for(conn, p, channel_id, "messages:write")
    prior = conn.execute(
        MESSAGE_SELECT + " WHERE m.sender_id = ? AND m.request_id = ?", (p.id, request_id)
    ).fetchone()
    if prior is not None:
        if prior["payload_hash"] != payload_hash:
            raise conflict("idempotency_conflict", "this Idempotency-Key was already used with different content")
        return _message_json(prior), False
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    if normalized["reply_to"] is not None:
        parent = conn.execute("SELECT channel_id FROM messages WHERE id = ?", (normalized["reply_to"],)).fetchone()
        if parent is None or parent["channel_id"] != channel_id:
            raise bad_request("reply_to must reference a message in the same channel")
    for att_id in normalized["attachments"]:
        att = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        if (att is None or att["channel_id"] != channel_id or att["state"] != "ready"
                or (att["uploader_id"] != p.id and not att["published"])):
            raise bad_request(f"attachment {att_id} is not a ready upload in this channel")
    seq = channel["next_seq"]
    cur = conn.execute(
        """INSERT INTO messages (channel_id, seq, sender_id, body, metadata, reply_to, attachments,
                                 request_id, payload_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id, seq, p.id, normalized["body"],
            json.dumps(normalized["metadata"]) if normalized["metadata"] is not None else None,
            normalized["reply_to"],
            json.dumps(normalized["attachments"]) if normalized["attachments"] else None,
            request_id, payload_hash, now,
        ),
    )
    conn.execute("UPDATE channels SET next_seq = ? WHERE id = ?", (seq + 1, channel_id))
    if normalized["attachments"]:
        marks = ",".join("?" * len(normalized["attachments"]))
        conn.execute(f"UPDATE attachments SET published = 1 WHERE id IN ({marks})", normalized["attachments"])
    row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (cur.lastrowid,)).fetchone()
    return _message_json(row), True


def read_messages(conn, p: Principal, channel_id: int, after: int, limit: int) -> dict:
    channel_for(conn, p, channel_id, "messages:read")
    rows = conn.execute(
        MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
        (channel_id, after, limit + 1),
    ).fetchall()
    messages, size = [], 0
    for row in rows[:limit]:
        size += len(row["body"]) + len(row["metadata"] or "") + 256
        if messages and size > LIMITS.max_page_bytes:
            break
        messages.append(_message_json(row))
    return {
        "messages": messages,
        "next_cursor": messages[-1]["seq"] if messages else after,
        "has_more": len(messages) < len(rows),
    }


# ------------------------------------------------------------------- attachments


def _attachment_json(row) -> dict:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "upload_id": row["upload_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "sha256": row["sha256"],
        "state": row["state"],
        "published": bool(row["published"]),
    }


def begin_upload(conn, p: Principal, channel_id: int, upload_id: str, filename, content_type, now: float) -> dict:
    channel = channel_for(conn, p, channel_id, "attachments:write")
    if not CLIENT_ID.match(upload_id):
        raise bad_request("upload_id must match " + CLIENT_ID.pattern)
    row = conn.execute(
        "SELECT * FROM attachments WHERE uploader_id = ? AND upload_id = ?", (p.id, upload_id)
    ).fetchone()
    if row is not None:
        if row["channel_id"] != channel_id:
            raise conflict("upload_conflict", "upload_id already used for another channel")
        return dict(row)
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    att_id = "att_" + secrets.token_hex(12)
    conn.execute(
        """INSERT INTO attachments (id, channel_id, uploader_id, upload_id, storage_key, filename,
                                    content_type, state, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (att_id, channel_id, p.id, upload_id, f"{att_id[4:6]}/{att_id}", filename, content_type, now),
    )
    return dict(conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone())


def finish_upload(conn, att_id: str, size: int, sha256: str) -> dict:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row["state"] == "ready":
        if row["sha256"] != sha256:
            raise conflict("upload_conflict", "upload_id already holds different content")
        return _attachment_json(row)
    conn.execute("UPDATE attachments SET state = 'ready', size = ?, sha256 = ? WHERE id = ?", (size, sha256, att_id))
    return _attachment_json(conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone())


def attachment_state(conn, att_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()


def get_attachment(conn, p: Principal, att_id: str) -> dict:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row is None or row["state"] != "ready":
        raise not_found("attachment not found")
    if row["uploader_id"] == p.id or is_admin(conn, p.id):
        pass
    elif not (row["published"] and has_action(conn, p.id, "messages:read", row["channel_id"])):
        raise not_found("attachment not found")
    out = _attachment_json(row)
    out["storage_key"] = row["storage_key"]
    return out
