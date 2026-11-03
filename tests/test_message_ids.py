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
        payload, digest = store.normalize_payload(channel["id"], {"body": body, "reply_to": reply_to})
        return store.post_message(conn, p, channel["id"], body, payload, digest, 1)[0]

    root = post(keep, "searchable")
    post(keep, "reply", reply_to=root["id"])
    removed = post(discard, "deleted message")
    store.delete_message(conn, p, discard["id"], removed["id"], 2)
    removed = post(discard, "deleted channel")
    store.delete_channel(conn, p, discard["id"], "discard", 2)
    if empty:
        store.delete_channel(conn, p, keep["id"], "keep", 2)
    before = [tuple(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]
    schema = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'messages' AND sql IS NOT NULL AND type != 'table' ORDER BY name"
    objects = [tuple(r) for r in conn.execute(schema)]

    assert db.migrate(conn, path) == db.SCHEMA_VERSION
    assert [tuple(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")] == before
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
