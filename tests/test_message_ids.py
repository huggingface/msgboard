from contextlib import closing

import pytest

from msgboard import db, store


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    with closing(db.connect(path)) as conn:
        with monkeypatch.context() as old:
            old.setattr(db, "MIGRATIONS", db.MIGRATIONS[:26])
            old.setattr(db, "SCHEMA_VERSION", 26)
            db.migrate(conn, path)
        yield conn, path


@pytest.mark.parametrize("empty", [False, True])
def test_message_id_migration_preserves_history_and_deleted_ids(legacy_db, empty):
    conn, path = legacy_db
    _, token = store.create_operator(conn, 0)
    p = store.authenticate(conn, token, 1)
    keep, _ = store.create_channel(conn, p, "keep", None, False, 1)
    discard, _ = store.create_channel(conn, p, "discard", None, False, 1)

    def post(channel, body, reply_to=None):
        # Seed legacy rows using the legacy schema, independent of fields the
        # current server serializer adds in later migrations.
        seq = conn.execute("SELECT next_seq FROM channels WHERE id = ?", (channel["id"],)).fetchone()[0]
        cur = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, reply_to, thread_id,
            request_id, payload_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', 1)""",
            (channel["id"], seq, p.id, body, reply_to, reply_to, body))
        conn.execute("UPDATE channels SET next_seq = ? WHERE id = ?", (seq + 1, channel["id"]))
        return {"id": cur.lastrowid}

    root = post(keep, "searchable")
    post(keep, "reply", reply_to=root["id"])
    removed = post(discard, "deleted message")
    store.delete_message(conn, p, discard["id"], removed["id"], 2)
    removed = post(discard, "deleted channel")
    store.delete_channel(conn, p, discard["id"], "discard", 2)
    if empty:
        store.delete_channel(conn, p, keep["id"], "keep", 2)
    before = [tuple(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    columns = ",".join(r[1] for r in conn.execute("PRAGMA table_info(messages)"))
    schema = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'messages' AND sql IS NOT NULL AND type != 'table' ORDER BY name"
    objects = [tuple(r) for r in conn.execute(schema)]

    assert db.migrate(conn, path) == db.SCHEMA_VERSION
    assert [tuple(r) for r in conn.execute(f"SELECT {columns} FROM messages ORDER BY id")] == before
    assert [tuple(r) for r in conn.execute(schema)] == objects
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert list(path.parent.glob(f"board.db.pre-v{db.SCHEMA_VERSION}-*.bak"))
    if empty:
        keep, _ = store.create_channel(conn, p, "keep", None, False, 3)
    else:
        assert [r[0] for r in conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'searchable'")] == [root["id"]]
    work = post(keep, "newsearchterm")
    assert work["id"] > removed["id"]
    assert [r[0] for r in conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'newsearchterm'")] == [work["id"]]
    store.edit_message(conn, p, keep["id"], work["id"], {"body": "editedsearchterm"}, 4)
    assert conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'newsearchterm'").fetchall() == []
    store.delete_message(conn, p, keep["id"], work["id"], 5)
    assert conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'editedsearchterm'").fetchall() == []
