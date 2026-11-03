from contextlib import closing

from msgboard import db, store


def test_channel_listing_reuses_read_state():
    with closing(db.connect(":memory:")) as conn:
        db.migrate(conn, ":memory:")
        pid, token = store.create_operator(conn, 0)
        principal = store.authenticate(conn, token, 1)
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
