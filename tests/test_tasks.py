"""Personal tasks use real identities, ordinary messages and durable receipts."""

import concurrent.futures
import hashlib
import json
import threading
import time
from argparse import Namespace
from contextlib import closing

import pytest

from msgboard import db, store
from msgboard.cli import cmd_init
from msgboard.client import BoardError, Client
from msgboard.local import ThreadedServer


def fails(status, fn, *args, **kwargs):
    with pytest.raises(BoardError) as exc:
        fn(*args, **kwargs)
    assert exc.value.status == status
    return exc.value


def ids(page):
    return [item["id"] for item in page["items"]]


def test_tasks_is_personal_and_only_writable_agents_propose(env):
    _, _, owner = env.human("owner")
    _, _, other = env.human("other", role="admin")
    _, _, writer = env.agent("writer", owner=owner)
    _, _, helper = env.agent("helper", owner=owner)
    _, _, stranger = env.agent("stranger", owner=other, role="admin")
    _, _, reader = env.agent("reader", role="reader")
    item = writer.put_task("release.check", "Review release")
    assert ids(owner.tasks()) == ids(helper.tasks()) == [item["id"]]
    assert other.tasks()["items"] == stranger.tasks()["items"] == []
    for client in (env.op, other, stranger):
        fails(404, client.task, item["id"])
        fails(404, client.task_action, item["id"], "done", item["version"])
    for client in (owner, reader):
        fails(403, client.put_task, "human-proposal", "Not allowed")
    fails(403, reader.task_action, item["id"], "done", item["version"])
    fails(403, helper.task_action, item["id"], "answer", item["version"], body="yes")
    closed = helper.task_action(item["id"], "done", item["version"])["item"]
    assert (closed["state"], closed["closed_reason"]) == ("closed", "done")
    assert owner.tasks()["counts"] == {"open": 0, "archived": 1}
    independent = helper.put_task("release.check", "Another agent’s request")
    assert independent["id"] != item["id"]
    assert owner.tasks()["counts"] == {"open": 1, "archived": 1}


def test_source_permissions_cover_proposer_owner_and_current_reader(env):
    owner_id, _, owner = env.human("owner")
    writer_id, _, writer = env.agent("writer", owner=owner)
    _, _, sibling = env.agent("sibling", owner=owner, role="guest")
    private = owner.create_channel("owner-secret", private=True)
    source = owner.post(private["id"], "private source body")
    fails(404, writer.put_task, "not-readable", "Review", sources=[source["id"]])
    owner.add_member(private["id"], writer_id["id"])
    item = writer.put_task("readable", "Review", sources=[source["id"]])
    assert "private source body" in json.dumps(owner.task(item["id"]))
    fails(404, sibling.task, item["id"])
    assert item["id"] not in ids(sibling.tasks())

    agent_only = env.op.create_channel("agent-secret", private=True)
    env.op.add_member(agent_only["id"], writer_id["id"])
    secret = env.op.post(agent_only["id"], "not for the owner")
    fails(404, writer.put_task, "owner-cannot-read", "Review", sources=[secret["id"]])
    env.op.add_member(agent_only["id"], owner_id["id"])
    allowed = writer.put_task("owner-can-read", "Review", sources=[secret["id"]])
    env.op.remove_member(agent_only["id"], owner_id["id"])
    fails(404, owner.task, allowed["id"])
    assert allowed["id"] not in ids(owner.tasks())
    assert owner.tasks_unread() == 1  # only the still-readable task counts


