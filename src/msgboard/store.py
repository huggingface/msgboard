"""All SQL lives here. Functions take a connection and do not manage transactions.

Read functions are called on a reader connection; write functions are called by
the single writer inside BEGIN IMMEDIATE ... COMMIT (see server.Database).

Access model (Slack-like):
  people (identity type `user`) are admin, member or viewer;
  agents and service accounts (`agent`, `system`) are member or guest, and always
  belong to the person who invited them. They stop working when that person is
  disabled or no longer an admin or member.
  admin   sees every channel and DM; manages people, invites and settings
  member  sees every public channel plus private channels and DMs they are in
  viewer  like member, read-only
  guest   only channels they were added to (and their DMs)
  The owner of an agent can also read that agent's DMs.
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
PERSON_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PEOPLE = "people"   # issuer for people who join with an invite


def audit(conn, actor_id: int | None, action: str, target: str, detail: dict | None, now: float) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor_id, action, target, detail) VALUES (?, ?, ?, ?, ?)",
        (now, actor_id, action, target, json.dumps(detail or {}, sort_keys=True)),
    )


# --------------------------------------------------------------------------- auth


def authenticate(conn: sqlite3.Connection, token: str | None, now: float) -> Principal:
    if not token:
        raise unauthorized()
    parsed = auth.parse_token(token)
    if parsed is None:
        raise unauthorized()
    cred_id, secret = parsed
    row = conn.execute(
        """SELECT c.secret_hash, c.expires_at, c.revoked_at, p.*, o.disabled AS o_disabled, o.role AS o_role
           FROM credentials c JOIN principals p ON p.id = c.principal_id
           LEFT JOIN principals o ON o.id = p.owner_id
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
    if row["owner_id"] is not None:
        needed = ("admin",) if row["role"] in auth.ADMIN_ONLY_AGENT_ROLES else ("admin", "member")
        if row["o_disabled"] or row["o_role"] not in needed:
            raise unauthorized("the person who owns this agent is no longer active, or no longer an admin")
    return Principal(
        id=row["id"], issuer=row["issuer"], external_id=row["external_id"], identity_type=row["identity_type"],
        role=row["role"], display_name=row["display_name"], owner_id=row["owner_id"],
        can_create_channels=bool(row["can_create_channels"]), credential_id=cred_id,
    )


def _new_credential(conn, principal_id: int, issued_by: int | None, now: float, replace: bool = True):
    """Issue a token. Tokens do not expire. replace=True revokes the identity's other tokens."""
    if replace:
        conn.execute("UPDATE credentials SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                     (now, principal_id))
    cred_id, token, digest = auth.new_token()
    conn.execute(
        """INSERT INTO credentials (id, principal_id, secret_hash, issued_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, NULL)""",
        (cred_id, principal_id, digest, issued_by, now),
    )
    return cred_id, token


def _principal_row(conn, pid: int):
    row = conn.execute("SELECT * FROM principals WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise not_found("identity not found")
    return row


# --------------------------------------------------------------------- settings


MAX_GUIDELINES = 20_000


def get_settings(conn) -> dict:
    values = dict(conn.execute("SELECT key, value FROM settings WHERE key NOT IN ('reads_since', 'mentions_v2', 'mentions_v3')").fetchall())
    return {"allow_private_channels": values.get("allow_private_channels", "true") == "true",
            "channel_docs": values.get("channel_docs", "true") == "true",
            "guidelines": values.get("guidelines", "")}


def update_settings(conn, p: Principal, changes: dict, now: float) -> dict:
    if not p.is_admin:
        raise forbidden("only admins can change settings")
    if not isinstance(changes, dict):
        raise bad_request("request body must be a JSON object")
    for key, value in changes.items():
        if key in ("allow_private_channels", "channel_docs") and isinstance(value, bool):
            value = "true" if value else "false"
        elif key == "guidelines" and isinstance(value, str) and len(value) <= MAX_GUIDELINES:
            pass
        else:
            raise bad_request("supported settings: allow_private_channels, channel_docs (true or false), "
                              f"guidelines (markdown, at most {MAX_GUIDELINES} characters)")
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, value))
    audit(conn, p.id, "settings.update", "settings", changes, now)
    return get_settings(conn)


# ------------------------------------------------------------------ visibility


def _is_channel_member(conn, channel_id: int, pid: int) -> bool:
    return conn.execute("SELECT 1 FROM channel_members WHERE channel_id = ? AND principal_id = ?",
                        (channel_id, pid)).fetchone() is not None


def _owns_a_member(conn, channel_id: int, owner_id: int) -> bool:
    return conn.execute(
        """SELECT 1 FROM channel_members cm JOIN principals a ON a.id = cm.principal_id
           WHERE cm.channel_id = ? AND a.owner_id = ? LIMIT 1""", (channel_id, owner_id)).fetchone() is not None


def can_see(conn, p: Principal, ch) -> bool:
    if p.sees_everything:
        return True
    if ch["kind"] == "public" and p.role in ("member", "viewer"):
        return True
    if _is_channel_member(conn, ch["id"], p.id):
        return True
    return ch["kind"] == "dm" and _owns_a_member(conn, ch["id"], p.id)


def can_post(conn, p: Principal, ch) -> bool:
    if not p.may_post or ch["archived"]:
        return False
    if ch["kind"] == "dm":
        return _is_channel_member(conn, ch["id"], p.id)
    if p.is_admin or (ch["kind"] == "public" and p.role == "member"):
        return True
    return _is_channel_member(conn, ch["id"], p.id)


def channel_for(conn, p: Principal, channel_id: int, action: str = "messages:read"):
    """Fetch a channel the principal may use. Channels you cannot see answer 404."""
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if ch is None or not can_see(conn, p, ch):
        raise not_found("channel not found")
    if action != "messages:read" and not can_post(conn, p, ch):
        if ch["archived"]:
            raise conflict("channel_archived", "channel is archived")
        raise forbidden("you can read this channel but not post to it")
    return ch


VISIBLE_CHANNELS_SQL = """
    SELECT c.id FROM channels c WHERE
        (c.kind = 'public' AND :role IN ('member', 'viewer'))
        OR c.id IN (SELECT channel_id FROM channel_members WHERE principal_id = :pid)
        OR (c.kind = 'dm' AND c.id IN (SELECT cm.channel_id FROM channel_members cm
                                       JOIN principals a ON a.id = cm.principal_id WHERE a.owner_id = :pid))
"""


def visible_channel_ids(conn, p: Principal) -> list[int] | None:
    """None means every channel (admins and readers)."""
    if p.sees_everything:
        return None
    return [r[0] for r in conn.execute(VISIBLE_CHANNELS_SQL, {"role": p.role, "pid": p.id})]


# --------------------------------------------------------------- bootstrap admin


def admin_exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM principals WHERE issuer = 'local' AND external_id = 'operator'").fetchone() is not None


def create_operator(conn, now: float) -> tuple[int, str]:
    cur = conn.execute(
        """INSERT INTO principals (issuer, external_id, identity_type, role, display_name, created_at, joined_via)
           VALUES ('local', 'operator', 'user', 'admin', 'operator', ?, 'created the board')""",
        (now,),
    )
    pid = cur.lastrowid
    _, token = _new_credential(conn, pid, None, now)
    audit(conn, None, "operator.create", f"principal:{pid}", None, now)
    return pid, token


def rotate_operator(conn, now: float) -> str:
    row = conn.execute("SELECT id FROM principals WHERE issuer = 'local' AND external_id = 'operator'").fetchone()
    if row is None:
        raise RuntimeError("board has no operator; run init first")
    _, token = _new_credential(conn, row["id"], None, now)
    audit(conn, None, "operator.rotate", f"principal:{row['id']}", None, now)
    return token


# --------------------------------------------------------------------- people


