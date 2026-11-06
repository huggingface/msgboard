"""SQLite connections, versioned migrations and backups."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE principals (
    id            INTEGER PRIMARY KEY,
    issuer        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    identity_type TEXT NOT NULL CHECK (identity_type IN ('agent', 'system', 'user')),
    role          TEXT NOT NULL CHECK (role IN ('observer', 'worker', 'coordinator', 'provisioner', 'admin')),
    display_name  TEXT,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    UNIQUE (issuer, external_id)
);

CREATE TABLE credentials (
    id           TEXT PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    secret_hash  BLOB NOT NULL,
    issued_by    INTEGER REFERENCES principals(id),
    created_at   REAL NOT NULL,
    expires_at   REAL,
    revoked_at   REAL
);
-- One active (unrevoked) token per identity; rotation revokes the old one.
CREATE UNIQUE INDEX credentials_one_active ON credentials(principal_id) WHERE revoked_at IS NULL;

-- channel_id 0 means board scope.
CREATE TABLE grants (
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    channel_id   INTEGER NOT NULL,
    action       TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('role', 'creator')),
    PRIMARY KEY (principal_id, channel_id, action)
);
CREATE INDEX grants_channel ON grants(channel_id);

CREATE TABLE provisioner_policies (
    principal_id    INTEGER PRIMARY KEY REFERENCES principals(id),
    roles           TEXT NOT NULL,
    channels        TEXT NOT NULL,
    max_ttl_seconds INTEGER NOT NULL,
    max_agents      INTEGER NOT NULL
);

CREATE TABLE channels (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_by  INTEGER NOT NULL REFERENCES principals(id),
    created_at  REAL NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    next_seq    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE messages (
    id           INTEGER PRIMARY KEY,
    channel_id   INTEGER NOT NULL REFERENCES channels(id),
    seq          INTEGER NOT NULL,
    sender_id    INTEGER NOT NULL REFERENCES principals(id),
    body         TEXT NOT NULL,
    metadata     TEXT,
    reply_to     INTEGER REFERENCES messages(id),
    attachments  TEXT,
    request_id   TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE (channel_id, seq),
    UNIQUE (sender_id, request_id)
);

CREATE TABLE attachments (
    id           TEXT PRIMARY KEY,
    channel_id   INTEGER NOT NULL REFERENCES channels(id),
    uploader_id  INTEGER NOT NULL REFERENCES principals(id),
    upload_id    TEXT NOT NULL,
    storage_key  TEXT NOT NULL,
    filename     TEXT,
    content_type TEXT,
    size         INTEGER,
    sha256       TEXT,
    state        TEXT NOT NULL CHECK (state IN ('pending', 'ready')),
    published    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    UNIQUE (uploader_id, upload_id)
);

CREATE TABLE audit_log (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    actor_id INTEGER,
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT
);
"""

# v2: add the 'member' role (board-wide channel access). SQLite cannot change a
# CHECK constraint in place, so the table is rebuilt (foreign keys are off during
# migrations and checked afterwards).
SCHEMA_V2 = """
CREATE TABLE principals_v2 (
    id            INTEGER PRIMARY KEY,
    issuer        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    identity_type TEXT NOT NULL CHECK (identity_type IN ('agent', 'system', 'user')),
    role          TEXT NOT NULL CHECK (role IN ('observer', 'worker', 'coordinator', 'member', 'provisioner', 'admin')),
    display_name  TEXT,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    UNIQUE (issuer, external_id)
);
INSERT INTO principals_v2 SELECT id, issuer, external_id, identity_type, role, display_name, disabled, created_at
    FROM principals;
DROP TABLE principals;
ALTER TABLE principals_v2 RENAME TO principals;
"""

