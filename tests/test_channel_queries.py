from contextlib import closing

import pytest

from msgboard import db, store


@pytest.fixture
def board_db():
    with closing(db.connect(":memory:")) as conn:
        db.migrate(conn, ":memory:")
        pid, token = store.create_operator(conn, 0)
        yield conn, pid, store.authenticate(conn, token, 1)


def test_channel_listing_reuses_read_state(board_db):
    conn, pid, principal = board_db
    conn.executemany("INSERT INTO channels (name, created_by, created_at) VALUES (?, ?, 0)",
                     [(f"channel-{i}", pid) for i in range(100)])
    conn.execute("""INSERT INTO channel_reads (principal_id, channel_id, last_read_seq, thread_floor, updated_at)
                    VALUES (?, 1, 7, 3, 1)""", (pid,))

    queries = []
    conn.set_trace_callback(queries.append)
    try:
        channels = store.list_channels(conn, principal)
    finally:
        conn.set_trace_callback(None)

    assert len(channels) == 100
    assert channels[0]["last_read"] == 7
    assert all(c["last_read"] == 0 for c in channels[1:])
    assert all((c["unread"], c["for_me"], c["threads_for_me"]) == (0, 0, 0) for c in channels)
    # Allow one count query per channel and a constant number of shared lookups.
    # Read positions and the unread baseline must not be fetched per channel.
    assert len(queries) <= len(channels) + 6


@pytest.mark.parametrize("channel_count", [1, 100])
def test_channel_listing_batches_unread_counts(board_db, channel_count):
    conn, pid, principal = board_db
    sender = conn.execute("""INSERT INTO principals (issuer, external_id, identity_type, role, created_at, owner_id)
                             VALUES ('local', 'sender', 'agent', 'member', 0, ?)""", (pid,)).lastrowid
    conn.execute("UPDATE settings SET value = '25' WHERE key = 'reads_since'")
    expected = []
    for i in range(channel_count):
        cid = conn.execute("INSERT INTO channels (name, created_by, created_at, next_seq) VALUES (?, ?, 0, 5)",
                           (f"channel-{i}", pid)).lastrowid
        root = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
                               VALUES (?, 1, ?, 'my question', ?, 'seed', 10)""", (cid, pid, f"root-{i}")).lastrowid
        for seq in (2, 3, 4):
            mid = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash,
                                                       created_at, reply_to, thread_id)
                                  VALUES (?, ?, ?, 'answer', ?, 'seed', ?, ?, ?)""",
                               (cid, seq, sender, f"{cid}-{seq}", seq * 10,
                                root if seq == 4 else None, root if seq == 4 else None)).lastrowid
            if seq == 3:
                conn.execute("INSERT INTO message_mentions (message_id, principal_id) VALUES (?, ?)", (mid, pid))
        if i % 4:
            conn.execute("""INSERT INTO channel_reads (principal_id, channel_id, last_read_seq, thread_floor, updated_at)
                            VALUES (?, ?, 3, ?, 0)""", (pid, cid, 4 if i % 4 == 3 else 0))
        if i % 4 == 2:
            conn.execute("INSERT INTO thread_reads (principal_id, thread_id, last_read_seq, updated_at) VALUES (?, ?, 4, 0)",
                         (pid, root))
        expected.append([(0, 1, 2, 1), (3, 0, 1, 1), (3, 0, 0, 0), (3, 0, 0, 0)][i % 4])
    conn.execute("INSERT INTO channels (name, created_by, created_at) VALUES ('empty', ?, 0)", (pid,))
    expected.append((0, 0, 0, 0))

    queries = []
    conn.set_trace_callback(queries.append)
    try:
        channels = store.list_channels(conn, principal)
    finally:
        conn.set_trace_callback(None)
    keys = ("last_read", "unread", "for_me", "threads_for_me")
    assert [tuple(c[key] for key in keys) for c in channels] == expected
    # Public-channel unread queries must stay bounded as the channel count grows.
    assert len(queries) <= 8