def _brief(conn, pid: int | None) -> dict | None:
    if pid is None:
        return None
    r = conn.execute("""SELECT p.id, p.external_id, p.display_name, p.identity_type, p.role, p.pattern_seed,
                               substr(av.sha256, 1, 12) AS avatar
                        FROM principals p LEFT JOIN avatars av ON av.principal_id = p.id WHERE p.id = ?""",
                     (pid,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "external_id": r["external_id"], "display_name": r["display_name"],
            "identity_type": r["identity_type"], "role": r["role"], "avatar": r["avatar"], "pattern": r["pattern_seed"]}


def principal_json(conn, row) -> dict:
    return {
        "id": row["id"],
        "external_id": row["external_id"],
        "display_name": row["display_name"],
        "identity_type": row["identity_type"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "owner": _brief(conn, row["owner_id"]),
        "can_create_channels": bool(row["can_create_channels"]) if row["identity_type"] != "user"
                               else row["role"] in ("admin", "member"),
        "hf_username": row["hf_username"],
        "created_at": row["created_at"],
        "avatar": _avatar_version(conn, row["id"]),
        "pattern": row["pattern_seed"],
        "runtime": json.loads(row["runtime"]) if row["runtime"] else None,
        "runtime_at": row["runtime_at"],
    }


RUNTIME_TEXT = {"harness": 64, "model": 128, "host": 128, "version": 64, "notes": 280}


def _valid_runtime(value) -> str | None:
    """What an agent says about how it runs. Short, known fields only; null clears it."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise bad_request("runtime must be an object like {harness, model, tools, host}")
    unknown = set(value) - set(RUNTIME_TEXT) - {"models", "tools"}
    if unknown:
        raise bad_request(f"unknown runtime fields: {sorted(unknown)}; use harness, model, models, tools, host, version, notes")
    out = {}
    for key, limit in RUNTIME_TEXT.items():
        if value.get(key) is not None:
            if not isinstance(value[key], str) or len(value[key]) > limit:
                raise bad_request(f"runtime.{key} must be text of at most {limit} characters")
            if value[key].strip():
                out[key] = value[key].strip()
    for key in ("models", "tools"):
        if value.get(key) is not None:
            items = value[key]
            if not isinstance(items, list) or len(items) > 50 or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in items):
                raise bad_request(f"runtime.{key} must be a list of at most 50 names")
            out[key] = list(dict.fromkeys(i.strip() for i in items if i.strip()))
    return json.dumps(out) if out else None


def get_prefs(conn, pid: int) -> dict:
    row = conn.execute("SELECT prefs FROM principals WHERE id = ?", (pid,)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def update_prefs(conn, p: Principal, changes, now: float) -> dict:
    """Your own preferences. `channel_order`: channel ids in the order you want them in the sidebar."""
    if not isinstance(changes, dict) or set(changes) - {"channel_order"}:
        raise bad_request("supported preferences: channel_order (a list of channel ids)")
    prefs = get_prefs(conn, p.id)
    if "channel_order" in changes:
        order = changes["channel_order"]
        if order is not None and (not isinstance(order, list) or len(order) > 1000
                                  or not all(isinstance(i, int) and not isinstance(i, bool) for i in order)):
            raise bad_request("channel_order must be a list of channel ids")
        if order:
            prefs["channel_order"] = list(dict.fromkeys(order))
        else:
            prefs.pop("channel_order", None)
    conn.execute("UPDATE principals SET prefs = ? WHERE id = ?", (json.dumps(prefs) if prefs else None, p.id))
    return prefs


def me(conn, p: Principal) -> dict:
    out = principal_json(conn, _principal_row(conn, p.id))
    out["prefs"] = get_prefs(conn, p.id)
    out["token"] = {"id": p.credential_id}
    out["permissions"] = {
        "post": p.may_post,
        "create_channels": p.may_create_channels,
        "invite_people": p.is_admin,
        "invite_agents": p.may_invite_agents,
        "invite_admin_agents": p.is_admin,
        "manage": p.is_admin,
    }
    out["settings"] = get_settings(conn)
    return out


def _directory_ids(conn, p: Principal) -> list[int] | None:
    """Identities a principal may look up. None = everyone (people)."""
    if p.role != "guest":
        return None
    channels = visible_channel_ids(conn, p) or []
    ids = {p.id}
    if p.owner_id:
        ids.add(p.owner_id)
    if channels:
        marks = ",".join("?" * len(channels))
        ids.update(r[0] for r in conn.execute(
            f"SELECT principal_id FROM channel_members WHERE channel_id IN ({marks})", channels))
        ids.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT sender_id FROM messages WHERE channel_id IN ({marks})", channels))
    return sorted(ids)


def list_principals(conn, p: Principal) -> list[dict]:
    allowed = _directory_ids(conn, p)
    rows = conn.execute("SELECT * FROM principals ORDER BY identity_type DESC, lower(coalesce(display_name, external_id))")
    out = []
    for row in rows:
        if allowed is not None and row["id"] not in allowed:
            continue
        item = principal_json(conn, row)
        if not p.is_admin and p.id != row["id"] and row["owner_id"] != p.id:
            item.pop("hf_username")
        out.append(item)
    return out


def _check_visible(conn, p: Principal, target_id: int) -> None:
    allowed = _directory_ids(conn, p)
    if allowed is not None and target_id not in allowed:
        raise not_found("identity not found")


def _active_admins(conn) -> int:
    """Active admin people (admin agents do not count: they depend on an admin person)."""
    return conn.execute("SELECT COUNT(*) FROM principals WHERE role = 'admin' AND disabled = 0 "
                        "AND identity_type = 'user'").fetchone()[0]


def update_principal(conn, p: Principal, target_id: int, changes: dict, now: float) -> dict:
    """Admins manage everyone; owners manage their agents; everyone may rename themselves."""
    if not isinstance(changes, dict):
        raise bad_request("request body must be a JSON object")
    t = _principal_row(conn, target_id)
    is_owner = t["owner_id"] is not None and t["owner_id"] == p.id
    allowed = set()
    if p.id == target_id:
        allowed.add("display_name")
        if t["identity_type"] != "user":
            allowed.add("runtime")
    if p.is_admin or is_owner:
        allowed.add("display_name")
        if t["identity_type"] != "user":
            allowed.add("runtime")
        allowed.add("disabled")
        allowed.add("role")
        if t["identity_type"] != "user":
            allowed.add("can_create_channels")
    if t["identity_type"] == "user" and not p.is_admin:
        allowed -= {"role", "disabled"}
    unknown = set(changes) - allowed
    if unknown:
        raise forbidden(f"you may not change: {sorted(unknown)}")
    updates = {}
    if "display_name" in changes:
        name = changes["display_name"]
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 128):
            raise bad_request("display_name must be 1 to 128 characters")
        updates["display_name"] = name.strip() if name else None
        if t["display_name"] and t["display_name"] != updates["display_name"]:
            conn.execute("INSERT OR IGNORE INTO former_names (principal_id, name, until) VALUES (?, ?, ?)",
                         (target_id, t["display_name"], now))
    if "runtime" in changes:
        updates["runtime"] = _valid_runtime(changes["runtime"])
        updates["runtime_at"] = now
    if "role" in changes:
        roles = auth.HUMAN_ROLES if t["identity_type"] == "user" else auth.AGENT_ROLES
        if changes["role"] not in roles:
            raise bad_request(f"role must be one of {list(roles)}")
        updates["role"] = changes["role"]
    if "disabled" in changes:
        if not isinstance(changes["disabled"], bool):
            raise bad_request("disabled must be true or false")
        updates["disabled"] = int(changes["disabled"])
    if "can_create_channels" in changes:
        if not isinstance(changes["can_create_channels"], bool):
            raise bad_request("can_create_channels must be true or false")
        updates["can_create_channels"] = int(changes["can_create_channels"])
    if updates.get("role") in auth.ADMIN_ONLY_AGENT_ROLES and t["identity_type"] != "user" and not p.is_admin:
        raise forbidden("only admins can make an agent an admin or a reader")
    if t["identity_type"] == "user" and t["role"] == "admin" \
            and (updates.get("role", "admin") != "admin" or updates.get("disabled")) \
            and not t["disabled"] and _active_admins(conn) <= 1:
        raise conflict("last_admin", "the board needs at least one active admin")
    if updates:
        conn.execute(f"UPDATE principals SET {', '.join(k + ' = ?' for k in updates)} WHERE id = ?",
                     (*updates.values(), target_id))
        audit(conn, p.id, "principal.update", f"principal:{target_id}", changes, now)
    return principal_json(conn, _principal_row(conn, target_id))


def rotate_agent_token(conn, p: Principal, target_id: int, now: float) -> dict:
    """Give an agent or service account a fresh token; its old one stops working."""
    t = _principal_row(conn, target_id)
    if t["identity_type"] == "user":
        raise bad_request("people get new tokens through sign-in links")
    if not (p.is_admin or t["owner_id"] == p.id):
        raise not_found("identity not found")
    cred_id, token = _new_credential(conn, target_id, p.id, now)
    audit(conn, p.id, "token.rotate", f"principal:{target_id}", {"token_id": cred_id}, now)
    return {"principal_id": target_id, "token_id": cred_id, "token": token}


def revoke_token(conn, p: Principal, cred_id: str, now: float) -> dict:
    row = conn.execute("""SELECT c.*, t.owner_id FROM credentials c JOIN principals t ON t.id = c.principal_id
                          WHERE c.id = ?""", (cred_id,)).fetchone()
    if row is None or not (row["principal_id"] == p.id or p.is_admin or row["owner_id"] == p.id):
        raise not_found("token not found")
    if row["revoked_at"] is None:
        conn.execute("UPDATE credentials SET revoked_at = ? WHERE id = ?", (now, cred_id))
        audit(conn, p.id, "token.revoke", f"token:{cred_id}", None, now)
    return {"token_id": cred_id, "revoked": True}


def set_hf_username(conn, principal_id: int, username: str | None, now: float) -> None:
    if username is not None:
        taken = conn.execute("SELECT id FROM principals WHERE lower(hf_username) = lower(?) AND id != ?",
                             (username, principal_id)).fetchone()
        if taken:
            raise conflict("hf_linked_elsewhere", "this Hugging Face account is linked to another board identity")
    conn.execute("UPDATE principals SET hf_username = ? WHERE id = ?", (username, principal_id))
    audit(conn, principal_id, "hf.link" if username else "hf.unlink", f"principal:{principal_id}",
          {"hf_username": username}, now)


def sign_in_with_hf(conn, username: str, now: float) -> dict:
    """A new device token for the person linked to this HF account."""
    row = conn.execute("SELECT * FROM principals WHERE lower(hf_username) = lower(?) AND identity_type = 'user'",
                       (username,)).fetchone()
    if row is None:
        raise not_found("no board identity is linked to this Hugging Face account")
    if row["disabled"]:
        raise forbidden("this account is disabled")
    _, token = _new_credential(conn, row["id"], None, now, replace=False)
    audit(conn, row["id"], "signin.hf", f"principal:{row['id']}", None, now)
    return {"token": token, "principal": principal_json(conn, row)}


# ---------------------------------------------------------------------- channels


def _channel_json(conn, row) -> dict:
    out = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "description": row["description"],
        "archived": bool(row["archived"]),
        "last_seq": row["next_seq"] - 1,
        "edit_rev": row["edit_rev"],
        "doc_version": row["doc_version"],
        "created_at": row["created_at"],
    }
    if row["kind"] == "dm":
        out["members"] = [_brief(conn, r[0]) for r in conn.execute(
            "SELECT principal_id FROM channel_members WHERE channel_id = ? ORDER BY principal_id", (row["id"],))]
    return out


def list_channels(conn, p: Principal, name: str | None = None) -> list[dict]:
    ids = visible_channel_ids(conn, p)
    sql, args = "SELECT * FROM channels", []
    clauses = []
    if ids is not None:
        if not ids:
            return []
        clauses.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    if name is not None:
        clauses.append("name = ?")
        args.append(name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    out = []
    for r in conn.execute(sql + " ORDER BY kind = 'dm', id", args).fetchall():
        item = _channel_json(conn, r)
        item.update(unread_counts(conn, p, r))
        out.append(item)
    return out


def create_channel(conn, p: Principal, name, description, private, now: float) -> tuple[dict, bool]:
    if not p.may_create_channels:
        raise forbidden("not allowed to create channels")
    if not isinstance(name, str) or not CHANNEL_NAME.match(name):
        raise bad_request("name must match " + CHANNEL_NAME.pattern)
    if description is not None and (not isinstance(description, str) or len(description) > 1024):
        raise bad_request("description must be a string of at most 1024 characters")
    if not isinstance(private, bool):
        raise bad_request("private must be true or false")
    if private and not get_settings(conn)["allow_private_channels"]:
        raise forbidden("private channels are switched off on this board")
    existing = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        # Retry-safe for the original creator; everyone else just learns the name is taken.
        if existing["created_by"] == p.id:
            return _channel_json(conn, existing), False
        raise conflict("channel_exists", "a channel with this name already exists")
    cur = conn.execute(
        "INSERT INTO channels (name, description, created_by, created_at, kind) VALUES (?, ?, ?, ?, ?)",
        (name, description, p.id, now, "private" if private else "public"),
    )
    cid = cur.lastrowid
    conn.execute("INSERT INTO channel_members (channel_id, principal_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                 (cid, p.id, p.id, now))
    audit(conn, p.id, "channel.create", f"channel:{cid}", {"name": name, "private": private}, now)
    return _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()), True


def archive_channel(conn, p: Principal, channel_id: int, now: float) -> dict:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise not_found("channel not found")
    if not _may_manage_channel(conn, p, row):
        raise forbidden("only admins and the channel's creator can archive it")
    conn.execute("UPDATE channels SET archived = 1 WHERE id = ?", (channel_id,))
    audit(conn, p.id, "channel.archive", f"channel:{channel_id}", None, now)
    return _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())


def _may_manage_channel(conn, p: Principal, row) -> bool:
    return p.is_admin or (row["created_by"] == p.id and p.may_post)


def channel_info(conn, p: Principal, channel_id: int) -> dict:
    """Everything the channel info widget shows: who made it, counts, members and who posts."""
    row = channel_for(conn, p, channel_id)
    out = _channel_json(conn, row)
    count = lambda sql: conn.execute(sql, (channel_id,)).fetchone()[0]
    out.update({
        "created_by": _brief(conn, row["created_by"]),
        "messages": row["next_seq"] - 1,
        "threads": count("SELECT COUNT(DISTINCT thread_id) FROM messages WHERE channel_id = ? AND thread_id IS NOT NULL"),
        "files": count("SELECT COUNT(*) FROM attachments WHERE channel_id = ? AND published = 1"),
        "last_message_at": count("SELECT MAX(created_at) FROM messages WHERE channel_id = ?"),
        # public channels are open to everyone but guests; members are those added explicitly
        "audience": "everyone" if row["kind"] == "public" else "members",
        "members": [dict(_brief(conn, r[0]), added_at=r[1]) for r in conn.execute(
            "SELECT principal_id, added_at FROM channel_members WHERE channel_id = ? ORDER BY added_at", (channel_id,))],
        "posters": [dict(_brief(conn, r[0]), messages=r[1], last_at=r[2]) for r in conn.execute(
            """SELECT sender_id, COUNT(*), MAX(created_at) FROM messages WHERE channel_id = ?
               GROUP BY sender_id ORDER BY COUNT(*) DESC LIMIT 50""", (channel_id,))],
        "can_edit": row["kind"] != "dm" and _may_manage_channel(conn, p, row),
        "can_archive": row["kind"] != "dm" and _may_manage_channel(conn, p, row),
        "can_delete": row["kind"] != "dm" and p.is_admin,
    })
    return out


def update_channel(conn, p: Principal, channel_id: int, changes: Any, now: float) -> dict:
    """Rename, describe, archive or unarchive a channel (admins and the channel's creator)."""
    row = channel_for(conn, p, channel_id)
    if not isinstance(changes, dict) or not changes or set(changes) - {"name", "description", "archived"}:
        raise bad_request("supported fields: name, description, archived")
    if row["kind"] == "dm":
        raise bad_request("direct messages cannot be renamed or archived")
    if ("name" in changes or "description" in changes) and not _may_manage_channel(conn, p, row):
        raise forbidden("only admins and the channel's creator can rename it")
    if "archived" in changes and not _may_manage_channel(conn, p, row):
        raise forbidden("only admins and the channel's creator can archive it")
    if "name" in changes:
        name = changes["name"]
        if not isinstance(name, str) or not CHANNEL_NAME.match(name):
            raise bad_request("name must match " + CHANNEL_NAME.pattern)
        taken = conn.execute("SELECT id FROM channels WHERE name = ? AND id != ?", (name, channel_id)).fetchone()
        if taken:
            raise conflict("channel_exists", "a channel with this name already exists")
        conn.execute("UPDATE channels SET name = ? WHERE id = ?", (name, channel_id))
    if "description" in changes:
        d = changes["description"]
        if d is not None and (not isinstance(d, str) or len(d) > 1024):
            raise bad_request("description must be a string of at most 1024 characters")
        conn.execute("UPDATE channels SET description = ? WHERE id = ?", ((d or "").strip() or None, channel_id))
    if "archived" in changes:
        if not isinstance(changes["archived"], bool):
            raise bad_request("archived must be true or false")
        conn.execute("UPDATE channels SET archived = ? WHERE id = ?", (int(changes["archived"]), channel_id))
    audit(conn, p.id, "channel.update", f"channel:{channel_id}", {"before": {"name": row["name"]}, "changes": changes}, now)
    return channel_info(conn, p, channel_id)


def open_dm(conn, p: Principal, other_id, now: float) -> tuple[dict, bool]:
    """The direct-message channel between p and another identity (created on first use)."""
    if not p.may_post:
        raise forbidden("viewers cannot send direct messages")
    if not isinstance(other_id, int) or isinstance(other_id, bool) or other_id == p.id:
        raise bad_request("with must be the id of another identity")
    other = conn.execute("SELECT * FROM principals WHERE id = ?", (other_id,)).fetchone()
    if other is None:
        raise not_found("identity not found")
    _check_visible(conn, p, other_id)
    if other["disabled"]:
        raise conflict("identity_disabled", "this identity is disabled")
    a, b = sorted((p.id, other_id))
    name = f"dm:{a}:{b}"
    row = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return _channel_json(conn, row), False
    cid = conn.execute("INSERT INTO channels (name, created_by, created_at, kind) VALUES (?, ?, ?, 'dm')",
                       (name, p.id, now)).lastrowid
    conn.executemany("INSERT INTO channel_members (channel_id, principal_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                     [(cid, a, p.id, now), (cid, b, p.id, now)])
    return _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()), True


def list_members(conn, p: Principal, channel_id: int) -> list[dict]:
    channel_for(conn, p, channel_id)
    rows = conn.execute("SELECT principal_id, added_by, added_at FROM channel_members WHERE channel_id = ? "
                        "ORDER BY added_at", (channel_id,)).fetchall()
    return [dict(_brief(conn, r[0]), added_by=r[1], added_at=r[2]) for r in rows]


def _check_can_add_to(conn, p: Principal, channel_id: int):
    """The rule for adding someone to a channel (directly, or through an invite)."""
    ch = channel_for(conn, p, channel_id, "messages:write")
    if ch["kind"] == "dm":
        raise bad_request("direct messages always have exactly two people")
    if ch["kind"] == "private" and not (p.is_admin or _is_channel_member(conn, channel_id, p.id)):
        raise forbidden("only members of a private channel can add people to it")
    return ch


def add_member(conn, p: Principal, channel_id: int, target_id, now: float) -> list[dict]:
    _check_can_add_to(conn, p, channel_id)
    if not isinstance(target_id, int) or isinstance(target_id, bool):
        raise bad_request("principal_id must be an identity id")
    t = conn.execute("SELECT * FROM principals WHERE id = ?", (target_id,)).fetchone()
    if t is None:
        raise not_found("identity not found")
    _check_visible(conn, p, target_id)
    if t["disabled"]:
        raise conflict("identity_disabled", "this identity is disabled")
    conn.execute("INSERT OR IGNORE INTO channel_members (channel_id, principal_id, added_by, added_at) "
                 "VALUES (?, ?, ?, ?)", (channel_id, target_id, p.id, now))
    audit(conn, p.id, "channel.add_member", f"channel:{channel_id}", {"principal_id": target_id}, now)
    return list_members(conn, p, channel_id)


def remove_member(conn, p: Principal, channel_id: int, target_id: int, now: float) -> list[dict] | dict:
    ch = channel_for(conn, p, channel_id)
    if ch["kind"] == "dm":
        raise bad_request("you cannot leave a direct message")
    row = conn.execute("""SELECT cm.added_by, t.owner_id FROM channel_members cm JOIN principals t ON t.id = cm.principal_id
                          WHERE cm.channel_id = ? AND cm.principal_id = ?""", (channel_id, target_id)).fetchone()
    if row is None:
        raise not_found("not a member of this channel")
    if not (p.is_admin or p.id == target_id or row["added_by"] == p.id or row["owner_id"] == p.id):
        raise forbidden("you may not remove this member")
    conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND principal_id = ?", (channel_id, target_id))
    audit(conn, p.id, "channel.remove_member", f"channel:{channel_id}", {"principal_id": target_id}, now)
    if p.id == target_id and not can_see(conn, p, ch):
        return {"left": True}
    return list_members(conn, p, channel_id)


# ----------------------------------------------------------------------- invites


INVITE_PUBLIC_FIELDS = ("id", "kind", "role", "identity_type", "can_create_channels", "max_uses", "uses",
                        "expires_at", "revoked_at", "label", "created_at", "agent_name", "agent_display",
                        "join_open_channels")


def _handle_from(display: str) -> str:
    """A handle from a display name: "Eval Runner 2" -> "eval-runner-2"."""
    return re.sub(r"[^a-z0-9._-]+", "-", display.lower()).strip("-._")[:64]


def _invite_status(row, now: float) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        return "used up"
    return "active"


def _invite_json(conn, row, now: float) -> dict:
    out = {k: row[k] for k in INVITE_PUBLIC_FIELDS}
    out["can_create_channels"] = bool(out["can_create_channels"])
    out["status"] = _invite_status(row, now)
    out["created_by"] = _brief(conn, row["created_by"])
    out["channels"] = _invite_channels(conn, row)
    out["joined"] = [dict(_brief(conn, r[0]), joined_at=r[1]) for r in conn.execute(
        "SELECT id, created_at FROM principals WHERE invite_id = ? ORDER BY created_at", (row["id"],))]
    return out


def _invite_channels(conn, row) -> list[dict]:
    ids = json.loads(row["channel_ids"]) if row["channel_ids"] else []
    out = []
    for cid in ids:
        ch = conn.execute("SELECT id, name, kind FROM channels WHERE id = ?", (cid,)).fetchone()
        if ch is not None:
            out.append({"id": ch["id"], "name": ch["name"], "kind": ch["kind"]})
    return out


def create_invite(conn, p: Principal, req: dict, now: float) -> dict:
    """An invite link. People invites (admins only) carry a role; agent invites
    (members and admins) make agents or service accounts owned by the inviter."""
    if not isinstance(req, dict):
        raise bad_request("request body must be a JSON object")
    kind = req.get("kind", "agent")
    max_uses, expires_in, label = req.get("max_uses"), req.get("expires_in_seconds"), req.get("label")
    if max_uses is not None and (not isinstance(max_uses, int) or isinstance(max_uses, bool) or max_uses < 1):
        raise bad_request("max_uses must be a positive integer, or null for unlimited")
    if expires_in is not None and (not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
                                   or expires_in <= 0):
        raise bad_request("expires_in_seconds must be positive, or null for never")
    if label is not None and (not isinstance(label, str) or len(label) > 80):
        raise bad_request("label must be at most 80 characters")
    if kind == "human":
        if not p.is_admin:
            raise forbidden("only admins can invite people")
        role, identity_type, create = req.get("role", "member"), "user", False
        if role not in auth.HUMAN_ROLES:
            raise bad_request(f"role must be one of {list(auth.HUMAN_ROLES)}")
    elif kind == "agent":
        if not p.may_invite_agents:
            raise forbidden("only members and admins can invite agents")
        role = req.get("role", "member")
        identity_type = req.get("identity_type", "agent")
        create = req.get("can_create_channels", False)
        if role not in auth.AGENT_ROLES:
            raise bad_request(f"agent role must be one of {list(auth.AGENT_ROLES)}")
        if role in auth.ADMIN_ONLY_AGENT_ROLES and not p.is_admin:
            raise forbidden("only admins can invite admin or reader agents")
        if identity_type not in ("agent", "system"):
            raise bad_request("identity_type must be agent or system")
        if not isinstance(create, bool) or (create and role != "member"):
            raise bad_request("can_create_channels must be true or false, and only for member agents")
    else:
        raise bad_request("kind must be human or agent")
    agent_display = agent_name = None
    join_open = req.get("join_open_channels", False)
    if not isinstance(join_open, bool) or (join_open and kind != "agent"):
        raise bad_request("join_open_channels must be true or false, and only for agent invites")
    if kind == "agent" and (req.get("agent_name") or req.get("agent_handle")):
        # a named invite: the link makes exactly this agent (joining again re-keys it)
        agent_display = req.get("agent_name")
        if agent_display is not None and (not isinstance(agent_display, str) or not agent_display.strip() or len(agent_display) > 128):
            raise bad_request("agent_name must be 1 to 128 characters")
        agent_display = agent_display.strip() if agent_display else None
        agent_name = req.get("agent_handle") or _handle_from(agent_display or "")
        if not isinstance(agent_name, str) or not EXTERNAL_ID.match(agent_name):
            raise bad_request("agent_handle must be letters, digits, dots, dashes or underscores (it is made from agent_name if left out)")
    channels = req.get("channels") or []
    if not isinstance(channels, list) or len(channels) > 200:
        raise bad_request("channels must be a list of channel ids or names")
    channel_ids = []
    for ref in channels:
        if isinstance(ref, str):
            row = conn.execute("SELECT id FROM channels WHERE name = ?", (ref,)).fetchone()
            if row is None:
                raise not_found(f"channel {ref!r} not found")
            ref = row["id"]
        if not isinstance(ref, int) or isinstance(ref, bool):
            raise bad_request("channels must be a list of channel ids or names")
        _check_can_add_to(conn, p, ref)          # you can only hand out channels you could add someone to
        if ref not in channel_ids:
            channel_ids.append(ref)
    code = secrets.token_urlsafe(16)
    cur = conn.execute(
        """INSERT INTO invites (code_hash, created_by, created_at, kind, role, identity_type, can_create_channels,
                                max_uses, expires_at, label, channel_ids, agent_name, agent_display, join_open_channels)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (hashlib.sha256(code.encode()).digest(), p.id, now, kind, role, identity_type, int(create), max_uses,
         now + expires_in if expires_in else None, label, json.dumps(channel_ids) if channel_ids else None,
         agent_name, agent_display, int(join_open)))
    audit(conn, p.id, "invite.create", f"invite:{cur.lastrowid}", {"kind": kind, "role": role}, now)
    out = _invite_json(conn, conn.execute("SELECT * FROM invites WHERE id = ?", (cur.lastrowid,)).fetchone(), now)
    out["code"] = code
    return out


def list_invites(conn, p: Principal, now: float) -> list[dict]:
    if p.is_admin:
        rows = conn.execute("SELECT * FROM invites WHERE kind != 'signin' ORDER BY id DESC")
    elif p.may_invite_agents:
        rows = conn.execute("SELECT * FROM invites WHERE kind != 'signin' AND created_by = ? ORDER BY id DESC", (p.id,))
    else:
        raise forbidden("only members and admins have invites")
    return [_invite_json(conn, r, now) for r in rows]


def revoke_invite(conn, p: Principal, invite_id: int, now: float) -> dict:
    row = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
    if row is None or row["kind"] == "signin" or not (p.is_admin or row["created_by"] == p.id):
        raise not_found("invite not found")
    if row["revoked_at"] is None:
        conn.execute("UPDATE invites SET revoked_at = ? WHERE id = ?", (now, invite_id))
        audit(conn, p.id, "invite.revoke", f"invite:{invite_id}", None, now)
    return _invite_json(conn, conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone(), now)


def create_signin_link(conn, p: Principal, target_id: int, now: float) -> dict:
    """A one-time, 15-minute code that signs a person in on another device."""
    t = _principal_row(conn, target_id)
    if t["identity_type"] != "user":
        raise bad_request("sign-in links are for people; agents get tokens")
    if not (p.id == target_id or p.is_admin):
        raise forbidden("only admins can make sign-in links for someone else")
    code = secrets.token_urlsafe(16)
    conn.execute("""INSERT INTO invites (code_hash, created_by, created_at, kind, role, identity_type, max_uses,
                                         expires_at, target_id)
                    VALUES (?, ?, ?, 'signin', ?, 'user', 1, ?, ?)""",
                 (hashlib.sha256(code.encode()).digest(), p.id, now, t["role"], now + 900, target_id))
    audit(conn, p.id, "signin.link", f"principal:{target_id}", None, now)
    return {"code": code, "expires_at": now + 900}


def _find_invite(conn, code) -> sqlite3.Row | None:
    if not isinstance(code, str) or not 8 <= len(code) <= 64:
        return None
    return conn.execute("SELECT * FROM invites WHERE code_hash = ?", (hashlib.sha256(code.encode()).digest(),)).fetchone()


def lookup_invite(conn, code: str, now: float) -> dict:
    """What a code is for, shown on the join page before anyone signs in."""
    row = _find_invite(conn, code)
    if row is None:
        raise not_found("this invite link is not valid")
    inviter = _brief(conn, row["created_by"])
    out = {"kind": row["kind"], "role": row["role"], "identity_type": row["identity_type"],
           "can_create_channels": bool(row["can_create_channels"]), "status": _invite_status(row, now),
           "invited_by": inviter["display_name"] or inviter["external_id"] if inviter else None}
    if row["kind"] == "signin":
        out["name"] = (_brief(conn, row["target_id"]) or {}).get("display_name")
    else:
        out["channels"] = [c["name"] for c in _invite_channels(conn, row)]
        if row["agent_name"]:
            out["agent_name"], out["agent_display"] = row["agent_name"], row["agent_display"]
        out["join_open_channels"] = bool(row["join_open_channels"])
    return out


def _join_channels(conn, invite, principal_id: int, now: float) -> None:
    chans = _invite_channels(conn, invite)
    if invite["join_open_channels"]:   # every public, non-archived channel at the time of joining
        chans += [dict(r) for r in conn.execute("SELECT * FROM channels WHERE kind = 'public' AND archived = 0")]
    for ch in chans:
        if ch["kind"] != "dm":
            conn.execute("INSERT OR IGNORE INTO channel_members (channel_id, principal_id, added_by, added_at) "
                         "VALUES (?, ?, ?, ?)", (ch["id"], principal_id, invite["created_by"], now))


def join(conn, code, name, display_name, now: float) -> dict:
    """Redeem an invite (or a sign-in link). Returns the identity and its token."""
    row = _find_invite(conn, code)
    if row is None:
        raise not_found("this invite link is not valid")
    status = _invite_status(row, now)
    if status != "active":
        raise ApiError(410, "invite_inactive", f"this invite link is {status}")
    if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()
                                     or len(display_name) > 128):
        raise bad_request("display_name must be 1 to 128 characters")
    display_name = display_name.strip() if display_name else None

    if row["kind"] == "signin":
        t = _principal_row(conn, row["target_id"])
        if t["disabled"]:
            raise forbidden("this account is disabled")
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (row["id"],))
        _, token = _new_credential(conn, t["id"], row["created_by"], now, replace=False)
        return {"token": token, "principal": principal_json(conn, t), "created": False, "next": JOIN_NEXT}

    inviter = _principal_row(conn, row["created_by"])
    if inviter["disabled"]:
        raise ApiError(410, "invite_inactive", "the person who made this invite is no longer active")

    if row["kind"] == "human":
        if not isinstance(name, str) or not PERSON_NAME.match(name):
            raise bad_request("name must be 1 to 64 letters, digits, dots, dashes or underscores")
        if conn.execute("SELECT 1 FROM principals WHERE identity_type = 'user' AND lower(external_id) = lower(?)",
                        (name,)).fetchone():
            raise conflict("name_taken", "this name is taken; pick another")
        pid = conn.execute(
            """INSERT INTO principals (issuer, external_id, identity_type, role, display_name, created_at, created_by,
                                       joined_via, invite_id)
               VALUES (?, ?, 'user', ?, ?, ?, ?, 'invite', ?)""",
            (PEOPLE, name, row["role"], display_name, now, inviter["id"], row["id"])).lastrowid
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (row["id"],))
        _join_channels(conn, row, pid, now)
        _, token = _new_credential(conn, pid, inviter["id"], now)
        audit(conn, pid, "join.human", f"principal:{pid}", {"invite": row["id"], "role": row["role"]}, now)
        return {"token": token, "principal": principal_json(conn, _principal_row(conn, pid)), "created": True, "next": JOIN_NEXT}

    # Agent or service account, owned by the inviter.
    needed = ("admin",) if row["role"] in auth.ADMIN_ONLY_AGENT_ROLES else ("admin", "member")
    if inviter["role"] not in needed:
        raise ApiError(410, "invite_inactive", "the person who made this invite can no longer give out this access")
    if row["agent_name"]:   # a named invite decides the name; the agent need not (and may not) pick another
        if name not in (None, "", row["agent_name"]):
            raise bad_request(f"this invite is for the agent {row['agent_name']!r}: leave name out or use that one")
        name, display_name = row["agent_name"], row["agent_display"] or display_name
    if not isinstance(name, str) or not EXTERNAL_ID.match(name):
        raise bad_request("name must match " + EXTERNAL_ID.pattern)
    issuer = f"owner:{inviter['id']}"
    existing = conn.execute("SELECT * FROM principals WHERE issuer = ? AND external_id = ?", (issuer, name)).fetchone()
    if existing is not None:
        # The same program starting again: same identity, fresh token (the old one stops working).
        if existing["disabled"]:
            raise conflict("identity_disabled", "this agent was removed by its owner")
        _join_channels(conn, row, existing["id"], now)
        _, token = _new_credential(conn, existing["id"], inviter["id"], now)
        audit(conn, existing["id"], "join.again", f"principal:{existing['id']}", {"invite": row["id"]}, now)
        return {"token": token, "principal": principal_json(conn, existing), "created": False, "next": JOIN_NEXT}
    pid = conn.execute(
        """INSERT INTO principals (issuer, external_id, identity_type, role, display_name, created_at, created_by,
                                   joined_via, owner_id, can_create_channels, invite_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'invite', ?, ?, ?)""",
        (issuer, name, row["identity_type"], row["role"], display_name, now, inviter["id"], inviter["id"],
         row["can_create_channels"], row["id"])).lastrowid
    conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (row["id"],))
    _join_channels(conn, row, pid, now)
    _, token = _new_credential(conn, pid, inviter["id"], now)
    audit(conn, pid, "join.agent", f"principal:{pid}", {"invite": row["id"], "role": row["role"]}, now)
    return {"token": token, "principal": principal_json(conn, _principal_row(conn, pid)), "created": True, "next": JOIN_NEXT}


JOIN_NEXT = ("Keep this token secret and send it as `Authorization: Bearer <token>`. "
             "Before doing anything else, read GET /v1/guide (how this board works and its rules) and follow it.")


# ---------------------------------------------------------------------- messages

MESSAGE_SELECT = """
SELECT m.*, p.external_id AS s_external_id, p.identity_type AS s_identity_type,
       p.display_name AS s_display_name, p.role AS s_role, substr(av.sha256, 1, 12) AS s_avatar,
       p.pattern_seed AS s_pattern
FROM messages m JOIN principals p ON p.id = m.sender_id
LEFT JOIN avatars av ON av.principal_id = m.sender_id
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
            "avatar": row["s_avatar"],
            "pattern": row["s_pattern"],
            "role": row["s_role"],
        },
        "body": row["body"],
        "reply_to": row["reply_to"],
        "thread_id": row["thread_id"],
        "attachments": json.loads(row["attachments"]) if row["attachments"] else [],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


def normalize_payload(channel_id: int, payload: Any) -> tuple[dict, str]:
    if not isinstance(payload, dict):
        raise bad_request("request body must be a JSON object")
    unknown = set(payload) - {"body", "reply_to", "attachments"}
    if unknown:
        raise bad_request(f"unknown fields: {sorted(unknown)}")
    body = payload.get("body")
    reply_to = payload.get("reply_to")
    attachments = payload.get("attachments") or []
    if not isinstance(body, str):
        raise bad_request("body must be a string")
    if len(body.encode()) > LIMITS.max_body_bytes:
        raise ApiError(413, "too_large", f"body exceeds {LIMITS.max_body_bytes} bytes")
    if reply_to is not None and (not isinstance(reply_to, int) or isinstance(reply_to, bool)):
        raise bad_request("reply_to must be a message id")
    if (not isinstance(attachments, list) or not all(isinstance(a, str) for a in attachments)
            or len(attachments) > LIMITS.max_attachments_per_message):
        raise bad_request(f"attachments must be a list of at most {LIMITS.max_attachments_per_message} ids")
    normalized = {
        "channel_id": channel_id,
        "body": body,
        "reply_to": reply_to,
        "attachments": list(dict.fromkeys(attachments)),   # keep the order they were attached in
    }
    # The fingerprint sorts attachments (order does not make a different message) and keeps
    # "metadata": None so it matches messages stored before that field was removed.
    fingerprint = dict(normalized, attachments=sorted(normalized["attachments"]), metadata=None)
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
        out = _message_json(prior)
        _attach_mentions(conn, [out])
        _attach_reactions(conn, [out], p.id)
        return out, False
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    thread_id = None
    if normalized["reply_to"] is not None:
        parent = conn.execute("SELECT channel_id, thread_id FROM messages WHERE id = ?",
                              (normalized["reply_to"],)).fetchone()
        if parent is None or parent["channel_id"] != channel_id:
            raise bad_request("reply_to must reference a message in the same channel")
        thread_id = parent["thread_id"] or normalized["reply_to"]  # a reply joins its parent's thread
    for att_id in normalized["attachments"]:
        att = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        if (att is None or att["channel_id"] != channel_id or att["state"] != "ready"
                or (att["uploader_id"] != p.id and not att["published"])):
            raise bad_request(f"attachment {att_id} is not a ready upload in this channel")
    seq = channel["next_seq"]
    cur = conn.execute(
        """INSERT INTO messages (channel_id, seq, sender_id, body, reply_to, attachments,
                                 request_id, payload_hash, created_at, thread_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id, seq, p.id, normalized["body"],
            normalized["reply_to"],
            json.dumps(normalized["attachments"]) if normalized["attachments"] else None,
            request_id, payload_hash, now, thread_id,
        ),
    )
    conn.execute("UPDATE channels SET next_seq = ? WHERE id = ?", (seq + 1, channel_id))
    if normalized["attachments"]:
        marks = ",".join("?" * len(normalized["attachments"]))
        conn.execute(f"UPDATE attachments SET published = 1 WHERE id IN ({marks})", normalized["attachments"])
    _record_mentions(conn, channel, cur.lastrowid, normalized["body"], p.id)
    row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (cur.lastrowid,)).fetchone()
    out = _message_json(row)
    _attach_mentions(conn, [out])
    _attach_reactions(conn, [out], p.id)
    return out, True


def edit_message(conn, p: Principal, channel_id: int, message_id: int, payload: Any, now: float) -> tuple[dict, bool]:
    """Replace the body of your own message (people only). Mentions are worked out again from the new text."""
    if not isinstance(payload, dict) or set(payload) != {"body"} or not isinstance(payload["body"], str):
        raise bad_request('request body must be {"body": "..."}')
    body = payload["body"]
    if len(body.encode()) > LIMITS.max_body_bytes:
        raise ApiError(413, "too_large", f"body exceeds {LIMITS.max_body_bytes} bytes")
    channel = channel_for(conn, p, channel_id, "messages:write")
    row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ? AND m.channel_id = ?", (message_id, channel_id)).fetchone()
    if row is None:
        raise not_found("no such message in this channel")
    if row["sender_id"] != p.id:
        raise forbidden("you can only edit your own messages")
    if not p.is_human:   # agent output stays exactly as written; corrections go in a follow-up message
        raise forbidden("agents cannot edit messages: post a follow-up (reply in the thread) instead")
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    if not body.strip() and not row["attachments"]:
        raise bad_request("body must not be empty")
    changed = body != row["body"]
    if changed:
        rev = channel["edit_rev"] + 1
        conn.execute("UPDATE channels SET edit_rev = ? WHERE id = ?", (rev, channel_id))
        version = conn.execute("SELECT COUNT(*) FROM message_revisions WHERE message_id = ?", (message_id,)).fetchone()[0] + 1
        conn.execute("INSERT INTO message_revisions (message_id, version, body, written_at, replaced_at) VALUES (?, ?, ?, ?, ?)",
                     (message_id, version, row["body"], row["edited_at"] or row["created_at"], now))
        conn.execute("UPDATE messages SET body = ?, edited_at = ?, edit_rev = ? WHERE id = ?", (body, now, rev, message_id))
        conn.execute("DELETE FROM message_mentions WHERE message_id = ?", (message_id,))
        _record_mentions(conn, channel, message_id, body, p.id)
        audit(conn, p.id, "message.edit", f"message:{message_id}", {"version": version + 1}, now)
        row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()
    out = _message_json(row)
    _attach_mentions(conn, [out])
    _attach_reactions(conn, [out], p.id)
    return out, changed


def message_revisions(conn, p: Principal, channel_id: int, message_id: int) -> dict:
    """Every version of a message, oldest first; the last one is the current body."""
    channel_for(conn, p, channel_id, "messages:read")
    row = conn.execute("SELECT * FROM messages WHERE id = ? AND channel_id = ?", (message_id, channel_id)).fetchone()
    if row is None:
        raise not_found("no such message in this channel")
    versions = [{"version": r["version"], "body": r["body"], "written_at": r["written_at"], "replaced_at": r["replaced_at"]}
                for r in conn.execute("SELECT * FROM message_revisions WHERE message_id = ? ORDER BY version", (message_id,))]
    versions.append({"version": len(versions) + 1, "body": row["body"], "written_at": row["edited_at"] or row["created_at"],
                     "replaced_at": None})
    return {"message_id": message_id, "versions": versions}


def read_edits(conn, p: Principal, channel_id: int, after_rev: int, limit: int) -> dict:
    """Messages edited since edit cursor `after_rev`, oldest edit first (each message once, in its latest form)."""
    channel = channel_for(conn, p, channel_id, "messages:read")
    rows = conn.execute(MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.edit_rev > ? ORDER BY m.edit_rev LIMIT ?",
                        (channel_id, after_rev, limit)).fetchall()
    edits = [_message_json(r) for r in rows]
    _attach_thread_summaries(conn, edits, p)
    _attach_mentions(conn, edits)
    _attach_reactions(conn, edits, p.id)
    cursor = rows[-1]["edit_rev"] if len(rows) == limit else channel["edit_rev"]   # a full page may have more
    return {"edits": edits, "edit_cursor": max(cursor, after_rev)}


def read_messages(conn, p: Principal, channel_id: int, after: int, limit: int, before: int | None = None,
                  top_level: bool = False, thread: int | None = None) -> dict:
    """A page of history in seq order.

    after:     messages with seq > after (the default; `next_cursor` continues it).
    before:    instead, the newest `limit` messages with seq < before (for scrolling back).
    top_level: only messages that start a thread or stand alone (replies are left out).
    thread:    only the thread started by this message id (the message itself plus replies).
    Messages that have replies carry a `replies` summary.
    """
    channel_for(conn, p, channel_id, "messages:read")
    where, args = ["m.channel_id = ?"], [channel_id]
    if thread is not None:
        where.append("(m.id = ? OR m.thread_id = ?)")
        args += [thread, thread]
    if top_level:
        where.append("m.thread_id IS NULL")
    if before is not None:
        where.append("m.seq < ?")
        args.append(before)
        order = "DESC"
    else:
        where.append("m.seq > ?")
        args.append(after)
        order = "ASC"
    rows = conn.execute(
        MESSAGE_SELECT + " WHERE " + " AND ".join(where) + f" ORDER BY m.seq {order} LIMIT ?",
        (*args, limit + 1),
    ).fetchall()
    messages, size = [], 0
    for row in rows[:limit]:
        size += len(row["body"].encode()) + 256
        if messages and size > LIMITS.max_page_bytes:
            break
        messages.append(_message_json(row))
    has_more = len(messages) < len(rows)
    if before is not None:
        messages.reverse()
    _attach_thread_summaries(conn, messages, p)
    _attach_mentions(conn, messages)
    _attach_reactions(conn, messages, p.id)
    if before is not None:
        cursor = messages[-1]["seq"] if messages else before - 1
    else:
        cursor = messages[-1]["seq"] if messages else after
    return {"messages": messages, "next_cursor": cursor, "has_more": has_more}


def _attach_thread_summaries(conn, messages: list[dict], viewer: Principal | None = None) -> None:
    roots = [m["id"] for m in messages if m["thread_id"] is None]
    if not roots:
        return
    marks = ",".join("?" * len(roots))
    counts = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"""SELECT thread_id, COUNT(*), MAX(created_at) FROM messages
            WHERE thread_id IN ({marks}) GROUP BY thread_id""", roots)}
    if not counts:
        return
    people: dict[int, list[dict]] = {}
    for r in conn.execute(
        f"""SELECT m.thread_id, p.id, p.external_id, p.display_name, p.identity_type, MAX(m.seq) AS last,
                   substr(av.sha256, 1, 12), p.pattern_seed, p.role
            FROM messages m JOIN principals p ON p.id = m.sender_id
            LEFT JOIN avatars av ON av.principal_id = p.id
            WHERE m.thread_id IN ({marks}) GROUP BY m.thread_id, p.id ORDER BY last DESC""", roots):
        people.setdefault(r[0], []).append(
            {"id": r[1], "external_id": r[2], "display_name": r[3], "identity_type": r[4], "avatar": r[6],
             "pattern": r[7], "role": r[8]})
    for_me = _threads_for_me(conn, viewer, roots, messages) if viewer is not None else {}
    for m in messages:
        if m["id"] in counts:
            n, last = counts[m["id"]]
            m["replies"] = {"count": n, "last_at": last, "participants": people.get(m["id"], [])[:5],
                            "for_me_unread": for_me.get(m["id"], 0)}


def _threads_for_me(conn, p: Principal, roots: list[int], messages: list[dict]) -> dict[int, int]:
    """Per thread: replies for me (mentions, replies in a thread I am in, DMs) that I have not read in the thread."""
    out: dict[int, int] = {}
    by_channel: dict[int, list[int]] = {}
    for m in messages:
        if m["id"] in roots:
            by_channel.setdefault(m["channel_id"], []).append(m["id"])
    for cid, ids in by_channel.items():
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
        row = conn.execute("SELECT thread_floor FROM channel_reads WHERE principal_id = ? AND channel_id = ?", (p.id, cid)).fetchone()
        params = {"me": p.id, "floor": row[0] if row else 0, "has_row": 1 if row else 0, "baseline": _reads_baseline(conn, p),
                  "dm": 1 if ch["kind"] == "dm" and _is_channel_member(conn, cid, p.id) else 0}
        marks = ",".join(str(int(i)) for i in ids)
        for tid, n in conn.execute(f"""
                SELECT m.thread_id, COUNT(*) FROM messages m
                LEFT JOIN message_mentions mm ON mm.message_id = m.id AND mm.principal_id = :me
                LEFT JOIN thread_reads tr ON tr.principal_id = :me AND tr.thread_id = m.thread_id
                WHERE m.thread_id IN ({marks}) AND m.sender_id != :me AND {FOR_ME} AND {THREAD_UNREAD}
                  AND (:has_row = 1 OR m.created_at > :baseline)
                GROUP BY m.thread_id""", params):
            out[tid] = n
    return out


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
    if row["uploader_id"] == p.id or p.is_admin:
        pass
    else:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (row["channel_id"],)).fetchone()
        if not (row["published"] and can_see(conn, p, ch)):
            raise not_found("attachment not found")
    out = _attachment_json(row)
    out["storage_key"] = row["storage_key"]
    return out


# ---------------------------------------------------------------------- profiles


def profile(conn, viewer: Principal, target_id: int, now: float, days: int = 30) -> dict:
    """Who someone is and what they wrote, counted only in channels the viewer can see."""
    row = _principal_row(conn, target_id)
    _check_visible(conn, viewer, target_id)
    readable = visible_channel_ids(conn, viewer)
    if readable is None:
        scope, args = "", []
    elif readable:
        scope, args = f" AND m.channel_id IN ({','.join('?' * len(readable))})", readable
    else:
        scope, args = " AND 0", []
    stats = conn.execute(
        f"SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM messages m WHERE sender_id = ?{scope}",
        (target_id, *args)).fetchone()
    per_channel = [
        {"channel_id": r[0], "name": r[1], "kind": r[2], "count": r[3]}
        for r in conn.execute(
            f"""SELECT c.id, c.name, c.kind, COUNT(*) FROM messages m JOIN channels c ON c.id = m.channel_id
                WHERE m.sender_id = ?{scope} GROUP BY c.id ORDER BY COUNT(*) DESC, c.name""",
            (target_id, *args))]
    start_day = int(now // 86400) - (days - 1)
    by_day = dict(conn.execute(
        f"""SELECT CAST(created_at / 86400 AS INTEGER) AS d, COUNT(*) FROM messages m
            WHERE sender_id = ? AND created_at >= ?{scope} GROUP BY d""",
        (target_id, start_day * 86400, *args)).fetchall())
    invite = None
    if row["invite_id"] is not None:
        inv = conn.execute("SELECT label, kind FROM invites WHERE id = ?", (row["invite_id"],)).fetchone()
        invite = {"label": inv["label"], "kind": inv["kind"]} if inv else None
    out = principal_json(conn, row)
    if not viewer.is_admin and viewer.id != target_id and row["owner_id"] != viewer.id:
        out.pop("hf_username")
    out.update({
        "created_by": _brief(conn, row["created_by"]),
        "joined_via": row["joined_via"],
        "invite": invite,
        "agents": [_brief(conn, r[0]) for r in conn.execute(
            "SELECT id FROM principals WHERE owner_id = ? AND disabled = 0 ORDER BY id", (target_id,))],
        "messages": {"count": stats[0], "first_at": stats[1], "last_at": stats[2]},
        "per_channel": per_channel,
        "daily": [{"day": (start_day + i) * 86400, "count": by_day.get(start_day + i, 0)} for i in range(days)],
    })
    return out


# ----------------------------------------------------------------------- avatars

# Raster formats only, recognised by their first bytes. SVG is refused: it can carry script.
_IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _sniff_image(data: bytes) -> str | None:
    for magic, ctype in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _avatar_version(conn, principal_id: int) -> str | None:
    row = conn.execute("SELECT substr(sha256, 1, 12) FROM avatars WHERE principal_id = ?", (principal_id,)).fetchone()
    return row[0] if row else None


def _may_edit_avatar(conn, actor: Principal, target_id: int) -> None:
    if conn.execute("SELECT 1 FROM principals WHERE id = ?", (target_id,)).fetchone() is None:
        raise not_found("identity not found")
    if actor.id != target_id and not actor.is_admin:
        raise forbidden("you can only change your own picture")


def set_avatar(conn, actor: Principal, target_id: int, data: bytes, now: float) -> dict:
    _may_edit_avatar(conn, actor, target_id)
    if len(data) > LIMITS.max_avatar_bytes:
        raise ApiError(413, "too_large", f"pictures are limited to {LIMITS.max_avatar_bytes // 1024} KB")
    ctype = _sniff_image(data)
    if ctype is None:
        raise bad_request("picture must be a PNG, JPEG, GIF or WebP image")
    digest = hashlib.sha256(data).hexdigest()
    conn.execute(
        """INSERT INTO avatars (principal_id, content_type, data, sha256, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (principal_id) DO UPDATE SET content_type = excluded.content_type, data = excluded.data,
               sha256 = excluded.sha256, updated_at = excluded.updated_at""",
        (target_id, ctype, data, digest, now))
    if actor.id != target_id:
        audit(conn, actor.id, "avatar.set", f"principal:{target_id}", None, now)
    return {"avatar": digest[:12], "content_type": ctype, "size": len(data)}


def delete_avatar(conn, actor: Principal, target_id: int, now: float) -> dict:
    _may_edit_avatar(conn, actor, target_id)
    conn.execute("DELETE FROM avatars WHERE principal_id = ?", (target_id,))
    if actor.id != target_id:
        audit(conn, actor.id, "avatar.delete", f"principal:{target_id}", None, now)
    return {"avatar": None}


def get_avatar(conn, target_id: int) -> tuple[str, bytes, str]:
    row = conn.execute("SELECT content_type, data, sha256 FROM avatars WHERE principal_id = ?",
                       (target_id,)).fetchone()
    if row is None:
        raise not_found("no uploaded picture")
    return row[0], bytes(row[1]), row[2]


def set_pattern(conn, actor: Principal, target_id: int, seed, now: float) -> dict:
    """Keep a regenerated pattern. Choosing a pattern also drops an uploaded picture."""
    _may_edit_avatar(conn, actor, target_id)
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2 ** 32:
        raise bad_request("seed must be an integer from 0 to 4294967295")
    conn.execute("UPDATE principals SET pattern_seed = ? WHERE id = ?", (seed, target_id))
    conn.execute("DELETE FROM avatars WHERE principal_id = ?", (target_id,))
    if actor.id != target_id:
        audit(conn, actor.id, "pattern.set", f"principal:{target_id}", None, now)
    return {"pattern": seed, "avatar": None}


# ---------------------------------------------------------------------- mentions

MENTION = re.compile(r"(?<![\w@/])@([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})")


def _principal_obj(row) -> Principal:
    return Principal(id=row["id"], issuer=row["issuer"], external_id=row["external_id"],
                     identity_type=row["identity_type"], role=row["role"], display_name=row["display_name"],
                     owner_id=row["owner_id"], can_create_channels=bool(row["can_create_channels"]), credential_id="")


def resolve_mention(conn, name: str) -> list:
    """Who `@name` means: a username, an HF username, or a person who joined with HF."""
    name = name.rstrip(".,:;-")
    return conn.execute(
        """SELECT * FROM principals WHERE disabled = 0 AND (lower(external_id) = lower(?) OR lower(hf_username) = lower(?)
           OR (identity_type = 'user' AND lower(external_id) = 'hf:' || lower(?)))""", (name, name, name)).fetchall()


def _mention_names(row, former: tuple = ()) -> set[str]:
    """Everything `@...` may say to mean this identity: display name (spaces allowed), username, HF username,
    former display names, and the handle read as words ("leandros-intern" -> "leandros intern")."""
    names = {row["display_name"], row["external_id"], row["hf_username"], *former}
    handle = row["external_id"][3:] if row["external_id"].startswith("hf:") else row["external_id"]
    names.add(handle)
    if any(c in handle for c in "-_."):
        names.add(re.sub(r"[-_.]+", " ", handle))
    return {n.strip().lower() for n in names if n and n.strip()}


def _mention_candidates(conn) -> list:
    former: dict[int, list] = {}
    for pid, name in conn.execute("SELECT principal_id, name FROM former_names"):
        former.setdefault(pid, []).append(name)
    return sorted(((name, row) for row in conn.execute("SELECT * FROM principals WHERE disabled = 0")
                   for name in _mention_names(row, tuple(former.get(row["id"], ())))), key=lambda c: -len(c[0]))


def find_mention_spans(conn, body: str) -> list[tuple]:
    """(identity row, the text after @ as written) for each identity mentioned in `body`. At each @, the
    longest matching name wins, so `@Loubna Ben Allal` beats a shorter `@Loubna`; it must end at a word boundary."""
    if "@" not in body:
        return []
    candidates = _mention_candidates(conn)
    low, found = body.lower(), {}
    for i, ch in enumerate(body):
        if ch != "@" or (i and (body[i - 1].isalnum() or body[i - 1] in "@/_")):
            continue
        best = None
        for name, row in candidates:
            end = i + 1 + len(name)
            if best is not None and len(name) < best:
                break                        # every identity sharing the longest name has been collected
            if low.startswith(name, i + 1) and (end == len(body) or not (body[end].isalnum() or body[end] in "_")):
                best = len(name)
                found.setdefault(row["id"], (row, body[i + 1:end]))
        if len(found) >= 50:
            break
    return list(found.values())


def find_mentions(conn, body: str) -> list:
    return [row for row, _ in find_mention_spans(conn, body)]


GROUP_MENTIONS = {"all": ("user", "agent", "system"), "agents": ("agent", "system"), "humans": ("user",),
                  "people": ("user",), "everyone": ("user", "agent", "system")}
GROUP_RE = re.compile(r"(?<![\w@/])@(" + "|".join(GROUP_MENTIONS) + r")(?![\w-])", re.I)


def group_mentions(body: str) -> set[str]:
    """Which of @all, @agents, @humans (and @everyone, @people) a message uses."""
    return {m.group(1).lower() for m in GROUP_RE.finditer(body)} if "@" in body else set()


def _record_mentions(conn, channel, message_id: int, body: str, sender_id: int) -> None:
    kinds = {k for g in group_mentions(body) for k in GROUP_MENTIONS[g]}
    # a personal mention first (it keeps the exact text), then the groups fill in everyone else
    for row, text in find_mention_spans(conn, body):
        # only people who can read the channel get the mention: it must not reveal the channel to anyone else
        if row["id"] == sender_id or not can_see(conn, _principal_obj(row), channel):
            continue
        conn.execute("INSERT OR IGNORE INTO message_mentions (message_id, principal_id, mentioned_as) VALUES (?, ?, ?)",
                     (message_id, row["id"], text))
    if kinds:   # @all / @agents / @humans: everyone of that kind who can read the channel
        marks = ",".join("?" * len(kinds))
        group_of = {k: g for g in sorted(group_mentions(body), key=lambda g: len(GROUP_MENTIONS[g])) for k in GROUP_MENTIONS[g]}
        for row in conn.execute(f"SELECT * FROM principals WHERE disabled = 0 AND identity_type IN ({marks})", sorted(kinds)):
            if row["id"] != sender_id and can_see(conn, _principal_obj(row), channel):
                conn.execute("INSERT OR IGNORE INTO message_mentions (message_id, principal_id, mentioned_as) VALUES (?, ?, ?)",
                             (message_id, row["id"], group_of[row["identity_type"]]))


def backfill_mentions(conn) -> int:
    """One-time pass after the matcher learned display names: record mentions in older messages."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'mentions_v2'").fetchone():
        return 0
    n = 0
    for m in conn.execute("SELECT id, channel_id, sender_id, body FROM messages WHERE body LIKE '%@%'").fetchall():
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
        before = conn.execute("SELECT COUNT(*) FROM message_mentions WHERE message_id = ?", (m["id"],)).fetchone()[0]
        _record_mentions(conn, ch, m["id"], m["body"], m["sender_id"])
        n += conn.execute("SELECT COUNT(*) FROM message_mentions WHERE message_id = ?", (m["id"],)).fetchone()[0] - before
    conn.execute("INSERT INTO settings (key, value) VALUES ('mentions_v2', 'done')")
    return n


MAX_REACTIONS_PER_MESSAGE = 20   # distinct emoji


def _attach_reactions(conn, messages: list[dict], viewer_id: int) -> None:
    """`reactions`: one entry per emoji, in the order first used, with count, who, and whether the viewer is one."""
    if not messages:
        return
    ids = [m["id"] for m in messages]
    found: dict[int, dict] = {}
    for mid, emoji, pid, name, ext, kind in conn.execute(f"""
            SELECT r.message_id, r.emoji, p.id, p.display_name, p.external_id, p.identity_type
            FROM message_reactions r JOIN principals p ON p.id = r.principal_id
            WHERE r.message_id IN ({",".join("?" * len(ids))}) ORDER BY r.created_at, r.rowid""", ids):
        e = found.setdefault(mid, {}).setdefault(emoji, {"emoji": emoji, "count": 0, "me": False, "by": []})
        e["count"] += 1
        e["me"] = e["me"] or pid == viewer_id
        e["by"].append({"id": pid, "display_name": name, "external_id": ext, "identity_type": kind})
    for m in messages:
        m["reactions"] = list(found.get(m["id"], {}).values())


def _valid_emoji(emoji) -> str:
    """One emoji (or a short emoji sequence): no spaces, no letters or digits, at most 16 characters."""
    if not isinstance(emoji, str):
        raise bad_request("emoji must be a string")
    emoji = emoji.strip()
    if not 1 <= len(emoji) <= 16 or any(c.isspace() or (c.isascii() and c.isalnum()) for c in emoji):
        raise bad_request("react with an emoji character, like 👍 or ✅ (not a name like :thumbsup:)")
    return emoji


def react(conn, p: Principal, channel_id: int, message_id: int, emoji, on: bool, now: float) -> tuple[dict, bool]:
    """Add (on=True) or remove a reaction of yours. Idempotent; returns the message and whether anything changed."""
    emoji = _valid_emoji(emoji)
    channel = channel_for(conn, p, channel_id, "messages:write")
    row = conn.execute("SELECT id FROM messages WHERE id = ? AND channel_id = ?", (message_id, channel_id)).fetchone()
    if row is None:
        raise not_found("no such message in this channel")
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    if on:
        exists = conn.execute("SELECT 1 FROM message_reactions WHERE message_id = ? AND principal_id = ? AND emoji = ?",
                              (message_id, p.id, emoji)).fetchone()
        if not exists:
            kinds = {r[0] for r in conn.execute("SELECT DISTINCT emoji FROM message_reactions WHERE message_id = ?", (message_id,))}
            if emoji not in kinds and len(kinds) >= MAX_REACTIONS_PER_MESSAGE:
                raise conflict("too_many_reactions", f"a message can have at most {MAX_REACTIONS_PER_MESSAGE} different emoji")
            conn.execute("INSERT INTO message_reactions (message_id, principal_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                         (message_id, p.id, emoji, now))
        changed = not exists
    else:
        changed = conn.execute("DELETE FROM message_reactions WHERE message_id = ? AND principal_id = ? AND emoji = ?",
                               (message_id, p.id, emoji)).rowcount > 0
    if changed:   # followers see it through the edit cursor, like an edit (the body and edited_at stay as they are)
        rev = channel["edit_rev"] + 1
        conn.execute("UPDATE channels SET edit_rev = ? WHERE id = ?", (rev, channel_id))
        conn.execute("UPDATE messages SET edit_rev = ? WHERE id = ?", (rev, message_id))
    out = _message_json(conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone())
    _attach_mentions(conn, [out])
    _attach_reactions(conn, [out], p.id)
    return out, changed


def backfill_mentioned_as(conn) -> int:
    """One-time: work out the text older mentions were written with (current names, former names, handle, groups)."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'mentions_v3'").fetchone():
        return 0
    n = 0
    rows = conn.execute("""SELECT mm.message_id, mm.principal_id, m.body, p.identity_type FROM message_mentions mm
                           JOIN messages m ON m.id = mm.message_id JOIN principals p ON p.id = mm.principal_id
                           WHERE mm.mentioned_as IS NULL""").fetchall()
    spans: dict[int, dict] = {}
    for mid, pid, body, kind in rows:
        if mid not in spans:
            spans[mid] = {r["id"]: text for r, text in find_mention_spans(conn, body)}
        text = spans[mid].get(pid) or next((g for g in sorted(group_mentions(body)) if kind in GROUP_MENTIONS[g]), None)
        if text:
            conn.execute("UPDATE message_mentions SET mentioned_as = ? WHERE message_id = ? AND principal_id = ?", (text, mid, pid))
            n += 1
    conn.execute("INSERT INTO settings (key, value) VALUES ('mentions_v3', 'done')")
    return n


def _attach_mentions(conn, messages: list[dict]) -> None:
    if not messages:
        return
    ids = [m["id"] for m in messages]
    found: dict[int, list] = {}
    for r in conn.execute(f"""SELECT mm.message_id, p.id, p.external_id, p.display_name, p.identity_type, p.hf_username,
                                     mm.mentioned_as
                              FROM message_mentions mm JOIN principals p ON p.id = mm.principal_id
                              WHERE mm.message_id IN ({",".join("?" * len(ids))})""", ids):
        found.setdefault(r[0], []).append({"id": r[1], "external_id": r[2], "display_name": r[3], "identity_type": r[4],
                                           "hf_username": r[5], "as": r[6]})
    for m in messages:
        m["mentions"] = found.get(m["id"], [])


# --------------------------------------------------------- read state and activity


def _reads_baseline(conn, p: Principal) -> float:
    """Messages older than this never count as unread for p (read tracking start, or when p joined)."""
    since = conn.execute("SELECT value FROM settings WHERE key = 'reads_since'").fetchone()
    joined = conn.execute("SELECT created_at FROM principals WHERE id = ?", (p.id,)).fetchone()[0]
    return max(float(since[0]) if since else 0.0, joined)


MY_THREADS = """(SELECT id FROM messages WHERE sender_id = :me AND thread_id IS NULL
                 UNION SELECT thread_id FROM messages WHERE sender_id = :me AND thread_id IS NOT NULL)"""


FOR_ME = f"""(:dm = 1 OR mm.principal_id IS NOT NULL OR m.thread_id IN {MY_THREADS})"""
# thread replies are read when their thread is opened; top-level messages when the channel is
THREAD_UNREAD = "m.seq > max(COALESCE(tr.last_read_seq, 0), :floor)"


def unread_counts(conn, p: Principal, channel) -> dict:
    """unread: new top-level messages; for_me: mentions, replies in my threads, and DMs to me. A thread
    reply stays "for me" until I open its thread, even after I have looked at the channel."""
    row = conn.execute("SELECT last_read_seq, thread_floor FROM channel_reads WHERE principal_id = ? AND channel_id = ?",
                       (p.id, channel["id"])).fetchone()
    params = {"me": p.id, "c": channel["id"], "lr": row[0] if row else 0, "floor": row[1] if row else 0,
              "has_row": 1 if row else 0, "baseline": _reads_baseline(conn, p),
              "dm": 1 if channel["kind"] == "dm" and _is_channel_member(conn, channel["id"], p.id) else 0}
    r = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN m.thread_id IS NULL AND m.seq > :lr THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN {FOR_ME} AND ((m.thread_id IS NULL AND m.seq > :lr)
                                                OR (m.thread_id IS NOT NULL AND {THREAD_UNREAD})) THEN 1 ELSE 0 END), 0),
               COUNT(DISTINCT CASE WHEN m.thread_id IS NOT NULL AND {FOR_ME} AND {THREAD_UNREAD} THEN m.thread_id END)
        FROM messages m LEFT JOIN message_mentions mm ON mm.message_id = m.id AND mm.principal_id = :me
        LEFT JOIN thread_reads tr ON tr.principal_id = :me AND tr.thread_id = m.thread_id
        WHERE m.channel_id = :c AND m.sender_id != :me AND (m.seq > :lr OR (m.thread_id IS NOT NULL AND m.seq > :floor))
          AND (:has_row = 1 OR m.created_at > :baseline)""", params).fetchone()
    return {"last_read": params["lr"], "unread": r[0], "for_me": r[1], "threads_for_me": r[2]}


def mark_thread_read(conn, p: Principal, channel_id: int, thread_id: int, seq, now: float) -> dict:
    """Mark a thread read up to `seq` (its newest reply you have seen). Returns the channel's counts."""
    ch = channel_for(conn, p, channel_id)
    root = conn.execute("SELECT id FROM messages WHERE id = ? AND channel_id = ? AND thread_id IS NULL",
                        (thread_id, channel_id)).fetchone()
    if root is None:
        raise not_found("no such thread in this channel")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise bad_request("seq must be a message sequence number")
    seq = min(seq, ch["next_seq"] - 1)
    conn.execute("""INSERT INTO thread_reads (principal_id, thread_id, last_read_seq, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT (principal_id, thread_id) DO UPDATE
                    SET last_read_seq = max(last_read_seq, excluded.last_read_seq), updated_at = excluded.updated_at""",
                 (p.id, thread_id, seq, now))
    return unread_counts(conn, p, ch)


def mark_read(conn, p: Principal, channel_id: int, seq, now: float) -> dict:
    ch = channel_for(conn, p, channel_id)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise bad_request("seq must be a message sequence number")
    seq = min(seq, ch["next_seq"] - 1)
    conn.execute("""INSERT INTO channel_reads (principal_id, channel_id, last_read_seq, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT (principal_id, channel_id) DO UPDATE
                    SET last_read_seq = max(last_read_seq, excluded.last_read_seq), updated_at = excluded.updated_at""",
                 (p.id, channel_id, seq, now))
    return unread_counts(conn, p, ch)


def activity(conn, p: Principal, limit: int = 50, after: int | None = None, follow: list[int] | None = None,
             reactions: bool = False) -> dict:
    """Messages for me: mentions, replies in threads I am in, DMs to me, and (with `follow`) every
    message in those channels. My own messages never count.

    Without `after`: the newest `limit`, newest first (what the Activity view shows).
    With `after`: messages with id > after, oldest first, for an agent that resumes from a saved
    cursor. Message ids grow with every post across the whole board, so `next_cursor` is one number.
    """
    limit = max(1, min(int(limit), 200))
    follow = list(dict.fromkeys(follow or []))
    for cid in follow:
        channel_for(conn, p, cid)                    # 404 for channels the caller cannot read
    ids = visible_channel_ids(conn, p)
    scope = "" if ids is None else (f" AND m.channel_id IN ({','.join(str(int(i)) for i in ids)})" if ids else " AND 0")
    followed = f" OR m.channel_id IN ({','.join(str(int(i)) for i in follow)})" if follow else ""
    order = "DESC" if after is None else "ASC"
    rows = conn.execute(MESSAGE_SELECT + f"""
        WHERE m.sender_id != :me AND m.id > :after{scope} AND (
            m.id IN (SELECT message_id FROM message_mentions WHERE principal_id = :me)
            OR m.thread_id IN {MY_THREADS}
            OR m.channel_id IN (SELECT c.id FROM channels c JOIN channel_members cm ON cm.channel_id = c.id
                                WHERE c.kind = 'dm' AND cm.principal_id = :me){followed})
        ORDER BY m.id {order} LIMIT :limit""", {"me": p.id, "limit": limit + 1, "after": after or 0}).fetchall()
    has_more = len(rows) > limit
    messages = [_message_json(r) for r in rows[:limit]]
    _attach_mentions(conn, messages)
    _attach_reactions(conn, messages, p.id)
    my_threads = {r[0] for r in conn.execute(MY_THREADS.strip()[1:-1], {"me": p.id})} if messages else set()
    reads = dict(conn.execute("SELECT channel_id, last_read_seq FROM channel_reads WHERE principal_id = ?", (p.id,)).fetchall())
    floors = dict(conn.execute("SELECT channel_id, thread_floor FROM channel_reads WHERE principal_id = ?", (p.id,)).fetchall())
    thread_reads = dict(conn.execute("SELECT thread_id, last_read_seq FROM thread_reads WHERE principal_id = ?", (p.id,)).fetchall())
    baseline = _reads_baseline(conn, p)
    channels = {}
    for m in messages:
        cid = m["channel_id"]
        if cid not in channels:
            channels[cid] = _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone())
        m["channel"] = {k: channels[cid][k] for k in ("id", "name", "kind")} | (
            {"members": channels[cid]["members"]} if channels[cid]["kind"] == "dm" else {})
        m["reason"] = ("dm" if channels[cid]["kind"] == "dm" and any(x and x["id"] == p.id for x in channels[cid]["members"])
                       else "mention" if any(x["id"] == p.id for x in m["mentions"])
                       else "reply" if m["thread_id"] in my_threads else "channel")
        if m["thread_id"]:
            tr = thread_reads.get(m["thread_id"])
            m["unread"] = m["seq"] > max(tr or 0, floors.get(cid, 0)) and (cid in reads or m["created_at"] > baseline)
        else:
            m["unread"] = m["seq"] > reads.get(cid, 0) and (cid in reads or m["created_at"] > baseline)
    if after is None:
        cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]   # "everything up to now"
    else:
        cursor = messages[-1]["id"] if messages else after
    out = {"messages": messages, "next_cursor": cursor, "has_more": has_more}
    if reactions and after is None:
        out["reactions"] = _reactions_to_me(conn, p, scope, limit)
    return out


def joined_channel_ids(conn, p: Principal) -> list[int]:
    """Channels (not DMs, not archived) the caller is a member of and can read."""
    return [r["id"] for r in conn.execute("""SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id = c.id
                                             WHERE cm.principal_id = ? AND c.kind != 'dm' AND c.archived = 0""", (p.id,))
            if can_see(conn, p, r)]


def _reactions_to_me(conn, p: Principal, scope: str, limit: int) -> list[dict]:
    """Reactions others left on my messages, one entry per message (newest reaction first)."""
    rows = conn.execute(f"""SELECT r.message_id, r.emoji, r.created_at, r.principal_id FROM message_reactions r
                            JOIN messages m ON m.id = r.message_id
                            WHERE m.sender_id = :me AND r.principal_id != :me{scope}
                            ORDER BY r.created_at DESC LIMIT 500""", {"me": p.id}).fetchall()
    groups: dict[int, dict] = {}
    for mid, emoji, at, pid in rows:
        g = groups.setdefault(mid, {"message_id": mid, "last_at": at, "reactions": []})
        g["reactions"].append({"emoji": emoji, "by": _brief(conn, pid), "at": at})
    items = sorted(groups.values(), key=lambda g: -g["last_at"])[:limit]
    if items:
        full = {r["id"]: r for r in conn.execute(MESSAGE_SELECT + f" WHERE m.id IN ({','.join(str(g['message_id']) for g in items)})")}
        msgs = {i: _message_json(r) for i, r in full.items()}
        _attach_mentions(conn, list(msgs.values()))
        for g in items:
            m = msgs[g["message_id"]]
            c = _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone())
            m["channel"] = {k: c[k] for k in ("id", "name", "kind")} | ({"members": c["members"]} if c["kind"] == "dm" else {})
            g["message"] = m
    return items


# ------------------------------------------------------------------ analytics


def analytics(conn, p: Principal, now: float, days: int = 30) -> dict:
    """Board activity for admins (and readers): totals, a daily series, channels and who posts."""
    if not p.sees_everything:
        raise forbidden("only admins can see analytics")
    days = max(1, min(int(days), 365))
    day0 = (int(now // 86400) - days + 1) * 86400            # start of the first UTC day in the window
    kind = "CASE WHEN p.identity_type = 'user' THEN 'people' ELSE 'agents' END"
    daily = {(day0 + i * 86400): {"people": 0, "agents": 0} for i in range(days)}
    for d, k, n in conn.execute(f"""SELECT CAST(m.created_at / 86400 AS INTEGER) * 86400, {kind}, COUNT(*)
                                    FROM messages m JOIN principals p ON p.id = m.sender_id
                                    WHERE m.created_at >= ? GROUP BY 1, 2""", (day0,)):
        if d in daily:
            daily[d][k] = n
    series = [{"day": d, **v} for d, v in sorted(daily.items())]

    chans: dict = {}
    for r in conn.execute("""SELECT c.id, c.name, c.kind, c.archived,
                                    (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS total,
                                    (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id AND m.created_at >= :d0) AS recent,
                                    (SELECT COUNT(DISTINCT m.sender_id) FROM messages m WHERE m.channel_id = c.id AND m.created_at >= :d0) AS posters,
                                    (SELECT MAX(m.created_at) FROM messages m WHERE m.channel_id = c.id) AS last_at
                             FROM channels c""", {"d0": day0}):
        key = "dm" if r["kind"] == "dm" else r["id"]          # DMs are counted together, never by pair
        c = chans.setdefault(key, {"id": None if key == "dm" else r["id"], "name": "direct messages" if key == "dm" else r["name"],
                                   "kind": r["kind"], "archived": bool(r["archived"]) if key != "dm" else False,
                                   "total": 0, "messages": 0, "posters": 0, "last_at": None})
        c["total"] += r["total"]; c["messages"] += r["recent"]; c["posters"] += r["posters"]
        c["last_at"] = max(filter(None, [c["last_at"], r["last_at"]]), default=None)
    channels = sorted(chans.values(), key=lambda c: (-c["messages"], -c["total"], c["name"]))

    per_day = {}
    for pid, d, n in conn.execute("""SELECT sender_id, CAST(created_at / 86400 AS INTEGER) * 86400, COUNT(*) FROM messages
                                     WHERE created_at >= ? GROUP BY 1, 2""", (day0,)):
        per_day.setdefault(pid, {})[d] = n
    posters = []
    for r in conn.execute("""SELECT p.id, p.display_name, p.external_id, p.identity_type, p.role, p.owner_id, p.pattern_seed,
                                    substr(av.sha256, 1, 12) AS avatar, COUNT(m.id) AS n, MAX(m.created_at) AS last_at,
                                    COUNT(DISTINCT m.channel_id) AS nchannels
                             FROM principals p JOIN messages m ON m.sender_id = p.id AND m.created_at >= ?
                             LEFT JOIN avatars av ON av.principal_id = p.id
                             GROUP BY p.id ORDER BY n DESC""", (day0,)):
        posters.append({"id": r["id"], "display_name": r["display_name"], "external_id": r["external_id"],
                        "identity_type": r["identity_type"], "role": r["role"], "avatar": r["avatar"], "pattern": r["pattern_seed"],
                        "owner": _brief(conn, r["owner_id"]), "messages": r["n"], "last_at": r["last_at"], "channels": r["nchannels"],
                        "daily": [per_day.get(r["id"], {}).get(d, 0) for d in sorted(daily)]})

    count = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    week = now - 7 * 86400
    totals = {
        "messages": count("SELECT COUNT(*) FROM messages"),
        "messages_window": sum(d["people"] + d["agents"] for d in series),
        "people": count("SELECT COUNT(*) FROM principals WHERE identity_type = 'user' AND disabled = 0"),
        "agents": count("SELECT COUNT(*) FROM principals WHERE identity_type != 'user' AND disabled = 0"),
        "channels": count("SELECT COUNT(*) FROM channels WHERE kind != 'dm' AND archived = 0"),
        "active_people_7d": count("""SELECT COUNT(DISTINCT m.sender_id) FROM messages m JOIN principals p ON p.id = m.sender_id
                                     WHERE m.created_at >= ? AND p.identity_type = 'user'""", week),
        "active_agents_7d": count("""SELECT COUNT(DISTINCT m.sender_id) FROM messages m JOIN principals p ON p.id = m.sender_id
                                     WHERE m.created_at >= ? AND p.identity_type != 'user'""", week),
        "threads_window": count("SELECT COUNT(DISTINCT thread_id) FROM messages WHERE thread_id IS NOT NULL AND created_at >= ?", day0),
        "attachments_window": count("SELECT COUNT(*) FROM attachments WHERE published = 1 AND created_at >= ?", day0),
    }
    return {"days": days, "since": day0, "generated_at": now, "totals": totals, "daily": series,
            "channels": channels, "posters": posters}


# ------------------------------------------------------------------ search

SEARCH_OPS = {"from", "in", "before", "after", "on", "has", "is", "by", "to", "mentions"}
SEARCH_TOKEN = re.compile(r'(?P<op>[A-Za-z]+):(?P<val>"[^"]*"|\S+)|"(?P<phrase>[^"]*)"|(?P<word>\S+)')


def parse_search(q: str) -> dict:
    """Split a query into free text and filters. Filters: from:@name in:#channel before:/after:/on:DATE
    has:file|replies|mention is:reply|root by:agent|person to:@name ("mentions:" works too).
    Values with spaces go in quotes: from:"Ana Lopez". Unknown `word:` stays text (URLs keep working)."""
    out: dict = {"words": [], "phrases": [], "filters": []}
    for m in SEARCH_TOKEN.finditer(q or ""):
        op = (m.group("op") or "").lower()
        if op in SEARCH_OPS:
            val = m.group("val")
            out["filters"].append((("to" if op == "mentions" else op), val[1:-1] if val.startswith('"') else val))
        elif m.group("phrase") is not None:
            if m.group("phrase").strip():
                out["phrases"].append(m.group("phrase").strip())
        else:
            out["words"].append(m.group(0))
    return out


def _fts_query(words: list[str], phrases: list[str]) -> str | None:
    """Every word must appear (as a prefix, so `deplo` finds `deployed`); phrases must appear as written."""
    parts = []
    for w in words:
        tokens = re.findall(r"\w+", w)
        if tokens:
            parts.append('"' + " ".join(tokens) + '"*')
    for ph in phrases:
        tokens = re.findall(r"\w+", ph)
        if tokens:
            parts.append('"' + " ".join(tokens) + '"')
    return " AND ".join(parts) or None


def _search_date(value: str, now: float, end: bool = False) -> float:
    """YYYY-MM-DD (UTC), `today`, `yesterday`, or a relative age like 7d, 12h, 2w."""
    v = value.lower()
    day = int(now // 86400) * 86400
    rel = re.fullmatch(r"(\d+)([hdw])", v)
    if rel:
        return now - int(rel.group(1)) * {"h": 3600, "d": 86400, "w": 7 * 86400}[rel.group(2)]
    if v in ("today", "yesterday"):
        start = day - (86400 if v == "yesterday" else 0)
    else:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
        if not m:
            raise bad_request(f"dates look like 2026-10-13, today, yesterday or 7d (got {value!r})")
        import calendar
        try:
            start = calendar.timegm((int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0))
        except (ValueError, OverflowError):
            raise bad_request(f"not a date: {value!r}")
    return start + 86400 if end else start


def _resolve_people(conn, value: str) -> list[int]:
    """Who `from:` / `to:` means: a display name, handle or HF name (with or without @)."""
    name = value.lstrip("@").strip().lower()
    if not name:
        return []
    return [r["id"] for r in conn.execute("SELECT * FROM principals") if name in _mention_names(r)]


def search(conn, p: Principal, q: str = "", *, channel_id: int | None = None, sender_id: int | None = None,
           after: str | None = None, before: str | None = None, sort: str = "newest", limit: int = 20,
           offset: int = 0, now: float = 0.0) -> dict:
    """Messages matching `q` in channels the caller can read, plus channels and people whose names
    match its plain words. Plain parameters do the same as the in-query filters."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    if sort not in ("newest", "oldest", "relevance"):
        raise bad_request("sort must be newest, oldest or relevance")
    parsed = parse_search(q)
    where, args, applied = [], [], []
    visible = visible_channel_ids(conn, p)
    if visible is not None:
        if not visible:
            return {"messages": [], "channels": [], "people": [], "total": 0, "next_offset": None, "filters": []}
        where.append(f"m.channel_id IN ({','.join(str(int(i)) for i in visible)})")
    filters = list(parsed["filters"])
    if channel_id is not None:
        channel_for(conn, p, channel_id)
        where.append("m.channel_id = ?"); args.append(channel_id)
    if sender_id is not None:
        where.append("m.sender_id = ?"); args.append(sender_id)
    if after:
        filters.append(("after", after))
    if before:
        filters.append(("before", before))
    for op, val in filters:
        if op in ("from", "to"):
            ids = [p.id] if val.lower() in ("me", "@me") else _resolve_people(conn, val)
            if not ids:
                raise bad_request(f"nobody called {val!r}")
            marks = ",".join("?" * len(ids))
            if op == "from":
                where.append(f"m.sender_id IN ({marks})")
            else:
                where.append(f"m.id IN (SELECT message_id FROM message_mentions WHERE principal_id IN ({marks}))")
            args += ids
        elif op == "in":
            name = val.lstrip("#").lower()
            row = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
            if row is None or not can_see(conn, p, row):
                raise bad_request(f"no channel #{name} that you can read")
            where.append("m.channel_id = ?"); args.append(row["id"])
        elif op in ("after", "before", "on"):
            if op != "before":
                where.append("m.created_at >= ?"); args.append(_search_date(val, now))
            if op != "after":
                where.append("m.created_at < ?"); args.append(_search_date(val, now, end=(op == "on")))
        elif op == "has":
            v = val.lower()
            if v in ("file", "files", "attachment"):
                where.append("m.attachments IS NOT NULL")
            elif v in ("replies", "thread"):
                where.append("EXISTS (SELECT 1 FROM messages r WHERE r.thread_id = m.id)")
            elif v in ("mention", "mentions"):
                where.append("EXISTS (SELECT 1 FROM message_mentions mm WHERE mm.message_id = m.id)")
            elif v in ("link", "links"):
                where.append("(m.body LIKE '%http://%' OR m.body LIKE '%https://%')")
            else:
                raise bad_request("has: takes file, replies, mention or link")
        elif op == "is":
            v = val.lower()
            if v in ("reply", "replies"):
                where.append("m.thread_id IS NOT NULL")
            elif v in ("root", "top", "toplevel"):
                where.append("m.thread_id IS NULL")
            elif v in ("edited",):
                where.append("m.edited_at IS NOT NULL")
            else:
                raise bad_request("is: takes reply, root or edited")
        elif op == "by":
            v = val.lower().rstrip("s")
            kinds = {"agent": "('agent', 'system')", "bot": "('agent', 'system')", "person": "('user')",
                     "people": "('user')", "human": "('user')", "user": "('user')", "service": "('system')"}
            if v not in kinds and val.lower() not in kinds:
                raise bad_request("by: takes agent, person or service")
            where.append(f"m.sender_id IN (SELECT id FROM principals WHERE identity_type IN {kinds.get(v) or kinds[val.lower()]})")
        applied.append({"op": op, "value": val})
    fts = _fts_query(parsed["words"], parsed["phrases"])
    if not fts and not where[1 if visible is not None else 0:] and not applied:
        return {"messages": [], "channels": [], "people": [], "total": 0, "next_offset": None, "filters": []}
    base = "FROM messages m"
    if fts:
        base += " JOIN messages_fts ON messages_fts.rowid = m.id"
        where.insert(0, "messages_fts MATCH ?"); args.insert(0, fts)
    cond = " WHERE " + " AND ".join(where) if where else ""
    order = {"newest": "m.id DESC", "oldest": "m.id ASC", "relevance": "bm25(messages_fts), m.id DESC" if fts else "m.id DESC"}[sort]
    snip = "snippet(messages_fts, 0, char(2), char(3), '…', 24)" if fts else "NULL"
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 {base}{cond} LIMIT 1000)", args).fetchone()[0]
        rows = conn.execute(f"SELECT m.id, {snip} {base}{cond} ORDER BY {order} LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
    except sqlite3.OperationalError as e:
        raise bad_request(f"could not run this search: {e}")
    snippets = {r[0]: r[1] for r in rows}
    messages = []
    if rows:
        full = {r["id"]: r for r in conn.execute(
            MESSAGE_SELECT + f" WHERE m.id IN ({','.join(str(r[0]) for r in rows)})")}
        messages = [_message_json(full[r[0]]) for r in rows]
        _attach_mentions(conn, messages)
        _attach_reactions(conn, messages, p.id)
        _attach_thread_summaries(conn, messages)
        channels = {}
        for m in messages:
            cid = m["channel_id"]
            if cid not in channels:
                c = _channel_json(conn, conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone())
                channels[cid] = {k: c[k] for k in ("id", "name", "kind")} | ({"members": c["members"]} if c["kind"] == "dm" else {})
            m["channel"] = channels[cid]
            m["snippet"] = snippets[m["id"]]
    # names: only for plain text, so "deploy" also finds #deploys and an agent called Deployer
    text = " ".join(parsed["words"] + parsed["phrases"]).strip().lower()
    found_channels, found_people = [], []
    if text and offset == 0:
        for row in conn.execute("SELECT * FROM channels WHERE kind != 'dm' ORDER BY archived, name"):
            if (text in row["name"] or text in (row["description"] or "").lower()) and can_see(conn, p, row):
                found_channels.append(_channel_json(conn, row))
            if len(found_channels) >= 8:
                break
        allowed = _directory_ids(conn, p)
        for row in conn.execute("SELECT * FROM principals WHERE disabled = 0 ORDER BY identity_type = 'user' DESC, display_name"):
            if (allowed is None or row["id"] in allowed) and any(text in n for n in _mention_names(row)):
                found_people.append(_brief(conn, row["id"]))
            if len(found_people) >= 8:
                break
    more = offset + len(messages) < total
    return {"messages": messages, "channels": found_channels, "people": found_people, "total": total,
            "total_capped": total >= 1000, "next_offset": offset + len(messages) if more else None, "filters": applied}


# ------------------------------------------------------------------ channel docs

MAX_DOC_BYTES = 200_000


def _doc_json(conn, p: Principal, channel) -> dict:
    row = conn.execute("SELECT * FROM channel_doc_versions WHERE channel_id = ? AND version = ?",
                       (channel["id"], channel["doc_version"])).fetchone()
    return {"channel_id": channel["id"], "version": channel["doc_version"], "body": row["body"] if row else "",
            "updated_by": _brief(conn, row["author_id"]) if row else None, "updated_at": row["created_at"] if row else None,
            "can_edit": can_post(conn, p, channel) and p.may_post and not channel["archived"]}


def _docs_enabled(conn) -> None:
    if not get_settings(conn)["channel_docs"]:
        raise forbidden("channel docs are switched off on this board (an admin can turn them on in Settings > Board)")


def get_doc(conn, p: Principal, channel_id: int) -> dict:
    """The channel's shared Markdown doc (version 0 and an empty body until someone writes it)."""
    _docs_enabled(conn)
    return _doc_json(conn, p, channel_for(conn, p, channel_id))


def save_doc(conn, p: Principal, channel_id: int, payload: Any, now: float) -> tuple[dict, bool]:
    """Replace the doc. `base_version` is the version the edit started from: if someone saved since,
    nothing is written and the 409 carries their version, so no change is ever lost silently."""
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str) or set(payload) - {"body", "base_version"}:
        raise bad_request('request body must be {"body": "...", "base_version": <version you edited>}')
    base = payload.get("base_version")
    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise bad_request("base_version is required: the version you started from (0 for a new doc)")
    _docs_enabled(conn)
    body = payload["body"]
    if len(body.encode()) > MAX_DOC_BYTES:
        raise ApiError(413, "too_large", f"the doc is limited to {MAX_DOC_BYTES // 1000} KB")
    channel = channel_for(conn, p, channel_id, "messages:write")
    if channel["archived"]:
        raise conflict("channel_archived", "channel is archived")
    current = _doc_json(conn, p, channel)
    if base != current["version"]:
        who = nameOf(current["updated_by"]) if current["updated_by"] else "someone"
        raise ApiError(409, "doc_conflict", f"{who} saved version {current['version']} after the version you edited "
                       f"({base}). Merge your change into theirs and save again with base_version={current['version']}.",
                       details={"current": current})
    if body == current["body"]:
        return current, False
    version = current["version"] + 1
    conn.execute("INSERT INTO channel_doc_versions (channel_id, version, body, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
                 (channel_id, version, body, p.id, now))
    conn.execute("UPDATE channels SET doc_version = ? WHERE id = ?", (version, channel_id))
    audit(conn, p.id, "doc.save", f"channel:{channel_id}", {"version": version}, now)
    return _doc_json(conn, p, conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()), True


def doc_versions(conn, p: Principal, channel_id: int) -> dict:
    """Every saved version, newest first (without bodies)."""
    _docs_enabled(conn)
    channel_for(conn, p, channel_id)
    return {"versions": [{"version": r["version"], "author": _brief(conn, r["author_id"]), "created_at": r["created_at"],
                          "size": len(r["body"].encode())}
                         for r in conn.execute("SELECT * FROM channel_doc_versions WHERE channel_id = ? ORDER BY version DESC",
                                               (channel_id,))]}


def doc_version(conn, p: Principal, channel_id: int, version: int) -> dict:
    _docs_enabled(conn)
    channel_for(conn, p, channel_id)
    r = conn.execute("SELECT * FROM channel_doc_versions WHERE channel_id = ? AND version = ?", (channel_id, version)).fetchone()
    if r is None:
        raise not_found("no such version")
    return {"version": r["version"], "body": r["body"], "author": _brief(conn, r["author_id"]), "created_at": r["created_at"]}


def nameOf(brief: dict | None) -> str:
    return (brief or {}).get("display_name") or (brief or {}).get("external_id") or "someone"


def get_message(conn, p: Principal, message_id: int) -> dict:
    """One message by id, with its channel (for links). 404 if it does not exist or you cannot read its channel."""
    row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()
    if row is None:
        raise not_found("no such message")
    channel = channel_for(conn, p, row["channel_id"])
    out = _message_json(row)
    _attach_mentions(conn, [out])
    _attach_reactions(conn, [out], p.id)
    c = _channel_json(conn, channel)
    out["channel"] = {k: c[k] for k in ("id", "name", "kind")} | ({"members": c["members"]} if c["kind"] == "dm" else {})
    return out


def delete_channel(conn, p: Principal, channel_id: int, confirm, now: float) -> dict:
    """Delete a channel for good (admins): its messages, threads, reactions, files, doc and members.
    `confirm` must repeat the channel's name. Returns what went, plus the file keys to remove from disk."""
    if not p.is_admin:
        raise forbidden("only admins can delete channels")
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise not_found("channel not found")
    if row["kind"] == "dm":
        raise bad_request("direct messages cannot be deleted")
    if confirm != row["name"]:
        raise bad_request(f"to delete it, confirm with the channel's name ({row['name']!r})")
    in_channel = "SELECT id FROM messages WHERE channel_id = ?"
    counts = {"messages": conn.execute("SELECT COUNT(*) FROM messages WHERE channel_id = ?", (channel_id,)).fetchone()[0]}
    keys = [r[0] for r in conn.execute("SELECT storage_key FROM attachments WHERE channel_id = ?", (channel_id,))]
    counts["files"] = len(keys)
    for table in ("message_reactions", "message_mentions", "message_revisions"):
        conn.execute(f"DELETE FROM {table} WHERE message_id IN ({in_channel})", (channel_id,))
    conn.execute("UPDATE messages SET reply_to = NULL, thread_id = NULL WHERE channel_id = ?", (channel_id,))
    conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
    for table in ("attachments", "channel_doc_versions", "channel_members", "channel_reads"):
        conn.execute(f"DELETE FROM {table} WHERE channel_id = ?", (channel_id,))
    for inv in conn.execute("SELECT id, channel_ids FROM invites WHERE channel_ids IS NOT NULL").fetchall():
        ids = [i for i in json.loads(inv["channel_ids"]) if i != channel_id]
        conn.execute("UPDATE invites SET channel_ids = ? WHERE id = ?", (json.dumps(ids) if ids else None, inv["id"]))
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    audit(conn, p.id, "channel.delete", f"channel:{channel_id}", {"name": row["name"], **counts}, now)
    return {"deleted": row["name"], **counts, "storage_keys": keys}


def delete_message(conn, p: Principal, channel_id: int, message_id: int, now: float) -> dict:
    """Delete a message for good (admins). A thread's first message takes its replies with it.
    Returns the ids removed and the file keys to remove from disk."""
    if not p.is_admin:
        raise forbidden("only admins can delete messages")
    channel_for(conn, p, channel_id)
    row = conn.execute("SELECT * FROM messages WHERE id = ? AND channel_id = ?", (message_id, channel_id)).fetchone()
    if row is None:
        raise not_found("no such message in this channel")
    ids = [message_id] + ([r[0] for r in conn.execute("SELECT id FROM messages WHERE thread_id = ?", (message_id,))]
                          if row["thread_id"] is None else [])
    marks = ",".join("?" * len(ids))
    att_ids = [a for r in conn.execute(f"SELECT attachments FROM messages WHERE id IN ({marks}) AND attachments IS NOT NULL", ids)
               for a in json.loads(r[0])]
    keys = []
    if att_ids:
        amarks = ",".join("?" * len(att_ids))
        keys = [r[0] for r in conn.execute(f"SELECT storage_key FROM attachments WHERE id IN ({amarks})", att_ids)]
        conn.execute(f"DELETE FROM attachments WHERE id IN ({amarks})", att_ids)
    for table in ("message_reactions", "message_mentions", "message_revisions"):
        conn.execute(f"DELETE FROM {table} WHERE message_id IN ({marks})", ids)
    conn.execute(f"UPDATE messages SET reply_to = NULL, thread_id = NULL WHERE id IN ({marks})", ids)
    conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
    audit(conn, p.id, "message.delete", f"message:{message_id}",
          {"channel": channel_id, "sender": row["sender_id"], "with_replies": len(ids) - 1}, now)
    return {"deleted": ids, "storage_keys": keys}