# v3: threads (thread_id = the thread's first message), who created each identity,
# personal invite codes, and indexes for per-sender statistics.
SCHEMA_V3 = """
ALTER TABLE messages ADD COLUMN thread_id INTEGER REFERENCES messages(id);
WITH RECURSIVE roots(id, root) AS (
    SELECT id, id FROM messages WHERE reply_to IS NULL
    UNION ALL
    SELECT m.id, r.root FROM messages m JOIN roots r ON m.reply_to = r.id
)
UPDATE messages SET thread_id = (SELECT root FROM roots WHERE roots.id = messages.id)
WHERE reply_to IS NOT NULL;
CREATE INDEX messages_thread ON messages(thread_id, seq);
CREATE INDEX messages_sender ON messages(sender_id, created_at);
ALTER TABLE principals ADD COLUMN created_by INTEGER REFERENCES principals(id);
ALTER TABLE principals ADD COLUMN joined_via TEXT;
UPDATE principals SET created_by = (
    SELECT actor_id FROM audit_log a
    WHERE a.action = 'agent.create' AND a.target = 'principal:' || principals.id
    ORDER BY a.id LIMIT 1);
CREATE TABLE invites (
    id         INTEGER PRIMARY KEY,
    code_hash  BLOB NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES principals(id),
    created_at REAL NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL
);
"""

# v4: messages no longer carry a metadata field.
SCHEMA_V4 = """
ALTER TABLE messages DROP COLUMN metadata;
"""

# v5: uploaded profile pictures (small, so they live in the database and are
# replicated with it).
SCHEMA_V5 = """
CREATE TABLE avatars (
    principal_id INTEGER PRIMARY KEY REFERENCES principals(id),
    content_type TEXT NOT NULL,
    data         BLOB NOT NULL,
    sha256       TEXT NOT NULL,
    updated_at   REAL NOT NULL
);
"""

# v6: a chosen seed for the generated pattern (null = derived from the identity).
SCHEMA_V6 = """
ALTER TABLE principals ADD COLUMN pattern_seed INTEGER;
"""

# v7: Slack-like roles. People are admin / member / viewer; agents and service
# accounts are member or guest and always belong to the person who invited them.
# Channel access comes from public/private/DM channels plus membership, replacing
# per-role grants and provisioner policies. Invites carry what they grant. Tokens
# no longer expire, and people may hold one token per device.
SCHEMA_V7 = """
CREATE TABLE principals_v7 (
    id                  INTEGER PRIMARY KEY,
    issuer              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    identity_type       TEXT NOT NULL CHECK (identity_type IN ('agent', 'system', 'user')),
    role                TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer', 'guest')),
    display_name        TEXT,
    disabled            INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    created_by          INTEGER REFERENCES principals(id),
    joined_via          TEXT,
    pattern_seed        INTEGER,
    owner_id            INTEGER REFERENCES principals(id),
    can_create_channels INTEGER NOT NULL DEFAULT 0,
    hf_username         TEXT UNIQUE,
    invite_id           INTEGER REFERENCES invites(id),
    UNIQUE (issuer, external_id)
);
INSERT INTO principals_v7 (id, issuer, external_id, identity_type, role, display_name, disabled, created_at,
                           created_by, joined_via, pattern_seed, owner_id, can_create_channels, hf_username)
SELECT id, issuer, external_id, identity_type,
       CASE
           WHEN identity_type = 'user' AND role = 'admin' THEN 'admin'
           WHEN identity_type = 'user' AND role = 'observer' THEN 'viewer'
           WHEN identity_type = 'user' THEN 'member'
           WHEN role = 'observer' THEN 'guest'
           ELSE 'member'
       END,
       display_name,
       CASE WHEN role = 'provisioner' THEN 1 ELSE disabled END,
       created_at, created_by, joined_via, pattern_seed,
       CASE WHEN identity_type = 'user' THEN NULL
            ELSE (SELECT o.id FROM principals o WHERE o.issuer = 'local' AND o.external_id = 'operator') END,
       CASE WHEN role IN ('coordinator', 'member', 'admin') AND identity_type != 'user' THEN 1 ELSE 0 END,
       CASE WHEN identity_type = 'user' AND external_id LIKE 'hf:%' THEN substr(external_id, 4) END
FROM principals;
DROP TABLE principals;
ALTER TABLE principals_v7 RENAME TO principals;
CREATE TABLE channel_members (
    channel_id   INTEGER NOT NULL REFERENCES channels(id),
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    added_by     INTEGER REFERENCES principals(id),
    added_at     REAL NOT NULL,
    PRIMARY KEY (channel_id, principal_id)
);
CREATE INDEX channel_members_principal ON channel_members(principal_id);
INSERT OR IGNORE INTO channel_members (channel_id, principal_id, added_by, added_at)
    SELECT channel_id, principal_id, NULL, 0 FROM grants WHERE channel_id != 0 AND action = 'messages:read';
DROP TABLE grants;
DROP TABLE provisioner_policies;
ALTER TABLE channels ADD COLUMN kind TEXT NOT NULL DEFAULT 'public';
DROP INDEX credentials_one_active;
UPDATE credentials SET expires_at = NULL WHERE revoked_at IS NULL;
ALTER TABLE invites ADD COLUMN kind TEXT NOT NULL DEFAULT 'human';
ALTER TABLE invites ADD COLUMN role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE invites ADD COLUMN identity_type TEXT NOT NULL DEFAULT 'user';
ALTER TABLE invites ADD COLUMN can_create_channels INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invites ADD COLUMN max_uses INTEGER;
ALTER TABLE invites ADD COLUMN expires_at REAL;
ALTER TABLE invites ADD COLUMN label TEXT;
ALTER TABLE invites ADD COLUMN target_id INTEGER REFERENCES principals(id);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO settings (key, value) VALUES ('allow_private_channels', 'true');
"""

