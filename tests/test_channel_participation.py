"""Posting joins the author; individual mentions can reach people outside the channel."""

import pytest


def members(client, channel):
    return client._request("GET", f"/channels/{channel}/members").json()["members"]


def candidates(client, channel):
    response = client._request("GET", f"/channels/{channel}/mention-candidates")
    return {p["external_id"]: p for p in response.json()["candidates"]}


@pytest.mark.parametrize("kind", ["human", "agent"])
def test_posting_joins_author_but_replay_does_not_rejoin(env, kind):
    channel = env.op.create_channel("work")["id"]
    root = env.op.post(channel, "question")
    identity, _, writer = getattr(env, kind)("writer")
    assert identity["id"] not in {p["id"] for p in members(env.op, channel)}

    first = writer.post(channel, "answer", reply_to=root["id"], request_id="answer")
    added = next(p for p in members(env.op, channel) if p["id"] == identity["id"])
    assert added["added_by"] == identity["id"] and added["added_at"] == first["created_at"]
    assert next(c for c in writer.list_channels() if c["id"] == channel)["member"]
    writer.post(channel, "another answer")
    again = next(p for p in members(env.op, channel) if p["id"] == identity["id"])
    assert (again["added_at"], again["added_by"]) == (added["added_at"], added["added_by"])
    group = env.op.post(channel, "@all thanks")
    assert identity["id"] in {p["id"] for p in group["mentions"]}
    assert group["id"] in {m["id"] for m in writer.activity_page(0, channels="joined")["messages"]}

    writer.remove_member(channel, identity["id"])
    assert writer.post(channel, "answer", reply_to=root["id"], request_id="answer")["id"] == first["id"]
    assert identity["id"] not in {p["id"] for p in members(env.op, channel)}
    writer.post(channel, "a new post joins again")
    assert identity["id"] in {p["id"] for p in members(env.op, channel)}


def test_failed_posts_do_not_join_or_grant_access(env):
    work = env.op.create_channel("work")["id"]
    secret = env.op.create_channel("secret", private=True)["id"]
    archived = env.op.create_channel("archived")["id"]
    env.op.update_channel(archived, archived=True)
    other = env.op.create_channel("other")["id"]
    who, token, writer = env.human("writer")
    _, viewer_token, _ = env.human("viewer", role="viewer")
    _, guest_token, _ = env.agent("guest", role="guest")
    writer.post(other, "original", request_id="reused")
    cases = [
        (token, work, {"body": "bad reply", "reply_to": 99999}, "bad-reply", 400),
        (token, work, {"body": "bad file", "attachments": ["missing"]}, "bad-file", 400),
        (token, work, {"body": "different"}, "reused", 409),
        (token, secret, {"body": "no access"}, "private", 404),
        (token, archived, {"body": "closed"}, "archived", 409),
        (viewer_token, work, {"body": "read only"}, "viewer", 403),
        (guest_token, work, {"body": "no access"}, "guest", 404),
    ]
    before = {c: members(env.op, c) for c in (work, secret, archived)}
    for credential, channel, body, key, status in cases:
        with env.http(credential) as http:
            response = http.post(f"/channels/{channel}/messages", json=body, headers={"Idempotency-Key": key})
        assert response.status_code == status, response.text
    assert {c: members(env.op, c) for c in before} == before
    assert who["id"] not in {p["id"] for p in members(env.op, work)}


def test_candidates_include_readable_nonmember_people_and_mentions_notify(env):
    channel = env.op.create_channel("work")["id"]
    _, _, sender = env.human("sender")
    person, _, recipient = env.human("person", display_name="Person A")
    _, _, _ = env.human("reader", role="viewer")
    _, _, _ = env.human("guest-person", role="guest")
    _, _, _ = env.agent("member-bot", channels=["work"])
    _, _, _ = env.agent("outside-bot")
    disabled, _, _ = env.human("disabled-person")
    env.op.update_identity(disabled["id"], disabled=True)

    got = candidates(sender, channel)
    assert {"operator", "member-bot", "person", "reader"} <= set(got)
    assert not ({"sender", "guest-person", "outside-bot", "disabled-person"} & set(got))
    assert got["member-bot"]["member"] and not got["person"]["member"]
    assert "hf_username" not in got["person"]                  # same directory privacy as /principals
    message = sender.post(channel, "@Person A please look")
    assert [m["id"] for m in message["mentions"]] == [person["id"]]
    activity = recipient.activity_page(0)["messages"]
    assert [(m["id"], m["reason"]) for m in activity] == [(message["id"], "mention")]
    assert person["id"] not in {p["id"] for p in members(env.op, channel)}
    assert person["id"] not in {m["id"] for m in sender.post(channel, "@humans joined only")["mentions"]}


def test_candidates_preserve_private_dm_and_guest_directory_boundaries(env):
    _, _, owner = env.human("owner")
    hidden = owner.create_channel("hidden", private=True)["id"]
    friend, _, _ = env.human("friend")
    admin, admin_token, administrator = env.human("another-admin", role="admin")
    _, outsider_token, outsider = env.human("outsider")
    guest_id, guest_token, guest = env.agent("guest", role="guest")
    owner.add_member(hidden, friend["id"])
    owner.add_member(hidden, guest_id["id"])

    got = candidates(owner, hidden)
    assert {"friend", "guest", "operator", "another-admin"} <= set(got)
    assert "outsider" not in got
    assert not got["another-admin"]["member"]  # an admin can read private channels
    # Guests must not gain directory access to people merely because those people are admins.
    assert "another-admin" not in candidates(guest, hidden)
    with env.http(outsider_token) as http:
        assert http.get(f"/channels/{hidden}/mention-candidates").status_code == 404

    dm = owner.open_dm(friend["id"])["id"]
    assert set(candidates(owner, dm)) == {"friend"}
    assert set(candidates(env.op, dm)) == {"owner", "friend"}  # observing admin is not added to the DM
    with env.http(guest_token) as http:
        assert http.get(f"/channels/{dm}/mention-candidates").status_code == 404
    with env.http(outsider_token) as http:
        assert http.get(f"/channels/{dm}/mention-candidates").status_code == 404
    with env.http(admin_token) as http:
        assert http.post(f"/channels/{dm}/messages", json={"body": "observer cannot participate"},
                         headers={"Idempotency-Key": "dm-observer"}).status_code == 403
    assert {p["id"] for p in members(env.op, dm)} == {owner.identity["id"], friend["id"]}
    administrator.post(hidden, "an admin may participate in this private channel")
    assert admin["id"] in {p["id"] for p in members(env.op, hidden)}