def test_thread_answer_posts_exact_options_and_has_durable_idempotence(env):
    _, _, owner = env.human("owner")
    _, _, writer = env.agent("writer", owner=owner)
    channel = owner.create_channel("decisions")
    root = writer.post(channel["id"], "What should happen?")
    item = writer.put_task(
        "decision", "Choose the release plan", channel_id=channel["id"], anchor_id=root["id"],
        sources=[root["id"]], multiple=True,
        options=[{"id": "ship", "label": "Ship the release."},
                 {"id": "notify", "label": "Notify the team."}])
    answer = dict(body="Schedule it for Monday.", option_ids=["ship", "notify"], request_id="answer-one")
    result = owner.task_action(item["id"], "answer", item["version"], **answer)
    message = result["message"]
    assert message["body"] == "Ship the release.\nNotify the team.\nSchedule it for Monday."
    assert message["sender"]["id"] == owner.me()["id"]
    assert message["channel_id"] == channel["id"]
    assert message["reply_to"] == message["thread_id"] == root["id"]
    assert (result["item"]["state"], result["item"]["closed_reason"]) == ("closed", "answered")
    assert owner.task_action(item["id"], "answer", item["version"], **answer) == result
    fails(409, owner.task_action, item["id"], "answer", item["version"],
          body="Different answer", request_id="answer-one")
    fails(409, owner.task_action, item["id"], "answer", item["version"],
          body="A second tab", request_id="another-tab")
    assert [m["id"] for m in owner.read(channel["id"])["messages"]] == [root["id"], message["id"]]
    env.op.update_channel(channel["id"], archived=True)
    replayed = owner.task_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] == message
    assert replayed["item"]["version"] == result["item"]["version"]


def test_answer_uses_owner_agent_dm_only_without_an_explicit_destination(env):
    _, _, owner = env.human("owner")
    writer_id, _, writer = env.agent("writer", owner=owner)
    item = writer.put_task("direct", "Need an answer")
    result = owner.task_action(item["id"], "answer", item["version"], body="Please proceed")
    dm = owner.open_dm(writer_id["id"])
    assert result["message"]["channel_id"] == dm["id"]
    assert result["message"]["reply_to"] is None
    assert [m["body"] for m in writer.read(dm["id"])["messages"]] == ["Please proceed"]

    channel = owner.create_channel("temporary")
    explicit = writer.put_task("explicit", "Answer there", channel_id=channel["id"])
    env.op.delete_channel(channel["id"], "temporary")
    error = fails(409, owner.task_action, explicit["id"], "answer", explicit["version"], body="Must not reach DM")
    assert error.code == "task_destination_unavailable"
    assert owner.task(explicit["id"])["state"] == "open"
    assert [m["body"] for m in owner.read(dm["id"])["messages"]] == ["Please proceed"]


def test_failed_answers_leave_the_item_open_and_post_nothing(env):
    _, _, owner = env.human("owner")
    _, _, writer = env.agent("writer", owner=owner)
    channel = owner.create_channel("decisions")
    item = writer.put_task("choice", "Choose one", channel_id=channel["id"],
                                 options=[{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}])
    fails(400, owner.task_action, item["id"], "answer", True, body="Invalid version")
    fails(400, owner.task_action, item["id"], "answer", item["version"], option_ids=["missing"])
    fails(400, owner.task_action, item["id"], "answer", item["version"], option_ids=["a", "b"])
    env.op.update_channel(channel["id"], archived=True)
    fails(409, owner.task_action, item["id"], "answer", item["version"], body="later")
    assert owner.task(item["id"])["state"] == "open"
    assert owner.read(channel["id"])["messages"] == []


def test_upserts_are_versioned_and_closed_items_require_explicit_restore(env):
    _, _, writer = env.agent("writer")
    item = writer.put_task("stable", "First", description="Original")
    assert writer.put_task("stable", "First", description="Original") == item
    changed = writer.put_task("stable", "Changed", base_version=item["version"])
    assert changed["id"] == item["id"] and changed["version"] > item["version"]
    assert writer.put_task("stable", "Changed", base_version=item["version"]) == changed
    error = fails(409, writer.put_task, "stable", "A stale edit", base_version=item["version"])
    assert error.code == "task_version_conflict"
    fails(409, env.op.task_action, item["id"], "done", item["version"])
    dismissed = env.op.task_action(item["id"], "dismiss", changed["version"])["item"]
    updated = writer.put_task("stable", "Updated while closed", base_version=dismissed["version"])
    assert (updated["state"], updated["closed_reason"]) == ("closed", "dismissed")
    assert ids(env.op.tasks()) == [] and ids(env.op.tasks(archived=True)) == [item["id"]]
    restored = env.op.task_action(item["id"], "restore", updated["version"])["item"]
    assert restored["state"] == "open" and restored["closed_reason"] is None
    assert env.op.tasks()["counts"] == {"open": 1, "archived": 0}
    dm = env.op.open_dm(writer.me()["id"])
    assert all(message["sender"]["id"] != env.op.me()["id"]
               for message in env.op.read(dm["id"])["messages"])


