from __future__ import annotations

import asyncio
import time
from argparse import Namespace
from types import SimpleNamespace

import pytest

from msgboard import db as dbmod, store
from msgboard.analytics import DAY, listening_series
from msgboard.cli import cmd_init
from msgboard.client import BoardError, Client
from msgboard.local import ThreadedServer
from msgboard.presence import Presence
from msgboard.replicate import DirStore, Replicator, restore

from .conftest import init_board

BASE = 20000 * DAY  # historical local fixtures, never live data


def write(env, fn, *args):
    return asyncio.run(env.server.board.db.write(fn, *args))


def retime(env, messages=(), attachments=()):
    def update(conn):
        conn.executemany("UPDATE messages SET created_at = ? WHERE id = ?", [(at, msg["id"]) for msg, at in messages])
        conn.executemany("UPDATE attachments SET created_at = ? WHERE id = ?", [(at, att["id"]) for att, at in attachments])
    write(env, update)


def test_empty_default_and_permissions(env):
    report = env.op.analytics()
    assert report["days"] == 7 and len(report["daily"]) == 7
    assert report["totals"]["messages_window"] == report["message_stats"]["messages"] == 0
    assert report["message_stats"]["average_length"] is None
    assert report["response_times"]["median_seconds"] is None
    assert report["response_times"]["requests"] == 0
    assert report["interactions"]["nodes"] == report["interactions"]["links"] == []
    assert report["files"] == {"count": 0, "bytes": 0, "by_principal": []}
    assert report["health"]["db_bytes"] > 0
    assert report["listening"]["sample_seconds"] == 60
    _, _, reader = env.agent("reader", role="reader")
    assert reader.analytics()["health"]["db_bytes"] > 0
    with env.http(reader.token) as http:
        assert http.get("/metrics").status_code == 403
    for role in ("member", "viewer", "guest"):
        _, _, person = env.human("person-" + role, role=role)
        with pytest.raises(BoardError) as error:
            person.analytics()
        assert error.value.status == 403
    with env.http(None) as http:
        assert http.get("/analytics").status_code == 401


@pytest.mark.parametrize("params", [
    {"from": BASE, "to": BASE}, {"from": BASE + 1, "to": BASE},
    {"from": -1, "to": BASE}, {"from": "nan", "to": BASE},
    {"from": BASE, "to": "inf"}, {"from": 1, "to": 367 * DAY},
    {"from": 1e20 - DAY, "to": 1e20},
    {"top_k": 0}, {"top_k": 101}, {"days": 0}, {"days": 366},
])
def test_invalid_range(env, params):
    with env.http(env.operator_token) as http:
        assert http.get("/analytics", params=params).status_code in (400, 422)


def test_half_open_range_unicode_words_and_legacy_counts(env):
    ch = env.op.create_channel("general")
    bot_id, _, bot = env.agent("bot")
    text = "Hello, café! Don't stop.\n**two_words** 中文 🙂"
    before = env.op.post(ch["id"], "excluded before")
    first = env.op.post(ch["id"], text)
    second = bot.post(ch["id"], "One\n2")
    after = bot.post(ch["id"], "excluded at end")
    retime(env, [(before, BASE + 99), (first, BASE + 100), (second, BASE + 200), (after, BASE + 300)])
    report = env.op.analytics(start=BASE + 100, end=BASE + 300)
    assert report["range"] == {"from": BASE + 100, "to": BASE + 300, "bucket_seconds": DAY}
    stats = report["message_stats"]
    assert stats["messages"] == 2 and stats["words"] == 8
    assert stats["characters"] == len(text) + len("One\n2")
    assert stats["average_length"] == stats["characters"] / 2
    assert report["daily"] == [{"day": BASE, "people": 1, "agents": 1}]
    assert stats["daily"][0]["words"] == 8
    assert report["totals"]["messages"] == 4 and report["totals"]["messages_window"] == 2
    assert report["channels"][0]["messages"] == 2
    by_pid = {p["id"]: p for p in report["posters"]}
    assert by_pid[bot_id["id"]]["messages"] == 1 and by_pid[bot_id["id"]]["daily"] == [1]
    # A partial-day custom interval crossing midnight retains both UTC days.
    cross = env.op.analytics(start=BASE + DAY - 1, end=BASE + DAY + 1)
    assert [row["day"] for row in cross["daily"]] == [BASE, BASE + DAY]


