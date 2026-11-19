from __future__ import annotations

import asyncio
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx

from msgboard import db as dbmod, store
from msgboard.cli import cmd_init
from msgboard.client import Client
from msgboard.errors import ApiError
from msgboard.local import ThreadedServer
from msgboard.presence import Presence, unknown
from msgboard.replicate import DirStore, Replicator, restore

from .conftest import Env, init_board


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)


def get_presence(env, pid, token=None):
    with env.http(token or env.operator_token) as http:
        response = http.get(f"/principals/{pid}/profile")
        response.raise_for_status()
        return response.json()["presence"]


def test_modes_contract_and_no_human_presence(env):
    a = env.op.create_channel("a")
    b = env.op.create_channel("b")
    bot_id, _, bot = env.agent("bot", channels=["a"])
    with env.http(env.operator_token) as http:
        principals = http.get("/principals").json()["principals"]
        assert next(p for p in principals if p["id"] == bot_id["id"])["presence"] == unknown()
        assert env.op.me()["presence"] is None

    initial = bot.activity_page(channels="joined")
    live = get_presence(env, bot_id["id"])
    assert live["last_seen_at"] and live["last_poll_at"]
    assert live["listens"] == {"mode": "joined", "channel_ids": [a["id"]]}
    assert live["cursor"] is None and live["cursor_since"] is None
    assert live["waiting"] is None and live["waiting_since"] is None

    cursor = initial["next_cursor"]
    bot.activity_page(cursor, channels=[b["id"], a["id"], b["id"]])
    live = get_presence(env, bot_id["id"])
    assert live["listens"] == {"mode": "channels", "channel_ids": [a["id"], b["id"]]}
    assert live["cursor"] == cursor and live["cursor_since"]
    assert live["waiting"] is False
    with env.http(bot.token) as http:
        assert http.get(f"/activity?after={cursor}&channels=joined,{b['id']}").status_code == 200
    assert get_presence(env, bot_id["id"])["listens"] == {
        "mode": "joined", "channel_ids": [a["id"], b["id"]]}

    for scope in (None, "", ","):
        with env.http(bot.token) as http:
            query = {"after": cursor} if scope is None else {"after": cursor, "channels": scope}
            assert http.get("/activity", params=query).status_code == 200
        assert get_presence(env, bot_id["id"])["listens"] == {"mode": "addressed", "channel_ids": []}

    # Even people's activity polls never create observations.
    env.op.activity_page(cursor, channels="joined")
    assert env.op.me()["presence"] is None
    assert env.op.me()["id"] not in env.server.board.presence.snapshot()
    with env.http(env.operator_token) as http:
        assert http.put("/me/presence", json={"active": True}).status_code == 404


def test_cached_auth_and_failed_polls(env):
    bot_id, _, bot = env.agent("bot")
    _, _, person = env.human("person")
    secret = person.create_channel("secret", private=True)
    bot.activity_page(0)
    before = get_presence(env, bot_id["id"])
    cache = env.server.board.db.auth_cache
    assert bot.token in cache
    with env.http(None) as http:
        assert http.get("/me", headers={"X-Msgboard-Token": bot.token}).status_code == 200
        assert get_presence(env, bot_id["id"])["last_seen_at"] > before["last_seen_at"]
        assert http.get("/activity", headers={"Authorization": "Bearer invalid"}).status_code == 401
    for params, status in [({"channels": "oops"}, 400),
                           ({"channels": secret["id"]}, 404), ({"wait": 1}, 400)]:
        with env.http(bot.token) as http:
            assert http.get("/activity", params=params).status_code == status
        after = get_presence(env, bot_id["id"])
        assert {key: after[key] for key in before if key != "last_seen_at"} == {
            key: before[key] for key in before if key != "last_seen_at"}


def test_matching_backlog_and_cursor_progress(env):
    ch = env.op.create_channel("general")
    other = env.op.create_channel("other")
    bot_id, _, bot = env.agent("bot", channels=["general"])
    cursor = bot.activity_page()["next_cursor"]
    env.op.post(other["id"], "unrelated")
    env.op.post(ch["id"], "unaddressed")
    bot.post(ch["id"], "my own message")
    bot.activity_page(cursor)
    assert get_presence(env, bot_id["id"])["waiting"] is False
    sent = env.op.post(ch["id"], "@bot matching")
    bot.activity_page(cursor)
    waiting = get_presence(env, bot_id["id"])
    assert waiting["waiting"] is True and waiting["waiting_since"]
    bot.activity_page(cursor)
    again = get_presence(env, bot_id["id"])
    assert again["waiting_since"] == waiting["waiting_since"]
    assert again["cursor_since"] == waiting["cursor_since"]
    bot.activity_page(sent["id"])
    progressed = get_presence(env, bot_id["id"])
    assert progressed["cursor"] == sent["id"] and progressed["waiting"] is False
    assert progressed["waiting_since"] is None
    assert progressed["cursor_since"] > waiting["cursor_since"]


