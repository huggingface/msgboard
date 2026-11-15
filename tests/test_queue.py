"""Personal queues use real identities, ordinary messages and durable receipts."""

import concurrent.futures
import json
import threading
import time

import pytest

from msgboard.client import BoardError


def fails(status, fn, *args, **kwargs):
    with pytest.raises(BoardError) as exc:
        fn(*args, **kwargs)
    assert exc.value.status == status


def ids(page):
    return [item["id"] for item in page["items"]]


def test_queue_is_personal_and_only_writable_agents_propose(env):
    _, _, owner = env.human("owner")
    _, _, other = env.human("other", role="admin")
    _, _, writer = env.agent("writer", owner=owner)
    _, _, helper = env.agent("helper", owner=owner)
    _, _, stranger = env.agent("stranger", owner=other, role="admin")
    _, _, reader = env.agent("reader", role="reader")
    item = writer.put_queue_item("release.check", "Review release")
    assert ids(owner.queue()) == ids(helper.queue()) == [item["id"]]
    assert other.queue()["items"] == stranger.queue()["items"] == []
    for client in (env.op, other, stranger):
        fails(404, client.queue_item, item["id"])
        fails(404, client.queue_action, item["id"], "done", item["version"])
    for client in (owner, reader):
        fails(403, client.put_queue_item, "human-proposal", "Not allowed")
    fails(403, reader.queue_action, item["id"], "done", item["version"])
    fails(403, helper.queue_action, item["id"], "answer", item["version"], body="yes")
    closed = helper.queue_action(item["id"], "done", item["version"])["item"]
    assert (closed["state"], closed["closed_reason"]) == ("closed", "done")
    assert owner.queue()["counts"] == {"open": 0, "archived": 1}
    independent = helper.put_queue_item("release.check", "Another agent’s request")
    assert independent["id"] != item["id"]
    assert owner.queue()["counts"] == {"open": 1, "archived": 1}


def test_source_permissions_cover_proposer_owner_and_current_reader(env):
    owner_id, _, owner = env.human("owner")
    writer_id, _, writer = env.agent("writer", owner=owner)
    _, _, sibling = env.agent("sibling", owner=owner, role="guest")
    private = owner.create_channel("owner-secret", private=True)
    source = owner.post(private["id"], "private source body")
    fails(404, writer.put_queue_item, "not-readable", "Review", sources=[source["id"]])
    owner.add_member(private["id"], writer_id["id"])
    item = writer.put_queue_item("readable", "Review", sources=[source["id"]])
    assert "private source body" in json.dumps(owner.queue_item(item["id"]))
    fails(404, sibling.queue_item, item["id"])
    assert item["id"] not in ids(sibling.queue())

    agent_only = env.op.create_channel("agent-secret", private=True)
    env.op.add_member(agent_only["id"], writer_id["id"])
    secret = env.op.post(agent_only["id"], "not for the owner")
    fails(404, writer.put_queue_item, "owner-cannot-read", "Review", sources=[secret["id"]])
    env.op.add_member(agent_only["id"], owner_id["id"])
    allowed = writer.put_queue_item("owner-can-read", "Review", sources=[secret["id"]])
    env.op.remove_member(agent_only["id"], owner_id["id"])
    fails(404, owner.queue_item, allowed["id"])
    assert allowed["id"] not in ids(owner.queue())


def test_thread_answer_posts_exact_options_and_has_durable_idempotence(env):
    _, _, owner = env.human("owner")
    _, _, writer = env.agent("writer", owner=owner)
    channel = owner.create_channel("decisions")
    root = writer.post(channel["id"], "What should happen?")
    item = writer.put_queue_item(
        "decision", "Choose the release plan", channel_id=channel["id"], anchor_id=root["id"],
        sources=[root["id"]], multiple=True,
        options=[{"id": "ship", "label": "Ship the release."},
                 {"id": "notify", "label": "Notify the team."}])
    answer = dict(body="Schedule it for Monday.", option_ids=["ship", "notify"], request_id="answer-one")
    result = owner.queue_action(item["id"], "answer", item["version"], **answer)
    message = result["message"]
    assert message["body"] == "Ship the release.\nNotify the team.\nSchedule it for Monday."
    assert message["sender"]["id"] == owner.me()["id"]
    assert message["channel_id"] == channel["id"]
    assert message["reply_to"] == message["thread_id"] == root["id"]
    assert (result["item"]["state"], result["item"]["closed_reason"]) == ("closed", "answered")
    assert owner.queue_action(item["id"], "answer", item["version"], **answer) == result
    fails(409, owner.queue_action, item["id"], "answer", item["version"],
          body="Different answer", request_id="answer-one")
    fails(409, owner.queue_action, item["id"], "answer", item["version"],
          body="A second tab", request_id="another-tab")
    assert [m["id"] for m in owner.read(channel["id"])["messages"]] == [root["id"], message["id"]]
    env.op.update_channel(channel["id"], archived=True)
    replayed = owner.queue_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] == message
    assert replayed["item"]["version"] == result["item"]["version"]