def test_manual_order_has_its_own_conflict_version_and_alternate_sorts(env):
    _, _, writer = env.agent("writer")
    a = writer.put_task("a", "Alpha")
    b = writer.put_task("b", "Beta")
    c = writer.put_task("c", "Gamma")
    original = env.op.tasks()
    env.op.move_task(c["id"], original["order_version"], before_id=a["id"])
    moved = env.op.tasks()
    assert ids(moved) == [c["id"], a["id"], b["id"]]
    assert moved["order_version"] != original["order_version"]
    assert env.op.task(c["id"])["version"] == c["version"]
    error = fails(409, env.op.move_task, b["id"], original["order_version"], before_id=c["id"])
    assert error.code == "task_order_conflict"
    assert ids(env.op.tasks(sort="latest")) == [a["id"], b["id"], c["id"]]
    assert ids(env.op.tasks(sort="latest", direction="desc")) == [c["id"], b["id"], a["id"]]
    assert ids(env.op.tasks()) == ids(moved)
    updated = writer.put_task("a", "Alpha revised", base_version=a["version"])
    assert env.op.tasks()["order_version"] == moved["order_version"]
    env.op.move_task(b["id"], moved["order_version"], after_id=c["id"])
    assert ids(env.op.tasks()) == [c["id"], b["id"], a["id"]]
    assert env.op.task(a["id"])["version"] == updated["version"]


def test_concurrent_answers_create_exactly_one_human_message(env):
    _, _, writer = env.agent("writer")
    channel = env.op.create_channel("race")
    item = writer.put_task("race", "One answer", channel_id=channel["id"])
    ready = threading.Barrier(2)

    def answer(number):
        with env.http(env.operator_token) as http:
            ready.wait(timeout=5)
            return http.post(f"/tasks/items/{item['id']}/actions",
                             headers={"Idempotency-Key": f"tab-{number}"},
                             json={"action": "answer", "version": item["version"], "body": f"answer {number}"})

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(answer, (1, 2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert [m["id"] for m in env.op.read(channel["id"])["messages"]] == [winner["message"]["id"]]
    assert env.op.task(item["id"])["closed_reason"] == "answered"


def test_deleted_messages_and_channels_leave_items_and_receipts_stable(env):
    _, _, writer = env.agent("writer")
    channel = env.op.create_channel("disposable")
    root = writer.post(channel["id"], "source body")
    item = writer.put_task("keep", "Keep the decision", channel_id=channel["id"],
                                 anchor_id=root["id"], sources=[root["id"]])
    answer = dict(body="Recorded answer", request_id="keep-receipt")
    receipt = env.op.task_action(item["id"], "answer", item["version"], **answer)
    env.op.delete_message(channel["id"], root["id"])
    current = env.op.task(item["id"])
    assert current["state"] == "closed" and "source body" not in json.dumps(current)
    assert current["sources"] == [{"message_id": root["id"], "channel_id": channel["id"],
                                    "message": None, "unavailable": True}]
    replayed = env.op.task_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] is None
    assert replayed["item"]["answer_message_id"] == receipt["message"]["id"]
    assert replayed["item"]["closed_reason"] == "answered"
    assert "Recorded answer" not in json.dumps(replayed)
    env.op.delete_channel(channel["id"], "disposable")
    assert env.op.task(item["id"])["id"] == item["id"]
    replayed = env.op.task_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] is None
    assert replayed["item"]["answer_message_id"] == receipt["message"]["id"]
    assert replayed["item"]["version"] == receipt["item"]["version"]