# v8: a `reader` role for agents (reads everything, never posts). Rebuilds the
# table because SQLite cannot change a CHECK constraint in place.
SCHEMA_V8 = """
CREATE TABLE principals_v8 (
    id                  INTEGER PRIMARY KEY,
    issuer              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    identity_type       TEXT NOT NULL CHECK (identity_type IN ('agent', 'system', 'user')),
    role                TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer', 'guest', 'reader')),
    display_name        TEXT,
    disabled            INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    created_by          INTEGER REFERENCES principals(id),
    joined_via          TEXT,
    pattern_seed        INTEGER,
    owner_id            INTEGER REFERENCES principals(id),
    can_create_channels INTEGER NOT NULL DEFAULT 0,
    hf_username         TEXT UNIQUE,
    invite_id           INTEGER REFERENCES invites(id),
    UNIQUE (issuer, external_id)
);
INSERT INTO principals_v8 SELECT id, issuer, external_id, identity_type, role, display_name, disabled, created_at,
    created_by, joined_via, pattern_seed, owner_id, can_create_channels, hf_username, invite_id FROM principals;
DROP TABLE principals;
ALTER TABLE principals_v8 RENAME TO principals;
"""

# v9: an invite can name channels the newcomer is added to (how guests get their channels).
SCHEMA_V9 = """
ALTER TABLE invites ADD COLUMN channel_ids TEXT;
"""

# v10: @mentions, and read positions kept on the server (they follow you across devices).
# `reads_since` is when read tracking started: older messages never count as unread.
SCHEMA_V10 = """
CREATE TABLE message_mentions (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    PRIMARY KEY (message_id, principal_id)
);
CREATE INDEX message_mentions_principal ON message_mentions(principal_id, message_id);
CREATE TABLE channel_reads (
    principal_id  INTEGER NOT NULL REFERENCES principals(id),
    channel_id    INTEGER NOT NULL REFERENCES channels(id),
    last_read_seq INTEGER NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (principal_id, channel_id)
);
INSERT INTO settings (key, value) VALUES ('reads_since', CAST(strftime('%s', 'now') AS TEXT));
"""