def test_answer_uses_owner_agent_dm_only_without_an_explicit_destination(env):
    _, _, owner = env.human("owner")
    writer_id, _, writer = env.agent("writer", owner=owner)
    item = writer.put_queue_item("direct", "Need an answer")
    result = owner.queue_action(item["id"], "answer", item["version"], body="Please proceed")
    dm = owner.open_dm(writer_id["id"])
    assert result["message"]["channel_id"] == dm["id"]
    assert result["message"]["reply_to"] is None
    assert [m["body"] for m in writer.read(dm["id"])["messages"]] == ["Please proceed"]

    channel = owner.create_channel("temporary")
    explicit = writer.put_queue_item("explicit", "Answer there", channel_id=channel["id"])
    env.op.delete_channel(channel["id"], "temporary")
    fails(409, owner.queue_action, explicit["id"], "answer", explicit["version"], body="Must not reach DM")
    assert owner.queue_item(explicit["id"])["state"] == "open"
    assert [m["body"] for m in owner.read(dm["id"])["messages"]] == ["Please proceed"]


def test_failed_answers_leave_the_item_open_and_post_nothing(env):
    _, _, owner = env.human("owner")
    _, _, writer = env.agent("writer", owner=owner)
    channel = owner.create_channel("decisions")
    item = writer.put_queue_item("choice", "Choose one", channel_id=channel["id"],
                                 options=[{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}])
    fails(400, owner.queue_action, item["id"], "answer", True, body="Invalid version")
    fails(400, owner.queue_action, item["id"], "answer", item["version"], option_ids=["missing"])
    fails(400, owner.queue_action, item["id"], "answer", item["version"], option_ids=["a", "b"])
    env.op.update_channel(channel["id"], archived=True)
    fails(409, owner.queue_action, item["id"], "answer", item["version"], body="later")
    assert owner.queue_item(item["id"])["state"] == "open"
    assert owner.read(channel["id"])["messages"] == []


def test_upserts_are_versioned_and_closed_items_require_explicit_restore(env):
    _, _, writer = env.agent("writer")
    item = writer.put_queue_item("stable", "First", description="Original")
    assert writer.put_queue_item("stable", "First", description="Original") == item
    changed = writer.put_queue_item("stable", "Changed", base_version=item["version"])
    assert changed["id"] == item["id"] and changed["version"] > item["version"]
    assert writer.put_queue_item("stable", "Changed", base_version=item["version"]) == changed
    fails(409, writer.put_queue_item, "stable", "A stale edit", base_version=item["version"])
    fails(409, env.op.queue_action, item["id"], "done", item["version"])
    dismissed = env.op.queue_action(item["id"], "dismiss", changed["version"])["item"]
    updated = writer.put_queue_item("stable", "Updated while closed", base_version=dismissed["version"])
    assert (updated["state"], updated["closed_reason"]) == ("closed", "dismissed")
    assert ids(env.op.queue()) == [] and ids(env.op.queue(archived=True)) == [item["id"]]
    restored = env.op.queue_action(item["id"], "restore", updated["version"])["item"]
    assert restored["state"] == "open" and restored["closed_reason"] is None
    assert env.op.queue()["counts"] == {"open": 1, "archived": 0}
    dm = env.op.open_dm(writer.me()["id"])
    assert all(message["sender"]["id"] != env.op.me()["id"]
               for message in env.op.read(dm["id"])["messages"])


def test_manual_order_has_its_own_conflict_version_and_alternate_sorts(env):
    _, _, writer = env.agent("writer")
    a = writer.put_queue_item("a", "Alpha")
    b = writer.put_queue_item("b", "Beta")
    c = writer.put_queue_item("c", "Gamma")
    original = env.op.queue()
    env.op.move_queue_item(c["id"], original["order_version"], before_id=a["id"])
    moved = env.op.queue()
    assert ids(moved) == [c["id"], a["id"], b["id"]]
    assert moved["order_version"] != original["order_version"]
    assert env.op.queue_item(c["id"])["version"] == c["version"]
    fails(409, env.op.move_queue_item, b["id"], original["order_version"], before_id=c["id"])
    assert ids(env.op.queue(sort="latest")) == [a["id"], b["id"], c["id"]]
    assert ids(env.op.queue(sort="latest", direction="desc")) == [c["id"], b["id"], a["id"]]
    assert ids(env.op.queue()) == ids(moved)
    updated = writer.put_queue_item("a", "Alpha revised", base_version=a["version"])
    assert env.op.queue()["order_version"] == moved["order_version"]
    env.op.move_queue_item(b["id"], moved["order_version"], after_id=c["id"])
    assert ids(env.op.queue()) == [c["id"], b["id"], a["id"]]
    assert env.op.queue_item(a["id"])["version"] == updated["version"]


