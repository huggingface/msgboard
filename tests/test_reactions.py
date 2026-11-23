from __future__ import annotations

import pytest

from msgboard.client import BoardError


def test_reactions_roundtrip(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    _, _, bot = env.agent("bot", channels=["general"])
    msg = ana.post(gen["id"], "lunch: pizza or sushi?")
    assert msg["reactions"] == []
    bob.react(gen["id"], msg["id"], "🍕")
    bob.react(gen["id"], msg["id"], "🍕")                      # idempotent
    bot.react(gen["id"], msg["id"], "🍕")
    out = ana.react(gen["id"], msg["id"], "🍣")
    assert [(r["emoji"], r["count"], r["me"]) for r in out["reactions"]] == [("🍕", 2, False), ("🍣", 1, True)]
    assert [b["external_id"] for b in out["reactions"][0]["by"]] == ["bob", "bot"]
    seen_by_bob = bob.read(gen["id"])["messages"][0]["reactions"]
    assert [(r["emoji"], r["me"]) for r in seen_by_bob] == [("🍕", True), ("🍣", False)]
    bob.unreact(gen["id"], msg["id"], "🍕"); bob.unreact(gen["id"], msg["id"], "🍕")
    assert [(r["emoji"], r["count"]) for r in ana.read(gen["id"])["messages"][0]["reactions"]] == [("🍕", 1), ("🍣", 1)]
    assert ana.read(gen["id"])["messages"][0]["edited_at"] is None   # a reaction is not an edit
    for bad in ["thumbsup", ":+1:", "a b", ""]:
        with pytest.raises(BoardError) as e:
            ana.react(gen["id"], msg["id"], bad)
        assert e.value.status in (400, 404, 405)


def test_reactions_need_write_access_and_reach_followers(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, viewer = env.human("vic", role="viewer")
    msg = ana.post(gen["id"], "ship it?")
    with pytest.raises(BoardError) as e:
        viewer.react(gen["id"], msg["id"], "👍")
    assert e.value.status == 403
    http = env.http(ana.token)
    start = ana.list_channels()[0]["edit_rev"]
    env.op.react(gen["id"], msg["id"], "✅")
    page = http.get(f"/channels/{gen['id']}/messages", params={"after": msg["seq"], "edits_after": start}).json()
    assert [r["emoji"] for r in page["edits"][0]["reactions"]] == ["✅"]


def test_reactions_to_my_messages_show_in_activity(env):
    gen = env.op.create_channel("general")
    _, _, ana = env.human("ana")
    _, _, bob = env.human("bob")
    _, _, bot = env.agent("bot", channels=["general"])
    mine = ana.post(gen["id"], "proposal: ship friday")
    other = bob.post(gen["id"], "not ana's")
    bob.react(gen["id"], mine["id"], "👍"); bot.react(gen["id"], mine["id"], "✅")
    ana.react(gen["id"], mine["id"], "🎉")          # my own reaction is not activity
    bob.react(gen["id"], other["id"], "👀")         # not my message
    r = env.http(ana.token).get("/activity", params={"reactions": "true"}).json()["reactions"]
    assert len(r) == 1 and r[0]["message"]["body"] == "proposal: ship friday"
    assert [(x["emoji"], x["by"]["external_id"]) for x in r[0]["reactions"]] == [("✅", "bot"), ("👍", "bob")]
    assert "reactions" not in env.http(ana.token).get("/activity").json()


def test_reaction_picker_on_a_phone(env, tmp_path):
    """On a phone the picker must stay open until an emoji is chosen: opening it pops no keyboard,
    and the keyboard (which shrinks the window) or a scroll never closes it."""
    import json
    import os
    pw = pytest.importorskip("playwright.sync_api")
    _, token, ana = env.human("ana")
    chan = ana.create_channel("phone")
    msg = ana.post(chan["id"], "react to me")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        ctx.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        ctx.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = ctx.new_page()
        page.goto(env.url + "/#phone")
        pop = page.get_by_role("dialog", name="Add reaction")
        page.locator(f'#messages [data-mid="{msg["id"]}"] .actions [data-react-pick]').tap()
        pw.expect(pop).to_be_visible()
        assert not page.evaluate("document.activeElement.matches('.react-pop input')")   # no keyboard on open
        page.set_viewport_size({"width": 390, "height": 200})     # the keyboard opens anyway (search tapped)
        pop.locator("input").tap()
        pop.locator("input").fill("heart")
        pw.expect(pop).to_be_visible()
        box = pop.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 200   # it shrinks to fit; the grid scrolls
        page.screenshot(path=str(tmp_path / "react-phone.png"))
        last = pop.locator(".emoji-grid [data-e]").last
        emoji = last.get_attribute("data-e")
        last.tap()                                                # reachable: scrolled into view in the grid
        pw.expect(pop).not_to_be_visible()
        pw.expect(page.locator(f'#messages [data-mid="{msg["id"]}"] .rchip[data-react="{emoji}"]')).to_be_visible()
        assert [r["emoji"] for r in ana.read(chan["id"])["messages"][0]["reactions"]] == [emoji]
        # the quick row works with one tap too; turning the phone (a width change) still closes it
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(f'#messages [data-mid="{msg["id"]}"] .actions [data-react-pick]').tap()
        pop.locator(".rquick [data-e]").first.tap()
        pw.expect(pop).not_to_be_visible()
        pw.expect(page.locator(f'#messages [data-mid="{msg["id"]}"] .rchip[data-react]')).to_have_count(2)
        assert len(ana.read(chan["id"])["messages"][0]["reactions"]) == 2
        page.locator(f'#messages [data-mid="{msg["id"]}"] .actions [data-react-pick]').tap()
        page.set_viewport_size({"width": 844, "height": 390})
        pw.expect(pop).not_to_be_visible()
        browser.close()