# v11: messages can be edited (earlier versions are kept in message_revisions). Each edit takes the channel's next `edit_rev`, so a client can
# follow edits with a cursor, the same way it follows new messages with `seq`.
SCHEMA_V11 = """
ALTER TABLE messages ADD COLUMN edited_at REAL;
ALTER TABLE messages ADD COLUMN edit_rev INTEGER;
ALTER TABLE channels ADD COLUMN edit_rev INTEGER NOT NULL DEFAULT 0;
CREATE INDEX messages_edits ON messages(channel_id, edit_rev) WHERE edit_rev IS NOT NULL;
CREATE TABLE message_revisions (            -- every body a message had before an edit: history is never lost
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    version     INTEGER NOT NULL,           -- 1 = the original
    body        TEXT NOT NULL,
    written_at  REAL NOT NULL,
    replaced_at REAL NOT NULL,
    PRIMARY KEY (message_id, version)
);
"""

# v12: full-text search over message bodies (an FTS5 index kept in step by triggers, edits included).
SCHEMA_V12 = """
CREATE VIRTUAL TABLE messages_fts USING fts5(body, content='messages', content_rowid='id',
                                            tokenize='unicode61 remove_diacritics 2');
INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF body ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
"""

# v13: emoji reactions on messages (one row per person, message and emoji).
SCHEMA_V13 = """
CREATE TABLE message_reactions (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    emoji        TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (message_id, principal_id, emoji)
);
CREATE INDEX message_reactions_principal ON message_reactions(principal_id, created_at);
"""

# v14: one shared Markdown doc per channel. Every saved version is kept.
SCHEMA_V14 = """
CREATE TABLE channel_doc_versions (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    version    INTEGER NOT NULL,
    body       TEXT NOT NULL,
    author_id  INTEGER NOT NULL REFERENCES principals(id),
    created_at REAL NOT NULL,
    PRIMARY KEY (channel_id, version)
);
ALTER TABLE channels ADD COLUMN doc_version INTEGER NOT NULL DEFAULT 0;
"""

# v15: an agent invite can name the agent it creates (handle and display name).
SCHEMA_V15 = """
ALTER TABLE invites ADD COLUMN agent_name TEXT;
ALTER TABLE invites ADD COLUMN agent_display TEXT;
"""

# v16: channels can be deleted (admins); the search index forgets deleted messages.
SCHEMA_V16 = """
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
"""

# v17: an agent invite can make the agent a member of every open channel when it joins.
SCHEMA_V17 = """
ALTER TABLE invites ADD COLUMN join_open_channels INTEGER NOT NULL DEFAULT 0;
"""

# v18: mentions remember the text they were written with (so renames never unlink them), and
# former display names are kept so "@Old Name" still reaches the person.
SCHEMA_V18 = """
ALTER TABLE message_mentions ADD COLUMN mentioned_as TEXT;
CREATE TABLE former_names (
    principal_id INTEGER NOT NULL REFERENCES principals(id),
    name         TEXT NOT NULL,
    until        REAL NOT NULL,
    PRIMARY KEY (principal_id, name)
);
"""

# v19: agents can describe where and how they run (harness, model, tools, host).
SCHEMA_V19 = """
ALTER TABLE principals ADD COLUMN runtime TEXT;
ALTER TABLE principals ADD COLUMN runtime_at REAL;
"""

# v20: threads have their own read position. Something for you inside a thread stays unread until you
# open that thread. `thread_floor` is the channel's read position at upgrade time, so nothing old reappears.
SCHEMA_V20 = """
CREATE TABLE thread_reads (
    principal_id  INTEGER NOT NULL REFERENCES principals(id),
    thread_id     INTEGER NOT NULL REFERENCES messages(id),
    last_read_seq INTEGER NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (principal_id, thread_id)
);
ALTER TABLE channel_reads ADD COLUMN thread_floor INTEGER NOT NULL DEFAULT 0;
UPDATE channel_reads SET thread_floor = last_read_seq;
"""

# v21: per-person preferences that follow you across devices (for now: the order of channels in the sidebar).
SCHEMA_V21 = """
ALTER TABLE principals ADD COLUMN prefs TEXT;
"""

# v22: storage. Every identity has a folder of files kept in the object store (the Space's bucket),
# with this index so listings, quotas and permission checks never touch the bucket.
SCHEMA_V22 = """
CREATE TABLE storage_files (
    owner_id     INTEGER NOT NULL REFERENCES principals(id),
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    content_type TEXT,
    updated_at   REAL NOT NULL,
    updated_by   INTEGER NOT NULL REFERENCES principals(id),
    PRIMARY KEY (owner_id, path)
);
"""

