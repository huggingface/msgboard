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