def test_restart_preserves_item_order_and_answer_receipts(env):
    writer_id, writer_token, writer = env.agent("writer")
    channel = env.op.create_channel("persistent")
    answered = writer.put_task("answered", "Answered", channel_id=channel["id"])
    first = writer.put_task("first", "First")
    second = writer.put_task("second", "Second")
    env.op.move_task(second["id"], env.op.tasks()["order_version"], before_id=first["id"])
    receipt = env.op.task_action(answered["id"], "answer", answered["version"],
                                  body="Durable answer", request_id="durable-answer")
    before = env.op.tasks()
    env.restart()
    writer = env.client(writer_token)
    assert writer.me()["id"] == writer_id["id"]
    assert env.op.tasks() == before
    assert env.op.task_action(answered["id"], "answer", answered["version"],
                               body="Durable answer", request_id="durable-answer") == receipt
    assert [m["id"] for m in env.op.read(channel["id"])["messages"]] == [receipt["message"]["id"]]


def test_tasks_changes_send_one_coalesced_durable_agent_dm(env):
    writer_id, writer_token, writer = env.agent("writer")
    dm = env.op.open_dm(writer_id["id"])
    first = writer.put_task("notice-1", "First request")
    writer.put_task("notice-2", "Second request")
    writer.put_task("notice-1", "First request revised", base_version=first["version"])
    env.restart()
    writer = env.client(writer_token)
    deadline = time.monotonic() + 10
    messages = []
    while time.monotonic() < deadline:
        messages = env.op.read(dm["id"], wait=1)["messages"]
        if messages:
            break
    assert len(messages) == 1
    assert messages[0]["sender"]["id"] == writer_id["id"]
    assert "#tasks" in messages[0]["body"]
    assert "3 changes" in messages[0]["body"]
    assert [m["id"] for m in writer.read(dm["id"])["messages"]] == [messages[0]["id"]]


