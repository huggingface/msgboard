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

# Index i holds the SQL that moves the schema from version i to i + 1.
MIGRATIONS = [SCHEMA_V1]
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
    for version in range(current, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
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
