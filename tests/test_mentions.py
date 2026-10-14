from __future__ import annotations

from msgboard.client import Client


def by_name(client, name):
    return [c for c in client.list_channels() if c["name"] == name][0]


def test_mentions_are_recorded_only_for_people_who_can_see(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    secret = ana.create_channel("secret", private=True)
    msg = ana.post(gen["id"], "@bob and @hf-nobody, also @operator: please look. Mail me at x@bob.com")
    assert sorted(m["external_id"] for m in msg["mentions"]) == ["bob", "operator"]
    assert [m["mentions"] for m in bob.read(gen["id"])["messages"]][0] == msg["mentions"]
    hidden = ana.post(secret["id"], "@bob you cannot see this")
    assert hidden["mentions"] == []                    # bob is not in the private channel
    assert all(m["channel"]["name"] != "secret" for m in bob.activity())


def test_unread_and_for_me_counts(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    me_ident, _, me = env.human("me")
    assert by_name(me, "general")["unread"] == 0
    ana.post(gen["id"], "just chatting")                     # unread, not for me
    root = me.post(gen["id"], "my question")                 # my own messages never count
    ana.post(gen["id"], "an answer", reply_to=root["id"])    # reply in my thread: for me
    ana.post(gen["id"], "hey @me look")                      # mention: for me
    c = by_name(me, "general")
    assert (c["unread"], c["for_me"]) == (2, 2)              # unread counts top-level messages only
    dm = ana.open_dm(me_ident["id"])
    ana.post(dm["id"], "psst")
    assert by_name(me, dm["name"])["for_me"] == 1            # every DM to me is for me
    reasons = [(m["reason"], m["unread"]) for m in me.activity()]
    assert reasons == [("dm", True), ("mention", True), ("reply", True)]
    after = me.mark_read(gen["id"], c["last_seq"])
    assert (after["unread"], after["for_me"]) == (0, 0)
    assert me.mark_read(gen["id"], 1)["last_read"] == c["last_seq"]   # read position never moves back
    assert [m["unread"] for m in me.activity()] == [True, False, False]


def test_read_position_follows_you_across_devices(env):
    gen = env.op.create_channel("general")
    ident, _, laptop = env.human("ana")
    env.op.post(gen["id"], "news")
    link = laptop._request("POST", f"/principals/{ident['id']}/signin-link").json()["code"]
    phone = Client.join(env.url, link, None)
    assert by_name(phone, "general")["unread"] == 1
    laptop.mark_read(gen["id"], 1)
    assert by_name(phone, "general")["unread"] == 0
    phone.close()


def test_new_people_do_not_inherit_old_unread(env):
    gen = env.op.create_channel("general")
    for i in range(5):
        env.op.post(gen["id"], f"old {i}")
    _, _, late = env.human("late")
    assert by_name(late, "general")["unread"] == 0


def test_display_names_with_spaces(env):
    gen = env.op.create_channel("general")
    loubna, _, _ = env.human("loubna", display_name="Loubna Ben Allal")
    lewis, _, _ = env.human("lewis", display_name="Lewis Tunstall")
    lou, _, _ = env.human("lou", display_name="Loubna")          # a shorter name that is a prefix
    _, _, cm = env.human("cm")
    msg = cm.post(gen["id"], "Can I tag people @Leandro @Loubna Ben Allal @Lewis Tunstall? and @loubna, @LOU!")
    got = sorted(m["external_id"] for m in msg["mentions"])
    assert got == ["lewis", "lou", "loubna"]                      # longest match wins; usernames work too
    assert all(m["id"] != lou["id"] for m in cm.post(gen["id"], "@Loubna Ben Allal only")["mentions"])
    assert cm.post(gen["id"], "no match: @Loubnaxyz or email a@lewis.org")["mentions"] == []


def test_older_messages_get_mentions_after_upgrade(env):
    gen = env.op.create_channel("general")
    _, _, _ = env.human("loubna", display_name="Loubna Ben Allal")
    _, _, cm = env.human("cm")
    msg = cm.post(gen["id"], "hello @Loubna Ben Allal")
    import sqlite3
    conn = sqlite3.connect(env.data_dir / "board.db")
    conn.execute("DELETE FROM message_mentions"); conn.execute("DELETE FROM settings WHERE key = 'mentions_v2'"); conn.commit(); conn.close()
    env.restart()
    assert [m["external_id"] for m in env.op.read(gen["id"])["messages"][0]["mentions"]] == ["loubna"]
