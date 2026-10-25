from __future__ import annotations

import time

import pytest

from msgboard.client import BoardError
from msgboard.config import LIMITS


def test_overloaded_reads_get_503_with_retry_after(env):
    _, _, ana = env.human("ana")
    db = env.server.board.db
    db.auth_cache.clear()
    db.queued_reads += LIMITS.max_queued_reads          # pretend the reader queue is full
    try:
        r = env.http(ana.token).get("/me")
        assert r.status_code == 503 and r.headers["retry-after"] == "2"
        assert r.json()["error"]["code"] == "overloaded"
    finally:
        db.queued_reads -= LIMITS.max_queued_reads
    assert ana.me()["external_id"] == "ana"             # and it recovers as soon as the queue drains


def test_auth_cache_does_not_outlive_a_suspension(env):
    _, _, ana = env.human("ana")
    ana_id = ana.me()["id"]
    ana.me()                                            # now cached
    env.op.suspend(ana_id, "testing")
    with pytest.raises(BoardError) as e:
        ana.me()
    assert e.value.status == 401
    env.op.unsuspend(ana_id)
    assert ana.me()["id"] == ana_id


def test_activity_long_poll_still_wakes_promptly(env):
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    ch = ana.create_channel("busy")
    cursor = bob.activity_page(channels=[ch["id"]])["next_cursor"]
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(bob.activity_page(cursor, wait=10, channels=[ch["id"]])))
    t.start()
    time.sleep(0.5)
    ana.post(ch["id"], "first"); ana.post(ch["id"], "second")
    t.join(15)
    assert [m["body"] for m in out["messages"]] == ["first", "second"]   # both posts in one answer


def test_a_runaway_read_is_cut_off(env):
    import asyncio
    from msgboard.errors import ApiError
    db = env.server.board.db
    forever = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) FROM r"
    t = time.monotonic()
    with pytest.raises(ApiError) as e:
        asyncio.run(db.read(lambda conn: conn.execute(forever).fetchone()))
    assert e.value.status == 503 and e.value.code == "too_slow"
    assert time.monotonic() - t < LIMITS.max_read_seconds + 5


def test_word_search_in_a_big_channel_stays_fast(env):
    import random
    _, _, ana = env.human("ana")
    _, _, bot = env.agent("bot")
    ch = ana.create_channel("big")
    db = env.server.board.db
    rnd = random.Random(7)
    vocab = [f"w{i}" for i in range(3000)] + "you put on the right call sleep well nurse night".split()
    with db._lock:                                    # 8000 messages straight into the database
        conn = db._writer_conn
        conn.execute("BEGIN IMMEDIATE")
        me = ana.me()["id"]
        for i in range(8000):
            body = " ".join(rnd.choice(vocab) for _ in range(40))
            conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
                            VALUES (?, ?, ?, ?, ?, '', ?)""", (ch["id"], i + 1, me, body, f"bulk-{i}", time.time()))
        conn.execute("UPDATE channels SET next_seq = 8001 WHERE id = ?", (ch["id"],))
        conn.execute("COMMIT")
    t = time.monotonic()
    for q in ("you put on", "the right call sleep", "w1 w2 w3", "zebra on"):
        bot.search(f"{q} in:#big", sort="relevance")
    assert time.monotonic() - t < 3                   # took minutes once a channel was big (2026-10-25)