# v23: suspensions. An admin can suspend a person or an agent with an optional reason; suspending a
# person suspends their agents too (suspended_via = that person), and lifting it brings those back.
SCHEMA_V23 = """
ALTER TABLE principals ADD COLUMN suspended_at REAL;
ALTER TABLE principals ADD COLUMN suspended_by INTEGER REFERENCES principals(id);
ALTER TABLE principals ADD COLUMN suspend_reason TEXT;
ALTER TABLE principals ADD COLUMN suspended_via INTEGER REFERENCES principals(id);
"""

# v24: batched observations of agents, separate from identity and permission state.
SCHEMA_V24 = """
CREATE TABLE principal_presence (
    principal_id  INTEGER PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
    last_seen_at  REAL,
    last_poll_at  REAL,
    listens       TEXT,
    cursor        INTEGER,
    cursor_since  REAL,
    waiting       INTEGER,
    waiting_since REAL
);
"""

# v25: sampled listening counts and bounded analytics lookups.
SCHEMA_V25 = """
CREATE TABLE presence_history (
    minute     INTEGER PRIMARY KEY,
    sampled_at REAL NOT NULL,
    agents     INTEGER NOT NULL CHECK (agents >= 0)
);
CREATE INDEX messages_created_at ON messages(created_at);
CREATE INDEX messages_response_thread ON messages(sender_id, thread_id, created_at, id);
CREATE INDEX messages_response_dm ON messages(channel_id, sender_id, created_at, id);
CREATE INDEX attachments_published_at ON attachments(published, created_at);
"""

# Agents are owned by people: an agent that an agent invited moves to the person at the top of the chain.
SCHEMA_V26 = """
WITH RECURSIVE up(agent, cur, path) AS (
    SELECT p.id, p.owner_id, ',' || p.id || ',' FROM principals p JOIN principals o ON o.id = p.owner_id
    WHERE o.identity_type != 'user'
  UNION ALL
    SELECT u.agent, o.owner_id, u.path || o.id || ',' FROM up u JOIN principals o ON o.id = u.cur
    WHERE o.identity_type != 'user' AND o.owner_id IS NOT NULL AND instr(u.path, ',' || o.id || ',') = 0
),
root(agent, person) AS (
    SELECT u.agent, u.cur FROM up u JOIN principals h ON h.id = u.cur WHERE h.identity_type = 'user'
)
UPDATE principals SET owner_id = (SELECT person FROM root WHERE root.agent = principals.id)
WHERE id IN (SELECT agent FROM root);
"""

# v27: activity cursors require message IDs that are never reused after deletion.
# Foreign keys are off during migrations; preserve IDs, references, indexes and the FTS index.
SCHEMA_V27 = """
CREATE TABLE messages_v27 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   INTEGER NOT NULL REFERENCES channels(id),
    seq          INTEGER NOT NULL,
    sender_id    INTEGER NOT NULL REFERENCES principals(id),
    body         TEXT NOT NULL,
    reply_to     INTEGER REFERENCES messages(id),
    attachments  TEXT,
    request_id   TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at   REAL NOT NULL,
    thread_id    INTEGER REFERENCES messages(id),
    edited_at    REAL,
    edit_rev     INTEGER,
    UNIQUE (channel_id, seq),
    UNIQUE (sender_id, request_id)
);
INSERT INTO messages_v27 SELECT id, channel_id, seq, sender_id, body, reply_to, attachments,
    request_id, payload_hash, created_at, thread_id, edited_at, edit_rev FROM messages;
DROP TABLE messages;
ALTER TABLE messages_v27 RENAME TO messages;
CREATE INDEX messages_thread ON messages(thread_id, seq);
CREATE INDEX messages_sender ON messages(sender_id, created_at);
CREATE INDEX messages_edits ON messages(channel_id, edit_rev) WHERE edit_rev IS NOT NULL;
CREATE INDEX messages_created_at ON messages(created_at);
CREATE INDEX messages_response_thread ON messages(sender_id, thread_id, created_at, id);
CREATE INDEX messages_response_dm ON messages(channel_id, sender_id, created_at, id);
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF body ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
-- Legacy boards may already have deleted their highest ID. Channel sequence totals plus
-- audited deletions bound the number of past posts, hence every automatically assigned ID.
-- This can overestimate the high-water mark; gaps are safe, reusing IDs is not.
UPDATE sqlite_sequence SET seq = max(seq,
    (SELECT COALESCE(SUM(next_seq - 1), 0) FROM channels) +
    (SELECT COALESCE(SUM(CASE action
        WHEN 'channel.delete' THEN COALESCE(json_extract(detail, '$.messages'), 0)
        WHEN 'message.delete' THEN 1 + COALESCE(json_extract(detail, '$.with_replies'), 0)
    END), 0) FROM audit_log WHERE action IN ('channel.delete', 'message.delete')))
WHERE name = 'messages';
"""

