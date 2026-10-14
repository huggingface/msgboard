from __future__ import annotations

import time

from msgboard import db as dbmod
from msgboard import store


def _db_at(path, version):
    conn = dbmod.connect(path)
    for v, script in enumerate(dbmod.MIGRATIONS[:version]):
        conn.execute("BEGIN IMMEDIATE")
        for statement in dbmod._split(script):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={v + 1}")
        conn.execute("COMMIT")
    return conn


def test_migration_from_v1_keeps_data(tmp_path):
    path = tmp_path / "board.db"
    conn = _db_at(path, 1)
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role, created_at)
                          VALUES ('local', 'operator', 'user', 'admin', 0)""")
    pid = cur.lastrowid
    conn.execute("INSERT INTO grants VALUES (?, 0, 'admin', 'role')", (pid,))
    conn.execute("INSERT INTO channels (name, created_by, created_at) VALUES ('c', ?, ?)", (pid, time.time()))
    conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
                    VALUES (1, 1, ?, 'kept', 'r', 'h', ?)""", (pid, time.time()))
    conn.execute("COMMIT")

    assert dbmod.migrate(conn, path) == dbmod.SCHEMA_VERSION
    assert list(tmp_path.glob(f"board.db.pre-v{dbmod.SCHEMA_VERSION}-*.bak"))  # backed up first
    assert conn.execute("SELECT body FROM messages").fetchone()[0] == "kept"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v7_maps_old_roles_and_grants(tmp_path):
    """Old roles become the Slack-like ones; agents get the operator as owner."""
    path = tmp_path / "board.db"
    conn = _db_at(path, 6)
    conn.execute("BEGIN IMMEDIATE")
    op, token = store_v6_operator(conn)
    rows = [("admin", "hf:boss", "user", "admin"), ("admin", "plain", "user", "coordinator"),
            ("admin", "watcher", "user", "observer"), ("p:9", "bot", "agent", "worker"),
            ("p:9", "eyes", "agent", "observer"), ("admin", "launcher", "system", "provisioner"),
            ("admin", "maker", "agent", "coordinator")]
    for issuer, ext, kind, role in rows:
        conn.execute("INSERT INTO principals (issuer, external_id, identity_type, role, created_at) VALUES (?, ?, ?, ?, 0)",
                     (issuer, ext, kind, role))
    conn.execute("INSERT INTO channels (name, created_by, created_at) VALUES ('c', ?, 0)", (op,))
    eyes = conn.execute("SELECT id FROM principals WHERE external_id = 'eyes'").fetchone()[0]
    conn.execute("INSERT INTO grants VALUES (?, 1, 'messages:read', 'role')", (eyes,))
    conn.execute("COMMIT")
    dbmod.migrate(conn, path)
    got = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT external_id, role, owner_id IS NOT NULL, disabled, can_create_channels, hf_username FROM principals")}
    assert got["operator"] == ("admin", 0, 0, 0, None)
    assert got["hf:boss"] == ("admin", 0, 0, 0, "boss")
    assert got["plain"][0] == "member" and got["watcher"][0] == "viewer"
    assert got["bot"] == ("member", 1, 0, 0, None)
    assert got["eyes"][:2] == ("guest", 1)
    assert got["launcher"][2] == 1                                           # provisioners are retired
    assert got["maker"][3] == 1                                              # coordinators keep channel creation
    assert [tuple(r) for r in conn.execute("SELECT principal_id FROM channel_members")] == [(eyes,)]
    assert conn.execute("SELECT kind FROM channels").fetchone()[0] == "public"
    assert store.authenticate(conn, token, time.time()).role == "admin"


def store_v6_operator(conn):
    """Insert an operator the way schema v6 stored it (role admin plus an admin grant)."""
    from msgboard import auth
    cur = conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role, created_at)
                          VALUES ('local', 'operator', 'user', 'admin', 0)""")
    pid = cur.lastrowid
    conn.execute("INSERT INTO grants VALUES (?, 0, 'admin', 'role')", (pid,))
    cred, token, digest = auth.new_token()
    conn.execute("INSERT INTO credentials (id, principal_id, secret_hash, created_at, expires_at) VALUES (?, ?, ?, 0, ?)",
                 (cred, pid, digest, time.time() + 60))
    return pid, token