def test_concurrent_source_changes_never_expose_private_card_content(env):
    public = env.op.create_channel("public-evidence")
    private = env.op.create_channel("private-evidence", private=True)
    writer_id, _, writer = env.agent("writer")
    _, _, reader = env.agent("reader", role="guest", channels=["public-evidence"])
    env.op.add_member(private["id"], writer_id["id"])
    marker = "PRIVATE TASKS CONTENT MUST STAY HIDDEN"
    private_source = writer.post(private["id"], marker)
    public_source = writer.post(public["id"], "Public evidence")
    items = [writer.put_task(f"changing-{number}", marker, sources=[private_source["id"]])
             for number in range(20)]
    assert reader.tasks()["items"] == []
    ready = threading.Barrier(2)

    def change_sources():
        ready.wait(timeout=5)
        for number in range(100):
            index = number % len(items)
            hidden = bool((number // len(items)) % 2)
            items[index] = writer.put_task(
                f"changing-{index}", marker if hidden else "Public request",
                sources=[private_source["id"] if hidden else public_source["id"]],
                base_version=items[index]["version"])

    reads = 0
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        changing = pool.submit(change_sources)
        ready.wait(timeout=5)
        while not changing.done() or reads < 10:
            assert marker not in json.dumps(reader.tasks())
            try:
                item = reader.task(items[reads % len(items)]["id"])
            except BoardError as exc:
                assert exc.status == 404
            else:
                assert marker not in json.dumps(item)
            reads += 1
        changing.result(timeout=10)
    assert set(ids(reader.tasks())) == {item["id"] for item in items}


def test_blocks_keep_text_and_quotes_in_order_and_register_sources(env):
    _, _, writer = env.agent("writer")
    channel = env.op.create_channel("evidence")
    first = writer.post(channel["id"], "First quoted message")
    second = writer.post(channel["id"], "Second quoted message")
    blocks = [{"type": "text", "text": "Context before the first quote."},
              {"type": "quote", "message_id": first["id"]},
              {"type": "text", "text": "An explanation between the quotes."},
              {"type": "quote", "message_id": second["id"]}]
    item = writer.put_task("interleaved", "Review both messages", blocks=blocks)
    assert item["blocks"] == blocks
    assert env.op.task(item["id"])["blocks"] == blocks
    assert [source["message_id"] for source in item["sources"]] == [first["id"], second["id"]]
    assert [source["message"]["body"] for source in item["sources"]] == [
        "First quoted message", "Second quoted message"]
    assert writer.put_task("interleaved", "Review both messages", blocks=blocks) == item
    revised = writer.put_task("interleaved", "Review both messages", sources=[second["id"]],
                               blocks=blocks, base_version=item["version"])
    assert [source["message_id"] for source in revised["sources"]] == [second["id"], first["id"]]
    assert revised["blocks"] == blocks
    env.op.delete_message(channel["id"], first["id"])
    current = env.op.task(item["id"])
    assert current["blocks"] == blocks
    assert current["sources"][1]["unavailable"] and current["sources"][1]["message"] is None
    assert "First quoted message" not in json.dumps(current)


def test_quote_blocks_cannot_bypass_proposer_owner_or_reader_permissions(env):
    _, _, owner = env.human("owner")
    writer_id, _, writer = env.agent("writer", owner=owner)
    _, _, narrow = env.agent("narrow", owner=owner, role="guest")
    owner_only = owner.create_channel("owner-only", private=True)
    source = owner.post(owner_only["id"], "Protected quote")
    quote = [{"type": "quote", "message_id": source["id"]}]
    fails(404, writer.put_task, "not-for-writer", "Review", blocks=quote)
    owner.add_member(owner_only["id"], writer_id["id"])
    item = writer.put_task("allowed", "Review", blocks=quote)
    assert item["sources"][0]["message"]["body"] == "Protected quote"
    fails(404, narrow.task, item["id"])
    assert item["id"] not in ids(narrow.tasks())

    agent_only = env.op.create_channel("agent-only", private=True)
    env.op.add_member(agent_only["id"], writer_id["id"])
    private = env.op.post(agent_only["id"], "Not for the owner")
    fails(404, writer.put_task, "not-for-owner", "Review",
          blocks=[{"type": "quote", "message_id": private["id"]}])
    assert ids(owner.tasks()) == [item["id"]]


def test_malformed_blocks_do_not_create_tasks(env):
    _, _, writer = env.agent("writer")
    for blocks in [
        "not a list", ["not a block"], [{"type": "html", "text": "<b>unsupported</b>"}],
        [{"type": "text", "text": 4}], [{"type": "text", "text": "valid", "extra": True}],
        [{"type": "quote", "message_id": True}], [{"type": "quote"}],
        [{"type": "text", "text": "a"}] * 41,
    ]:
        fails(400, writer.put_task, "invalid-blocks", "Invalid", blocks=blocks)
        assert env.op.tasks()["items"] == [], blocks


def test_block_text_and_description_share_the_size_limit(env):
    _, _, writer = env.agent("writer")
    blocks = [{"type": "text", "text": "x" * 100}] * 40
    item = writer.put_task("limit", "At the limit", description="d" * 12000, blocks=blocks)
    assert item["blocks"] == blocks and len(item["description"]) == 12000
    fails(400, writer.put_task, "too-long", "Too long", description="d" * 12001, blocks=blocks)
    assert ids(env.op.tasks()) == [item["id"]]


def test_v30_migration_preserves_items_receipts_order_and_pending_notice(tmp_path):
    data = tmp_path / "legacy-board"
    data.mkdir()
    path = data / "board.db"
    now = time.time()

    def legacy_hash(value):
        # v30's wire-content fingerprint, before blocks existed.
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()

    with closing(db.connect(path)) as conn:
        # Build an actual v30 database, rather than initializing the newest schema
        # and changing its version number.
        conn.execute("PRAGMA foreign_keys=OFF")
        for version, script in enumerate(db.MIGRATIONS[:30], start=1):
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
        conn.execute("PRAGMA foreign_keys=ON")
        assert db.schema_version(conn) == 30
        owner_id, owner_token = store.create_operator(conn, now)
        owner = store.authenticate(conn, owner_token, now)
        invite = store.create_invite(conn, owner, {"kind": "agent", "role": "member"}, now)
        joined = store.join(conn, invite["code"], "legacy-agent", None, now)
        agent_id, agent_token = joined["principal"]["id"], joined["token"]
        agent = store.authenticate(conn, agent_token, now)
        channel, _ = store.create_channel(conn, owner, "legacy-decisions", None, False, now)

        def message(sender, request_id, body, reply_to=None, channel_id=channel["id"]):
            payload, digest = store.normalize_payload(channel_id, {"body": body, "reply_to": reply_to})
            result, _ = store.post_message(conn, sender, channel_id, request_id, payload, digest, now)
            return result

        root = message(agent, "legacy-question", "Original source")
        answer = message(owner, f"#queue-answer:{owner_id}:legacy-answer", "Keep the original answer", root["id"])
        dm, _ = store.open_dm(conn, agent, owner_id, now)
        historical_notice = message(agent, "#queue-notice:1",
                                    "I updated your queue (1 change). [Open queue](#queue)", channel_id=dm["id"])
        contents = {}
        for item_id, key, title, rank in [(1, "legacy-answered", "Legacy answered", 8),
                                           (2, "legacy-open", "Legacy open", 2),
                                           (3, "legacy-last", "Legacy last", 5)]:
            content = {"title": title, "description": "Description from v30", "channel_id": None,
                       "anchor_id": None, "sources": [root["id"]], "options": [], "multiple": False}
            if item_id == 1:
                content.update(channel_id=channel["id"], anchor_id=root["id"])
            contents[item_id] = content
            closed = item_id == 1
            conn.execute("""INSERT INTO queue_items
                (id, owner_id, agent_id, item_key, title, description, channel_id, original_channel_id,
                 anchor_id, original_anchor_id, content_hash, version, state, closed_reason, closed_at,
                 answer_id, original_answer_id, created_at, updated_at, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (item_id, owner_id, agent_id, key, title, content["description"],
                          content["channel_id"], content["channel_id"], content["anchor_id"], content["anchor_id"],
                          legacy_hash(content), 4 if closed else 1, "closed" if closed else "open",
                          "answered" if closed else None, now if closed else None,
                          answer["id"] if closed else None, answer["id"] if closed else None,
                          now - 100, now, rank))
            conn.execute("""INSERT INTO queue_sources
                (item_id, position, message_id, original_message_id, channel_id, original_channel_id)
                VALUES (?, 0, ?, ?, ?, ?)""", (item_id, root["id"], root["id"], channel["id"], channel["id"]))
        conn.execute("INSERT INTO queue_orders(owner_id, version) VALUES (?, 7)", (owner_id,))
        payload = {"action": "answer", "version": 3, "body": "Keep the original answer", "option_ids": []}
        recorded = {"action": "answer", "version": 4, "state": "closed", "closed_reason": "answered",
                    "closed_at": now, "message_id": answer["id"]}
        conn.execute("""INSERT INTO queue_receipts
            (actor_id, request_key, item_id, payload_hash, result, created_at) VALUES (?, ?, 1, ?, ?, ?)""",
                     (owner_id, "legacy-answer", legacy_hash({"item_id": 1, "payload": payload}),
                      json.dumps(recorded), now))
        conn.execute("""INSERT INTO queue_notices
            (id, owner_id, agent_id, due_at, updated_at, changes, state, message_id)
            VALUES (1, ?, ?, ?, ?, 1, 'sent', ?)""",
                     (owner_id, agent_id, now - 10, now - 10, historical_notice["id"]))
        conn.execute("""INSERT INTO queue_notices(owner_id, agent_id, due_at, updated_at, changes)
                        VALUES (?, ?, ?, ?, 3)""", (owner_id, agent_id, now + 1, now))
        names = ("items", "sources", "orders", "receipts", "notices")
        before = {name: [dict(row) for row in conn.execute(f"SELECT * FROM queue_{name} ORDER BY rowid")]
                  for name in names}
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()

    assert cmd_init(Namespace(data_dir=str(data))) == 0
    with closing(db.connect(path)) as conn:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        for name in names:
            after = [dict(row) for row in conn.execute(f"SELECT * FROM task_{name} ORDER BY rowid")]
            assert [{key: row[key] for key in old} for row, old in zip(after, before[name])] == before[name]
            assert len(after) == len(before[name])
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'queue_%'").fetchall() == []

    with ThreadedServer(data) as server, Client(server.url, owner_token) as owner, Client(server.url, agent_token) as agent:
        page = owner.tasks()
        assert ids(page) == [2, 3] and page["order_version"] == 7
        assert page["counts"] == {"open": 2, "archived": 1}
        assert owner.tasks_unread() == 0  # migration does not flag the historical backlog
        assert ids(owner.tasks(archived=True)) == [2, 3, 1]
        assert owner.task(1)["sources"][0]["message"]["id"] == root["id"]
        assert owner.task(1)["answer"]["id"] == answer["id"]
        assert owner.task(1)["blocks"] == []
        retried = agent.put_task("legacy-open", **contents[2])
        assert retried["id"] == 2 and retried["version"] == 1
        assert agent.put_task("legacy-open", **contents[2], blocks=[])["version"] == 1
        receipt = owner.task_action(1, "answer", 3, body="Keep the original answer", request_id="legacy-answer")
        assert receipt["message"]["id"] == answer["id"] and receipt["item"]["version"] == 4
        assert [m["id"] for m in owner.read(channel["id"])["messages"]] == [root["id"], answer["id"]]
        dm = owner.open_dm(agent_id)
        assert owner.read(dm["id"])["messages"][0]["body"] == historical_notice["body"]
        deadline = time.monotonic() + 10
        notices = []
        while time.monotonic() < deadline:
            notices = owner.read(dm["id"], after=historical_notice["seq"], wait=1)["messages"]
            if notices:
                break
        assert len(notices) == 1
        assert notices[0]["sender"]["id"] == agent_id
        assert "3 changes" in notices[0]["body"] and "#tasks" in notices[0]["body"]


def test_task_attention_is_personal_durable_and_independent_of_completion(env):
    _, _, owner = env.human("attention-owner")
    _, _, other = env.human("attention-other", role="admin")
    _, _, agent = env.agent("attention-agent", owner=owner)
    _, _, other_agent = env.agent("other-agent", owner=other)
    first = agent.put_task("first", "First question")
    second = agent.put_task("second", "Second question")
    foreign = other_agent.put_task("foreign", "Someone else’s question")
    assert owner.tasks_unread() == agent.tasks_unread() == 2
    assert first["unread"] is True
    fails(403, agent.mark_tasks_read, [first["id"]])
    fails(404, other.mark_tasks_read, [first["id"]])
    # Bad IDs and mixed-ownership batches never partially acknowledge tasks.
    for bad in ([first["id"], foreign["id"]], [0], [True], ["1"], [first["id"]] * 201):
        fails(404 if foreign["id"] in bad and bad != [True] else 400, owner.mark_tasks_read, bad)
        assert owner.tasks_unread() == 2
    assert owner.mark_tasks_read([]) == 2
    assert owner.mark_tasks_read([first["id"], first["id"]]) == 1
    third = agent.put_task("new-arrival", "Arrived after the old list was read")
    assert owner.mark_tasks_read([first["id"]]) == 2
    assert owner.task(first["id"])["version"] == first["version"]
    assert owner.task(first["id"])["state"] == "open"
    updated = agent.put_task("first", "Updated first question", base_version=first["version"])
    assert updated["unread"] is False
    assert owner.tasks_unread() == 2  # only the second task and new arrival are unread
    agent.task_action(second["id"], "done", second["version"])
    assert owner.tasks_unread() == 1
    assert owner.mark_tasks_read([third["id"]]) == 0
    closed = owner.task(second["id"])
    owner.task_action(second["id"], "restore", closed["version"])
    assert owner.tasks_unread() == 0
    # Read state follows the account and survives a real server restart.
    token = owner.token
    env.restart()
    owner = env.client(token)
    assert owner.tasks_unread() == 0
    assert owner.task(first["id"])["unread"] is False