SCHEMA_V28 = """
ALTER TABLE messages ADD COLUMN imported INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN import_source TEXT;
INSERT INTO settings (key, value) VALUES ('transfer_board_id', lower(hex(randomblob(16))));
CREATE TABLE channel_imports (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    source_board_id TEXT NOT NULL,
    source_channel_id INTEGER NOT NULL,
    digest TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# v29: one-time membership backfill; subsequent sign-ins/restarts respect leaving.
SCHEMA_V29 = """
INSERT OR IGNORE INTO channel_members (channel_id, principal_id, added_by, added_at)
    SELECT c.id, p.id, NULL, CAST(strftime('%s', 'now') AS REAL)
    FROM channels c CROSS JOIN principals p
    WHERE c.name = 'general' AND c.kind = 'public' AND c.archived = 0
      AND p.identity_type = 'user' AND p.disabled = 0;
"""

# Index i holds the SQL that moves the schema from version i to i + 1.
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9,
              SCHEMA_V10, SCHEMA_V11, SCHEMA_V12, SCHEMA_V13, SCHEMA_V14, SCHEMA_V15, SCHEMA_V16, SCHEMA_V17, SCHEMA_V18, SCHEMA_V19, SCHEMA_V20, SCHEMA_V21, SCHEMA_V22,
              SCHEMA_V23, SCHEMA_V24, SCHEMA_V25, SCHEMA_V26, SCHEMA_V27, SCHEMA_V28, SCHEMA_V29]
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(RuntimeError):
    pass


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    """Open a connection with the pragmas every connection needs.

    isolation_level=None: no implicit transactions. Reads never hold a
    transaction open; writes use an explicit BEGIN IMMEDIATE.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def backup(conn: sqlite3.Connection, dest: str | os.PathLike) -> None:
    """Consistent snapshot through SQLite's backup API (includes WAL content)."""
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"backup target already exists: {dest}")
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()


def migrate(conn: sqlite3.Connection, db_path: str | os.PathLike) -> int:
    """Apply pending migrations. Back up first if the database already had data."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {current} is newer than this software ({SCHEMA_VERSION}); refusing to run"
        )
    if current == SCHEMA_VERSION:
        return current
    if current > 0:
        backup(conn, f"{db_path}.pre-v{SCHEMA_VERSION}-{int(time.time())}.bak")
    conn.execute("PRAGMA foreign_keys=OFF")  # must be set outside a transaction
    try:
        for version in range(current, SCHEMA_VERSION):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split(MIGRATIONS[version]):
                    conn.execute(statement)
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise SchemaError(f"migration to v{version + 1} broke foreign keys: {problems[:3]}")
                conn.execute(f"PRAGMA user_version={version + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return SCHEMA_VERSION


def _split(script: str) -> list[str]:
    """Split a migration script into statements (executescript would COMMIT for us)."""
    statements, buf = [], []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        candidate = "\n".join(buf).strip()
        if candidate.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    if "\n".join(buf).strip():
        raise SchemaError("incomplete statement in migration")
    return statements