def test_held_poll_visible_and_new_messages_update_waiting(env):
    ch = env.op.create_channel("chat")
    bot_id, _, bot = env.agent("bot", channels=["chat"])
    cursor = bot.activity_page()["next_cursor"]
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(bot.activity_page, cursor, wait=3, channels="joined")
        wait_for(lambda: env.server.board.open_polls == 1)
        wait_for(lambda: get_presence(env, bot_id["id"])["cursor"] == cursor)
        live = get_presence(env, bot_id["id"])
        assert live["last_poll_at"] and not live["waiting"] and not future.done()
        env.op.post(ch["id"], "work")
        assert future.result(timeout=5)["messages"]
    live = get_presence(env, bot_id["id"])
    assert live["waiting"] is True and live["waiting_since"]


def test_directory_visibility_and_private_listening_scope(env):
    shared = env.op.create_channel("shared")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    guest_id, _, guest = env.human("guest", role="guest")
    env.op.add_member(shared["id"], guest_id["id"])
    secret = ana.create_channel("secret", private=True)
    bot_id, _, bot = env.agent("bot", owner=ana, channels=["shared"])
    ana.add_member(secret["id"], bot_id["id"])
    unseen_id, _, _ = env.agent("unseen")
    bot.activity_page(0, channels=[secret["id"], shared["id"]])

    for client, expected in [(env.op, {bot_id["id"], unseen_id["id"]}),
                             (ana, {bot_id["id"]}), (bob, set()), (guest, set()),
                             (bot, {bot_id["id"]})]:
        with env.http(client.token) as http:
            response = http.get("/presence")
            assert response.status_code == 200
            assert {p["id"] for p in response.json()["principals"]} == expected
    with env.http(env.operator_token) as http:
        assert http.get("/presence").json()["principals"][0]["id"] == bot_id["id"]
    assert env.op.presence()[0]["id"] == bot_id["id"]
    with env.http(None) as http:
        assert http.get("/presence").status_code == 401

    for client in (bob, guest):
        with env.http(client.token) as http:
            principal = next(p for p in http.get("/principals").json()["principals"] if p["id"] == bot_id["id"])
            profile = http.get(f"/principals/{bot_id['id']}/profile").json()
            member = next(p for p in http.get(f"/channels/{shared['id']}/members").json()["members"] if p["id"] == bot_id["id"])
            info_member = next(p for p in http.get(f"/channels/{shared['id']}/info").json()["members"] if p["id"] == bot_id["id"])
            for item in (principal, profile, member, info_member):
                assert item["presence"]["listens"]["channel_ids"] == [shared["id"]]
    assert get_presence(env, bot_id["id"], ana.token)["listens"]["channel_ids"] == [shared["id"], secret["id"]]
    # Serialization must not modify the underlying unfiltered observation.
    assert env.server.board.presence.snapshot()[bot_id["id"]]["listens"]["channel_ids"] == [shared["id"], secret["id"]]
    with env.http(guest.token) as http:
        assert http.get(f"/principals/{unseen_id['id']}/profile").status_code == 404


def test_polls_are_batched_and_flush_preserves_auth_cache(env):
    bot_id, _, bot = env.agent("bot")
    db = env.server.board.db
    start = db.generation
    for _ in range(20):
        bot.activity_page(0)
    assert db.generation == start
    assert bot.token in db.auth_cache
    latest = env.server.board.presence.snapshot()[bot_id["id"]]
    asyncio.run(env.server.board.flush_presence())
    assert db.generation == start + 1
    assert bot.token in db.auth_cache
    with dbmod.connect(env.data_dir / "board.db") as conn:
        assert store.load_presence(conn)[bot_id["id"]] == latest
    asyncio.run(env.server.board.flush_presence())
    assert db.generation == start + 1  # a clean batch is not a write


def test_periodic_flush_retries_overload(tmp_path, monkeypatch):
    monkeypatch.setattr("msgboard.server.PRESENCE_FLUSH_SECONDS", 0.05)
    env = Env(tmp_path / "board")
    try:
        bot_id, _, bot = env.agent("bot")
        original = env.server.board.db.write
        attempts = []

        async def overloaded_once(fn, *args):
            if fn is store.save_presence:
                attempts.append(1)
                if len(attempts) == 1:
                    raise ApiError(503, "overloaded", "busy")
            return await original(fn, *args)

        monkeypatch.setattr(env.server.board.db, "write", overloaded_once)
        bot.activity_page(0)
        wait_for(lambda: len(attempts) >= 2 and not env.server.board.presence.pending()[0])
        with dbmod.connect(env.data_dir / "board.db") as conn:
            assert store.load_presence(conn)[bot_id["id"]]["cursor"] == 0
    finally:
        env.close()