def test_concurrent_answers_create_exactly_one_human_message(env):
    _, _, writer = env.agent("writer")
    channel = env.op.create_channel("race")
    item = writer.put_queue_item("race", "One answer", channel_id=channel["id"])
    ready = threading.Barrier(2)

    def answer(number):
        with env.http(env.operator_token) as http:
            ready.wait(timeout=5)
            return http.post(f"/queue/items/{item['id']}/actions",
                             headers={"Idempotency-Key": f"tab-{number}"},
                             json={"action": "answer", "version": item["version"], "body": f"answer {number}"})

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(answer, (1, 2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert [m["id"] for m in env.op.read(channel["id"])["messages"]] == [winner["message"]["id"]]
    assert env.op.queue_item(item["id"])["closed_reason"] == "answered"


def test_deleted_messages_and_channels_leave_items_and_receipts_stable(env):
    _, _, writer = env.agent("writer")
    channel = env.op.create_channel("disposable")
    root = writer.post(channel["id"], "source body")
    item = writer.put_queue_item("keep", "Keep the decision", channel_id=channel["id"],
                                 anchor_id=root["id"], sources=[root["id"]])
    answer = dict(body="Recorded answer", request_id="keep-receipt")
    receipt = env.op.queue_action(item["id"], "answer", item["version"], **answer)
    env.op.delete_message(channel["id"], root["id"])
    current = env.op.queue_item(item["id"])
    assert current["state"] == "closed" and "source body" not in json.dumps(current)
    assert current["sources"] == [{"message_id": root["id"], "channel_id": channel["id"],
                                    "message": None, "unavailable": True}]
    replayed = env.op.queue_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] is None
    assert replayed["item"]["answer_message_id"] == receipt["message"]["id"]
    assert replayed["item"]["closed_reason"] == "answered"
    assert "Recorded answer" not in json.dumps(replayed)
    env.op.delete_channel(channel["id"], "disposable")
    assert env.op.queue_item(item["id"])["id"] == item["id"]
    replayed = env.op.queue_action(item["id"], "answer", item["version"], **answer)
    assert replayed["message"] is None
    assert replayed["item"]["answer_message_id"] == receipt["message"]["id"]
    assert replayed["item"]["version"] == receipt["item"]["version"]


def test_restart_preserves_item_order_and_answer_receipts(env):
    writer_id, writer_token, writer = env.agent("writer")
    channel = env.op.create_channel("persistent")
    answered = writer.put_queue_item("answered", "Answered", channel_id=channel["id"])
    first = writer.put_queue_item("first", "First")
    second = writer.put_queue_item("second", "Second")
    env.op.move_queue_item(second["id"], env.op.queue()["order_version"], before_id=first["id"])
    receipt = env.op.queue_action(answered["id"], "answer", answered["version"],
                                  body="Durable answer", request_id="durable-answer")
    before = env.op.queue()
    env.restart()
    writer = env.client(writer_token)
    assert writer.me()["id"] == writer_id["id"]
    assert env.op.queue() == before
    assert env.op.queue_action(answered["id"], "answer", answered["version"],
                               body="Durable answer", request_id="durable-answer") == receipt
    assert [m["id"] for m in env.op.read(channel["id"])["messages"]] == [receipt["message"]["id"]]


def test_queue_changes_send_one_coalesced_durable_agent_dm(env):
    writer_id, writer_token, writer = env.agent("writer")
    dm = env.op.open_dm(writer_id["id"])
    first = writer.put_queue_item("notice-1", "First request")
    writer.put_queue_item("notice-2", "Second request")
    writer.put_queue_item("notice-1", "First request revised", base_version=first["version"])
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
    assert "#queue" in messages[0]["body"]
    assert "3 changes" in messages[0]["body"]
    assert [m["id"] for m in writer.read(dm["id"])["messages"]] == [messages[0]["id"]]


def test_concurrent_source_changes_never_expose_private_card_content(env):
    public = env.op.create_channel("public-evidence")
    private = env.op.create_channel("private-evidence", private=True)
    writer_id, _, writer = env.agent("writer")
    _, _, reader = env.agent("reader", role="guest", channels=["public-evidence"])
    env.op.add_member(private["id"], writer_id["id"])
    marker = "PRIVATE QUEUE CONTENT MUST STAY HIDDEN"
    private_source = writer.post(private["id"], marker)
    public_source = writer.post(public["id"], "Public evidence")
    items = [writer.put_queue_item(f"changing-{number}", marker, sources=[private_source["id"]])
             for number in range(20)]
    assert reader.queue()["items"] == []
    ready = threading.Barrier(2)

    def change_sources():
        ready.wait(timeout=5)
        for number in range(100):
            index = number % len(items)
            hidden = bool((number // len(items)) % 2)
            items[index] = writer.put_queue_item(
                f"changing-{index}", marker if hidden else "Public request",
                sources=[private_source["id"] if hidden else public_source["id"]],
                base_version=items[index]["version"])

    reads = 0
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        changing = pool.submit(change_sources)
        ready.wait(timeout=5)
        while not changing.done() or reads < 10:
            assert marker not in json.dumps(reader.queue())
            try:
                item = reader.queue_item(items[reads % len(items)]["id"])
            except BoardError as exc:
                assert exc.status == 404
            else:
                assert marker not in json.dumps(item)
            reads += 1
        changing.result(timeout=10)
    assert set(ids(reader.queue())) == {item["id"] for item in items}