def test_response_distribution_pairing_and_unanswered_as_of_end(env):
    ch = env.op.create_channel("requests")
    elsewhere = env.op.create_channel("elsewhere")
    bot_id, _, bot = env.agent("bot")
    other_id, _, other = env.agent("other")
    request = env.op.post(ch["id"], "@bot question")
    wrong_thread = bot.post(elsewhere["id"], "not the response")
    answer = bot.post(ch["id"], "written response", reply_to=request["id"])
    dm = env.op.open_dm(bot_id["id"])
    dm_request = env.op.post(dm["id"], "@bot dm question")
    unanswered = env.op.post(ch["id"], "@bot unanswered within the range")
    multi = env.op.post(ch["id"], "@bot @other two recipients")
    other_answer = other.post(ch["id"], "answer", reply_to=multi["id"])
    self_mention = bot.post(ch["id"], "@bot myself")
    dm_answer = bot.post(dm["id"], "dm response")
    late_answer = bot.post(ch["id"], "too late for this range", reply_to=unanswered["id"])
    bot.react(ch["id"], unanswered["id"], "👀")
    retime(env, [(request, BASE + 100), (wrong_thread, BASE + 130), (answer, BASE + 160),
                 (dm_request, BASE + 200), (unanswered, BASE + 300), (multi, BASE + 400),
                 (other_answer, BASE + 430), (self_mention, BASE + 500), (dm_answer, BASE + 800),
                 (late_answer, BASE + 1100)])
    responses = env.op.analytics(start=BASE, end=BASE + 1000)["response_times"]
    assert responses["requests"] == 5
    assert responses["answered"] == 3 and responses["unanswered"] == 2
    assert responses["median_seconds"] == 60
    assert responses["p90_seconds"] == pytest.approx(492)
    assert sum(row["count"] for row in responses["histogram"]) == 3
    assert [row["count"] for row in responses["histogram"][:3]] == [1, 1, 1]
    by_pid = {row["id"]: row for row in responses["agents"]}
    assert by_pid[bot_id["id"]]["requests"] == 4  # DM plus mention remains one request
    assert by_pid[other_id["id"]]["median_seconds"] == 30
    # Moving the selection end includes the formerly late answer.
    later = env.op.analytics(start=BASE, end=BASE + 1200)["response_times"]
    assert later["answered"] == 4 and later["unanswered"] == 1


def test_each_prior_dm_request_uses_first_later_message_and_replies_outside_cohort(env):
    bot_id, _, bot = env.agent("bot")
    dm = env.op.open_dm(bot_id["id"])
    old = env.op.post(dm["id"], "request before selected start")
    q1 = env.op.post(dm["id"], "one")
    q2 = env.op.post(dm["id"], "two")
    answer = bot.post(dm["id"], "answer")
    retime(env, [(old, BASE + 99), (q1, BASE + 100), (q2, BASE + 110), (answer, BASE + 110)])
    response = env.op.analytics(start=BASE + 100, end=BASE + 200)["response_times"]
    assert response["requests"] == response["answered"] == 2
    assert response["unanswered"] == 0 and response["median_seconds"] == 5
    assert response["histogram"][0]["count"] == 2  # includes a zero-second response


