"""Mention ownership labels and ordering on a real board, across the shared composers."""

import json
import os

import pytest


def test_myagents_suggestions_labels_and_recipient_highlights(env, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    _, token, owner = env.human("person-a", display_name="Person A")
    _, _, other_owner = env.human("person-b", display_name="Person B")
    channel = owner.create_channel("project")
    cid = channel["id"]
    _, mine_token, mine = env.agent("omega-helper", owner=owner, display_name="Omega Helper", channels=["project"])
    named, _, _ = env.agent("zeta-helper", owner=owner, display_name="Zeta Helper", channels=["project"])
    _, other_token, _ = env.agent("alpha-helper", owner=other_owner, display_name="Alpha Helper", channels=["project"])
    for i in range(5):
        env.agent("other-" + str(i), owner=other_owner, channels=["project"])
    person, _, _ = env.human("helper", display_name="Helper")
    owner.add_member(cid, person["id"])
    env.agent("outside-helper", owner=owner)
    errors = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        def context_for(credential, **kwargs):
            ctx = browser.new_context(**kwargs)
            ctx.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(credential) + ")")
            ctx.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            return ctx
        context = context_for(token, viewport={"width": 1160, "height": 820})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(env.url + "/#project")
        field = page.locator("#composer textarea")
        field.fill("@")
        options = page.locator("#composer .mention-pop [role=option]")
        pw.expect(options.first).to_contain_text("Omega Helper")
        pw.expect(options.nth(1)).to_contain_text("Zeta Helper")
        pw.expect(options.first.locator(".mention-yours")).to_have_text("Yours")
        assert options.count() <= 7
        field.fill("@helper")  # both owned substring matches precede another person's exact match
        pw.expect(options.first).to_contain_text("Omega Helper")
        pw.expect(options.nth(1)).to_contain_text("Zeta Helper")
        pw.expect(options.nth(2)).to_contain_text("Helper")
        field.fill("@alpha")
        pw.expect(options.first).to_contain_text("Person B’s agent")
        pw.expect(options.locator(".mention-yours")).to_have_count(0)
        page.screenshot(path=str(tmp_path / "other-owner-desktop.png"))
        field.fill("@")
        pw.expect(options.first).to_contain_text("Omega Helper")
        page.screenshot(path=str(tmp_path / "myagents-desktop.png"))
        field.fill("@my")
        pw.expect(options).to_have_count(1)
        pw.expect(options.first).to_contain_text("your agents in this channel")
        field.press("Tab")
        pw.expect(field).to_have_value("@myagents ")
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/channels/{cid}/messages")) as sent:
            field.press("Enter")
        message = sent.value.json()
        mid = message["id"]
        assert {m["external_id"] for m in message["mentions"]} == {"omega-helper", "zeta-helper"}
        # The shared reply composer also prioritizes owned agents.
        page.locator(f'#messages [data-mid="{mid}"]').hover()
        page.locator(f'#messages [data-thread="{mid}"]').click()
        reply = page.locator("#panel-composer textarea")
        reply.fill("@")
        pw.expect(page.locator("#panel-composer [role=option]").first).to_contain_text("Omega Helper")
        reply.fill("@my")
        reply.press("Tab")
        pw.expect(reply).to_have_value("@myagents ")
        reply.fill(""); page.locator("#panel-close").click()
        page.locator(f'#messages [data-mid="{mid}"]').hover()
        page.locator(f'#messages [data-edit="{mid}"]').click()
        edit = page.locator("#messages .edit-box textarea")
        edit.fill("@myagents updated")
        page.locator("[data-edit-save]").click()
        pw.expect(page.locator("#messages .edit-box")).to_have_count(0)
        # Tasks answers use the shared composer with the destination channel's candidates.
        task = mine.put_task("mention-labels", "Review the details", channel_id=cid)
        page.locator("#tasks-open").click()
        page.locator(f'[data-tasks-open="{task["id"]}"]').click()
        answer = page.locator("#tasks-answer-composer textarea")
        answer.fill("@")
        pw.expect(page.locator("#tasks-answer-composer [role=option]").first).to_contain_text("Omega Helper")
        answer.fill("@my")
        answer.press("Tab")
        pw.expect(answer).to_have_value("@myagents ")
        page.locator("#tasks-item-close").click()
        # A display name that is also a group word inserts the safe handle instead.
        owner.update_identity(named["id"], display_name="MyAgents")
        page.goto(env.url + "/#project")
        field.fill("@my")
        pw.expect(options.first).to_contain_text("Yours")
        field.press("Tab")
        pw.expect(field).to_have_value("@zeta-helper ")
        # Rendering 'my' is relative to the author, including when other people read it.
        for credential, expected in [(mine_token, True), (other_token, False)]:
            viewer = context_for(credential)
            view = viewer.new_page()
            view.on("pageerror", lambda error: errors.append(str(error)))
            view.goto(env.url + f"/#msg/{mid}")
            tag = view.locator(f'#messages [data-mid="{mid}"] .mention.group')
            pw.expect(tag).to_have_text("@myagents")
            assert tag.evaluate("el => el.classList.contains('mine')") is expected
            assert "author" in tag.get_attribute("title")
            viewer.close()
        # A crowded owned prefix must retain the group shortcut, after the owned rows.
        owner.update_identity(named["id"], display_name="Zeta Helper")
        for i in range(7):
            env.agent(f"myagents-{i}", owner=owner, channels=["project"])
            owner.create_channel(f"clip-{i}")
        page.set_viewport_size({"width": 844, "height": 300})
        page.reload()
        def selected_is_visible():
            selected = page.locator("#composer .mention-pop .on")
            assert selected.evaluate("""el => {
                const row = el.getBoundingClientRect(), box = el.parentElement.getBoundingClientRect();
                return row.top >= box.top - 1 && row.bottom <= box.bottom + 1;
            }""")
            assert page.locator("#composer .mention-pop").evaluate("el => el.scrollTop > 0")
        for prefix, last in [("@myagents-", "myagents-6"), ("#clip", "clip-6")]:
            field.fill(prefix)
            pw.expect(options).to_have_count(7)
            field.press("ArrowUp")
            pw.expect(page.locator("#composer .mention-pop .on")).to_contain_text(last)
            selected_is_visible()
            field.press("Tab")
            pw.expect(field).to_have_value(prefix[0] + last + " ")
        for prefix in ["@myagents", "@my"]:
            field.fill(prefix)
            pw.expect(options).to_have_count(8)
            pw.expect(options.nth(6)).to_contain_text("Yours")
            pw.expect(options.last).to_contain_text("your agents in this channel")
            field.press("ArrowUp")
            pw.expect(page.locator("#composer .mention-pop .on")).to_contain_text("your agents in this channel")
            selected_is_visible()
            if prefix == "@my":
                page.screenshot(path=str(tmp_path / "myagents-crowded-landscape.png"))
                page.locator("#composer .mention-pop").screenshot(path=str(tmp_path / "myagents-crowded-picker.png"))
            field.press("Tab")
            pw.expect(field).to_have_value("@myagents ")
        touch = context_for(token, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = touch.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(env.url + "/#project")
        mobile_field = mobile.locator("#composer textarea")
        mobile_field.fill("@")
        mobile_options = mobile.locator("#composer [role=option]")
        pw.expect(mobile_options.first).to_contain_text("Yours")
        pw.expect(mobile_options.nth(1)).to_contain_text("Yours")
        assert mobile.locator("body").evaluate("el => el.scrollWidth <= innerWidth")
        mobile.screenshot(path=str(tmp_path / "myagents-phone.png"))
        mobile_field.fill("@myagents")
        # Here the renamed agent is also a match, so choose the actual group row by text.
        mobile_options.filter(has_text="your agents in this channel").tap()
        pw.expect(mobile_field).to_have_value("@myagents ")
        assert not errors
        browser.close()
