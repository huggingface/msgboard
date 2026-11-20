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
    # opening the channel reads the mention, but the reply in my thread waits until I open the thread
    assert (after["unread"], after["for_me"], after["threads_for_me"]) == (0, 1, 1)
    assert me.mark_read(gen["id"], 1)["last_read"] == c["last_seq"]   # read position never moves back
    assert [m["unread"] for m in me.activity()] == [True, False, True]
    summary = [m for m in me.read(gen["id"])["messages"] if m["id"] == root["id"]][0]["replies"]
    assert summary["for_me_unread"] == 1
    done = me.mark_thread_read(gen["id"], root["id"], c["last_seq"])
    assert (done["for_me"], done["threads_for_me"]) == (0, 0)
    assert [m["unread"] for m in me.activity()] == [True, False, False]
    assert [m for m in me.read(gen["id"])["messages"] if m["id"] == root["id"]][0]["replies"]["for_me_unread"] == 0


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


def test_group_mentions(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    bob_ident, _, bob = env.human("bob")
    env.op.add_member(gen["id"], bob_ident["id"])
    _, _, bot = env.agent("bot", channels=["general"])
    _, _, outsider = env.agent("guest", role="guest")           # cannot see #general
    ids = lambda m: sorted(x["external_id"] for x in m["mentions"])
    assert ids(ana.post(gen["id"], "@all standup in 5")) == ["bob", "bot", "operator"]
    assert ids(ana.post(gen["id"], "@agents please rerun")) == ["bot"]
    assert ids(ana.post(gen["id"], "hey @humans, review?")) == ["bob", "operator"]
    assert ids(ana.post(gen["id"], "mail all@example.com or @allison")) == []
    assert [m["reason"] for m in bot.activity_page(0)["messages"]] == ["mention", "mention"]
    assert outsider.activity_page(0)["messages"] == []


def test_group_mentions_reach_only_channel_members(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    ana.remove_member(gen["id"], ana.identity["id"])              # readable, but explicitly left
    _, _, bot = env.agent("bot", channels=["general"])
    _, _, other = env.agent("other-bot")                         # can read #general but has not joined it
    ids = lambda m: sorted(x["external_id"] for x in m["mentions"])
    assert ids(env.op.post(gen["id"], "@all standup")) == ["bot"]
    assert ids(env.op.post(gen["id"], "@agents go")) == ["bot"]
    assert ids(env.op.post(gen["id"], "@humans look")) == []
    assert other.activity_page(0)["messages"] == [] and ana.activity_page(0)["messages"] == []
    assert ids(env.op.post(gen["id"], "@other-bot and @agents"))  == ["bot", "other-bot"]   # by name still works
    # @myagents uses the author's ownership, with the same channel-member boundary.
    owned, _, mine = env.agent("ana-bot", owner=ana, channels=["general"])
    _, _, outside = env.agent("ana-outside", owner=ana)
    inactive, _, _ = env.agent("ana-disabled", owner=ana, channels=["general"])
    env.op.update_identity(inactive["id"], disabled=True)
    message = ana.post(gen["id"], "@MYAGENTS please check", request_id="owned-group")
    assert ids(message) == ["ana-bot"] and message["mentions"][0]["as"] == "myagents"
    assert mine.activity_page(0)["messages"][0]["reason"] == "mention"
    assert outside.activity_page(0)["messages"] == []
    assert ids(env.op.post(gen["id"], "@myagents operator's agents")) == ["bot"]
    assert ids(mine.post(gen["id"], "@myagents does not mean my owner's agents")) == []
    mixed = ana.post(gen["id"], "@myagents and @agents together")
    assert ids(mixed) == ["ana-bot", "bot"]
    assert {m["external_id"]: m["as"] for m in mixed["mentions"]} == {"ana-bot": "myagents", "bot": "agents"}
    assert ids(ana.post(gen["id"], "@ana-bot and @myagents deduplicate")) == ["ana-bot"]
    assert ids(ana.post(gen["id"], "email a@myagents.test /@myagents @myagents_extra @myagents-extra")) == []
    ana.add_member(gen["id"], outside.identity["id"])
    assert ana.post(gen["id"], "@MYAGENTS please check", request_id="owned-group")["mentions"] == message["mentions"]
    assert ids(ana.post(gen["id"], "@myagents newly joined")) == ["ana-bot", "ana-outside"]
    # Private-channel access is still required; edits recalculate from the same author.
    secret = ana.create_channel("owned-secret", private=True)
    ana.add_member(secret["id"], owned["id"])
    original = ana.post(secret["id"], "A question")
    edited = ana.edit(secret["id"], original["id"], "@myagents private question")
    assert ids(edited) == ["ana-bot"]
    assert all(m["channel_id"] != secret["id"] for m in outside.activity_page(0)["messages"])


def test_group_words_are_not_personal_names(env):
    gen = env.op.create_channel("general")
    _, _, bot = env.agent("bot", channels=["general"])
    named, _, agents = env.agent("agents-bot", display_name="Agents")       # not a member
    _, _, humans = env.agent("humans-bot", display_name="humans")           # not a member, and not a person
    mine_named, _, myagents = env.agent("myagents-bot", display_name="MyAgents")
    ids = lambda m: sorted(x["external_id"] for x in m["mentions"])
    assert ids(env.op.post(gen["id"], "@agents go")) == ["bot"]
    assert ids(env.op.post(gen["id"], "@humans look")) == []
    assert ids(env.op.post(gen["id"], "@myagents look")) == ["bot"]
    env.op.update_identity(mine_named["id"], display_name="Renamed Agent")
    assert ids(env.op.post(gen["id"], "@MyAgents again")) == ["bot"]
    assert myagents.activity_page(0)["messages"] == []
    env.op.update_identity(named["id"], display_name="Agent Smith")        # a former name is no way in either
    assert ids(env.op.post(gen["id"], "@Agents again")) == ["bot"]
    assert agents.activity_page(0)["messages"] == [] and humans.activity_page(0)["messages"] == []
    assert ids(env.op.post(gen["id"], "@Agent Smith hi")) == ["agents-bot"]  # their other names still work


def test_invite_can_join_all_open_channels(env):
    a = env.op.create_channel("alpha"); b = env.op.create_channel("beta")
    old = env.op.create_channel("old"); env.op.update_channel(old["id"], archived=True)
    _, _, ana = env.human("ana")
    ana.create_channel("secret", private=True)
    inv = env.op.create_invite("agent", role="guest", join_open_channels=True)
    bot = Client.join(env.url, inv["code"], "roamer")
    assert sorted(c["name"] for c in bot.list_channels()) == ["alpha", "beta"]   # not archived, not private
    plain = Client.join(env.url, env.op.create_invite("agent", role="guest")["code"], "homebody")
    assert plain.list_channels() == []


def test_mentions_survive_renames(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    bot_ident, _, bot = env.agent("leandros-intern", display_name="Leandros Intern", channels=["general"])
    old = ana.post(gen["id"], "thanks @Leandros Intern!")
    assert [(m["external_id"], m["as"]) for m in old["mentions"]] == [("leandros-intern", "Leandros Intern")]
    env.op.update_identity(bot_ident["id"], display_name="leandro's intern")
    again = ana.read(gen["id"])["messages"][0]["mentions"]
    assert [(m["display_name"], m["as"]) for m in again] == [("leandro's intern", "Leandros Intern")]  # still linked
    new = ana.post(gen["id"], "@Leandros Intern and @leandro's intern")   # the former name still reaches them
    assert [m["external_id"] for m in new["mentions"]] == ["leandros-intern"]
    grp = ana.post(gen["id"], "@agents go")
    assert grp["mentions"][0]["as"] == "agents"



def test_upgrade_does_not_resurface_old_thread_notifications(env):
    import sqlite3
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    me_ident, _, me = env.human("me")
    root = me.post(gen["id"], "question")
    ana.post(gen["id"], "old answer", reply_to=root["id"])
    me.mark_read(gen["id"], 2)
    # as if this board had been read before threads had their own read state
    env.close()
    db = sqlite3.connect(env.data_dir / "board.db")
    db.execute("UPDATE channel_reads SET thread_floor = last_read_seq"); db.commit(); db.close()
    env.restart()
    me = env.client(me.token)
    assert by_name(me, "general")["for_me"] == 0                       # the old reply stays read
    ana2 = env.client(ana.token)
    ana2.post(gen["id"], "new answer", reply_to=root["id"])
    assert by_name(me, "general")["for_me"] == 1                       # a new one is for me until the thread is read