def test_chord_dms_replies_mentions_and_other_totals(env):
    ch = env.op.create_channel("general")
    a_id, _, a = env.human("person-a")
    b_id, _, b = env.human("person-b")
    x_id, _, x = env.agent("agent-x")
    y_id, _, y = env.agent("agent-y")
    request = a.post(ch["id"], "@agent-x question")
    x.post(ch["id"], "@person-a answer", reply_to=request["id"])
    dm = a.open_dm(x_id["id"])
    dm_request = a.post(dm["id"], "@agent-x dm")
    x.post(dm["id"], "@person-a reply", reply_to=dm_request["id"])
    a.post(ch["id"], "@agent-x @agent-y group")
    q = b.post(ch["id"], "@agent-y question")
    y.post(ch["id"], "@person-b reply", reply_to=q["id"])
    b.post(ch["id"], "@person-a same type, excluded")
    x.post(ch["id"], "@agent-y same type, excluded")
    graph = env.op.analytics(top_k=100)["interactions"]
    assert graph["total"] == 8
    pairs = {frozenset((link["source"], link["target"])): link for link in graph["links"]}
    ax = pairs[frozenset((a_id["id"], x_id["id"]))]
    assert {key: ax[key] for key in ("mentions", "replies", "dms", "total")} == {
        "mentions": 2, "replies": 1, "dms": 2, "total": 5}
    for top_k in (1, 2, 3, 4, 100):
        grouped = env.op.analytics(top_k=top_k)["interactions"]
        assert grouped["total"] == sum(link["total"] for link in grouped["links"]) == 8
        assert sum(node["total"] for node in grouped["nodes"]) == 16  # two endpoints per interaction
        for link in grouped["links"]:
            assert link["total"] == link["mentions"] + link["replies"] + link["dms"]
    grouped = env.op.analytics(top_k=1)["interactions"]
    assert grouped["nodes"][0]["id"] == a_id["id"]
    assert grouped["other_internal"]["total"] == 2  # hidden person-b <-> agent-y remains visible in Other
    other = next(node for node in grouped["nodes"] if node["id"] == "other")
    assert other["members"] == 3
    assert env.op.analytics(top_k=2)["interactions"] == env.op.analytics(top_k=2)["interactions"]


def test_published_attachments_counts_bytes_and_people_agents(env):
    ch = env.op.create_channel("files")
    bot_id, _, bot = env.agent("bot")
    selected = env.op.upload(ch["id"], b"abc", filename="selected.txt")
    env.op.post(ch["id"], "with file", attachments=[selected["id"]])
    agent_file = bot.upload(ch["id"], b"12345", filename="agent.txt")
    bot.post(ch["id"], "with file", attachments=[agent_file["id"]])
    unpublished = bot.upload(ch["id"], b"unpublished", filename="draft.txt")
    old = env.op.upload(ch["id"], b"old", filename="old.txt")
    env.op.post(ch["id"], "old", attachments=[old["id"]])
    end_file = env.op.upload(ch["id"], b"at end", filename="end.txt")
    env.op.post(ch["id"], "end", attachments=[end_file["id"]])
    retime(env, attachments=[(selected, BASE + 100), (agent_file, BASE + 200), (unpublished, BASE + 300),
                             (old, BASE + 99), (end_file, BASE + 1000)])
    report = env.op.analytics(start=BASE + 100, end=BASE + 1000)
    files = report["files"]
    assert files["count"] == report["totals"]["attachments_window"] == 2
    assert files["bytes"] == 8
    assert next(row for row in files["by_principal"] if row["id"] == bot_id["id"])["bytes"] == 5
    assert sum(row["count"] for row in files["by_principal"]) == 2


def test_sampled_history_zero_gaps_pending_flush_and_restart(env):
    cache = Presence()
    cache.sample(BASE + 125)  # a real observed zero, not inferred emptiness
    agent = SimpleNamespace(id=999, is_human=False)
    cache.polled(agent, cache.next_poll(), BASE + 170, 0, "addressed", [], False)
    cache.sample(BASE + 185)
    cache.sample(BASE + 190)  # same minute, no second sample
    cache.sample(BASE + 305)  # jump over minute 240; it must remain unknown
    pending = cache.pending_history()
    assert len(pending) == 3 and pending[BASE + 180][1] == 1 and pending[BASE + 300][1] == 0
    # Only the history goes onto the real board; fake principal 999 is not persisted.
    write(env, store.save_presence, {}, pending)
    cache.sample(BASE + 365)
    cache.history_saved(pending)
    assert list(cache.pending_history()) == [BASE + 360]
    report = env.op.analytics(start=BASE, end=BASE + 360)["listening"]
    assert report["started_at"] == BASE + 125
    assert [row["count"] for row in report["series"]] == [None, None, 0, 1, None, 0]
    assert report["series"][4]["samples"] == 0 and report["series"][4]["expected_samples"] == 1
    env.restart()
    assert env.op.analytics(start=BASE, end=BASE + 360)["listening"]["series"] == report["series"]