def test_final_presence_in_replication_snapshot(tmp_path):
    data, replica = tmp_path / "original", DirStore(tmp_path / "replica")
    token = init_board(data)
    with ThreadedServer(data, replicator=Replicator(replica, data, interval=3600)) as server:
        with Client(server.url, token) as op:
            invitation = op.create_invite("agent")
            with Client.join(server.url, invitation["code"], "bot") as bot:
                bot.activity_page(17)
                pid = bot.identity["id"]
                expected = server.board.presence.snapshot()[pid]
        # This closes regular writes before Board.close; final presence still persists.
        server.board.begin_shutdown()
    fresh = tmp_path / "restored"
    restore(replica, fresh)
    with ThreadedServer(fresh) as server:
        with httpx.Client(base_url=server.url, headers={"Authorization": f"Bearer {token}"}) as http:
            assert http.get(f"/v1/principals/{pid}/profile").json()["presence"] == expected


def test_migrate_existing_board_to_presence(tmp_path):
    data = tmp_path / "old"
    token = init_board(data)
    with dbmod.connect(data / "board.db") as conn:
        conn.execute("DROP TABLE principal_presence")
        conn.execute("DROP TABLE presence_history")
        for index in ("messages_created_at", "messages_response_thread", "messages_response_dm", "attachments_published_at"):
            conn.execute(f"DROP INDEX {index}")
        for table in ("thread_subscriptions", "task_receipts", "task_sources", "task_notices", "task_items", "task_orders"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=23")
        conn.execute("DROP TABLE channel_imports")
        conn.execute("DELETE FROM settings WHERE key = 'transfer_board_id'")
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    with ThreadedServer(data) as server, Client(server.url, token) as op:
        assert op.me()["presence"] is None
    assert list(data.glob(f"*.pre-v{dbmod.SCHEMA_VERSION}-*.bak"))


def test_presence_flush_failure_does_not_skip_final_messages(tmp_path, monkeypatch):
    data, replica = tmp_path / "original", DirStore(tmp_path / "replica")
    token = init_board(data)
    with ThreadedServer(data, replicator=Replicator(replica, data, interval=3600)) as server:
        with Client(server.url, token) as op:
            channel = op.create_channel("keep")
            invitation = op.create_invite("agent")
            with Client.join(server.url, invitation["code"], "bot") as bot:
                bot.post(channel["id"], "acknowledged before shutdown")

        def fail_presence(conn, records, history=None):
            raise RuntimeError("presence flush failed")

        monkeypatch.setattr(store, "save_presence", fail_presence)
    fresh = tmp_path / "restored"
    restore(replica, fresh)
    with ThreadedServer(fresh) as server, Client(server.url, token) as op:
        assert [m["body"] for m in op.read(channel["id"])["messages"]] == ["acknowledged before shutdown"]


def test_cursor_timers_concurrency_and_dirty_versions():
    agent = SimpleNamespace(id=1, is_human=False)
    cache = Presence()
    cache.seen(agent, 1000)
    first = cache.next_poll()
    cache.polled(agent, first, 1000, 431, "addressed", [], False)
    assert cache.snapshot()[1]["waiting_since"] is None
    # A quiet cursor remains normal; new backlog starts its own timer.
    for now in range(1045, 1721, 45):
        cache.polled(agent, cache.next_poll(), now, 431, "addressed", [], False)
    cache.polled(agent, cache.next_poll(), 1730, 431, "addressed", [], True)
    assert cache.snapshot()[1]["waiting_since"] == 1730
    for now in range(1775, 2380, 45):
        cache.polled(agent, cache.next_poll(), now, 431, "addressed", [], True)
    assert cache.snapshot()[1]["waiting_since"] == 1730
    assert cache.snapshot()[1]["cursor_since"] == 1000
    current = cache.next_poll()
    cache.polled(agent, current, 2390, 500, "channels", [2], False)
    cache.observed(agent, first, True, 2391)
    cache.polled(agent, first, 1001, 431, "addressed", [], True)
    assert cache.snapshot()[1]["cursor"] == 500 and not cache.snapshot()[1]["waiting"]
    # An update during a database flush must remain dirty after that flush completes.
    versions, batch = cache.pending()
    cache.seen(agent, 2392)
    cache.saved(versions)
    assert cache.pending()[0] and batch[1]["last_seen_at"] == 1000
    assert cache.snapshot()[1]["last_seen_at"] == 2392
    # Polling gaps interrupt the evidence of continuously stuck work.
    cache.polled(agent, cache.next_poll(), 2500, 500, "channels", [2], True)
    assert cache.snapshot()[1]["waiting_since"] == 2500
    cache.polled(agent, cache.next_poll(), 2600, 500, "channels", [2], True)
    assert cache.snapshot()[1]["waiting_since"] == 2600
    # A scope change at the same cursor starts a new observation window.
    cache.polled(agent, cache.next_poll(), 2601, 500, "channels", [3], True)
    assert cache.snapshot()[1]["cursor_since"] == 2601
    assert cache.snapshot()[1]["waiting_since"] == 2601