def test_unflushed_samples_visible_without_request_writes(env):
    cache = Presence()
    cache.sample(BASE + 5)
    env.server.board.presence = cache
    before = env.server.board.db.generation
    report = env.op.analytics(start=BASE, end=BASE + 60)
    assert report["listening"]["series"][0]["count"] == 0
    assert report["listening"]["started_at"] == BASE + 5
    assert env.server.board.db.generation == before
    asyncio.run(env.server.board.flush_presence())
    assert env.server.board.db.generation == before + 1
    assert not cache.pending_history()
    assert env.op.analytics(start=BASE, end=BASE + 60)["listening"]["series"][0]["count"] == 0


def test_long_history_is_bounded_and_partial_coverage_is_explicit():
    data = {BASE: (BASE + 1, 4), BASE + 120: (BASE + 121, 2)}
    report = listening_series(BASE, BASE + 90 * DAY, data, BASE + 1)
    assert report["sample_seconds"] == 60 and report["bucket_seconds"] > 60
    assert len(report["series"]) <= 1501
    first = next(row for row in report["series"] if row["samples"])
    assert first["count"] == 3 and first["min"] == 2 and first["max"] == 4
    assert first["samples"] == 2 < first["expected_samples"]
    assert any(row["count"] is None for row in report["series"])


def test_history_survives_final_replication(tmp_path):
    data, replica = tmp_path / "original", DirStore(tmp_path / "replica")
    token = init_board(data)
    with ThreadedServer(data, replicator=Replicator(replica, data, interval=3600)) as server:
        # Replace initial live sample with a deterministic historical sample.
        cache = Presence()
        cache.sample(BASE + 10)
        server.board.presence = cache
        server.board.begin_shutdown()
    restored = tmp_path / "restored"
    restore(replica, restored)
    with ThreadedServer(restored) as server, Client(server.url, token) as client:
        history = client.analytics(start=BASE, end=BASE + 60)["listening"]
        assert history["started_at"] == BASE + 10 and history["series"][0]["count"] == 0


def test_migrate_v24_preserves_presence(tmp_path):
    data = tmp_path / "board"
    token = init_board(data)
    with ThreadedServer(data) as server, Client(server.url, token) as op:
        invitation = op.create_invite("agent")
        with Client.join(server.url, invitation["code"], "bot") as bot:
            bot.activity_page(11)
            pid = bot.identity["id"]
            expected = server.board.presence.snapshot()[pid]
    with dbmod.connect(data / "board.db") as conn:
        conn.execute("DROP TABLE presence_history")
        for index in ("messages_created_at", "messages_response_thread", "messages_response_dm", "attachments_published_at"):
            conn.execute(f"DROP INDEX {index}")
        conn.execute("PRAGMA user_version=24")
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    with ThreadedServer(data) as server:
        with dbmod.connect(data / "board.db") as conn:
            assert store.load_presence(conn)[pid] == expected
    assert list(data.glob(f"*.pre-v{dbmod.SCHEMA_VERSION}-*.bak"))


def test_response_lookup_remains_bounded_for_many_threads(env):
    channel = env.op.create_channel("bulk")
    bot_id, _, _ = env.agent("bot")
    operator_id = env.op.me()["id"]

    def seed(conn):
        seq = 1
        for i in range(1200):
            at = BASE + i
            request = conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at)
                                      VALUES (?, ?, ?, '@bot request', ?, '', ?)""",
                                   (channel["id"], seq, operator_id, f"request-{i}", at)).lastrowid
            conn.execute("INSERT INTO message_mentions (message_id, principal_id) VALUES (?, ?)", (request, bot_id["id"]))
            conn.execute("""INSERT INTO messages (channel_id, seq, sender_id, body, request_id, payload_hash, created_at, reply_to, thread_id)
                            VALUES (?, ?, ?, 'response', ?, '', ?, ?, ?)""",
                         (channel["id"], seq + 1, bot_id["id"], f"response-{i}", at + 5, request, request))
            seq += 2
        conn.execute("UPDATE channels SET next_seq = ? WHERE id = ?", (seq, channel["id"]))

    write(env, seed)
    started = time.monotonic()
    report = env.op.analytics(start=BASE, end=BASE + 1800)
    assert time.monotonic() - started < 5
    assert report["response_times"]["answered"] == 1200
    assert report["response_times"]["median_seconds"] == 5
