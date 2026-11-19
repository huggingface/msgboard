"""Personal sidebar groups exercised in the real desktop and touch UI."""

import json
import os

import pytest


def test_channel_groups_desktop_touch_order_and_save_failures(env, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    _, token, reader = env.human("person-a")
    _, _, writer = env.human("person-b")
    env.op.create_channel("general")
    channels = [writer.create_channel(name) for name in ("dev-msgboard", "dev-runtime", "papers")]
    cid, runtime, papers = [c["id"] for c in channels]
    secret = writer.create_channel("invisible-channel", private=True)
    dm = reader.open_dm(writer.me()["id"])
    writer.post(cid, "@person-a please check")
    writer.post(runtime, "@person-a a second question")
    errors = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1160, "height": 850})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(env.url + "/#general")
        pw.expect(page.locator("#ch-title")).to_have_text("#general")

        def menu(label):
            page.get_by_role("button", name=label, exact=True).click()

        def create_group(name):
            menu("New channel or group")
            page.get_by_role("menuitem", name="New group…", exact=True).click()
            page.locator("#group-name").fill(name)
            page.locator("#group-save").click()
            pw.expect(page.locator("#group-dialog")).not_to_be_visible()

        def move(name, group):
            menu("Move " + name + " to group")
            with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/me/prefs")):
                page.get_by_role("menuitem", name=group, exact=True).click()

        create_group("Engineering")
        create_group("Research")
        move("dev-msgboard", "Engineering")
        move("dev-runtime", "Engineering")
        move("papers", "Research")
        group = page.locator(".channel-group").filter(has=page.locator(".group-name", has_text="Engineering"))
        pw.expect(group.locator("li[data-cid]")).to_have_count(2)
        # Real HTML drag: between groups, back into a collapsed group, and within a group.
        page.locator(f'li[data-cid="{runtime}"]').drag_to(page.get_by_role("button", name="Research", exact=True))
        pw.expect(group.locator("li[data-cid]")).to_have_count(1)
        page.get_by_role("button", name="Engineering", exact=True).click()
        page.locator(f'li[data-cid="{runtime}"]').drag_to(group.locator(".group-toggle"))
        pw.expect(group.locator(".group-toggle")).to_have_attribute("aria-expanded", "false")
        pw.expect(group.locator(".group-toggle .count")).to_have_text("2")
        page.screenshot(path=str(tmp_path / "groups-collapsed.png"))
        group.locator(".group-toggle").click()
        source, target = page.locator(f'li[data-cid="{runtime}"]'), page.locator(f'li[data-cid="{cid}"]')
        source.drag_to(target, target_position={"x": 50, "y": 2})
        pw.expect(group.locator("li[data-cid]").first).to_have_attribute("data-cid", str(runtime))
        page.screenshot(path=str(tmp_path / "groups-desktop.png"))
        # Native dragging also reorders entire groups.
        page.get_by_role("button", name="Research", exact=True).drag_to(group.locator(".group-head"), target_position={"x": 40, "y": 2})
        pw.expect(page.locator(".group-name").first).to_have_text("Research")
        group.locator(".group-head").drag_to(page.locator(".group-head").first, target_position={"x": 40, "y": 2})
        pw.expect(page.locator(".group-name").first).to_have_text("Engineering")
        # Arrow-key menu navigation, group order and escaped names.
        menu("Research group options")
        page.get_by_role("menuitem", name="Rename group…", exact=True).press("Enter")
        page.locator("#group-name").fill("Research <notes>")
        page.locator("#group-save").click()
        pw.expect(page.get_by_role("button", name="Research <notes>", exact=True)).to_be_visible()
        page.get_by_role("button", name="Research <notes> group options", exact=True).click()
        page.get_by_role("menuitem", name="Rename group…", exact=True).press("ArrowDown")
        page.keyboard.press("Enter")  # Move up
        pw.expect(page.locator(".group-name").first).to_have_text("Research <notes>")
        page.reload()
        pw.expect(page.locator(".group-name").first).to_have_text("Research <notes>")
        pw.expect(group.locator("li[data-cid]").first).to_have_attribute("data-cid", str(runtime))
        # Archived channels respect the same sidebar filter inside groups.
        writer.update_channel(papers, archived=True)
        with env.http(token) as h:
            assert h.put("/me/prefs", json={"hide_archived": True}).status_code == 200
        page.reload()
        pw.expect(page.locator(f'#channels li[data-cid="{papers}"]')).to_have_count(0)
        page.locator("#show-archived").check()
        pw.expect(page.locator(f'#channels li[data-cid="{papers}"]')).to_be_visible()
        # Failed writes roll back the visual layout instead of leaving an unsaved group.
        page.route("**/v1/me/prefs", lambda route: route.fulfill(status=503, json={"error": {"code": "unavailable", "message": "Try again"}}))
        page.once("dialog", lambda dialog: dialog.accept())
        group.locator(".group-toggle").click()
        pw.expect(group.locator(".group-toggle")).to_have_attribute("aria-expanded", "true")
        page.unroute("**/v1/me/prefs")
        # A real delayed response cannot erase a newer collapse choice.
        responses = []
        def hold_first(route):
            if not responses:
                response = route.fetch()
                responses.append((route, response))
            else:
                route.continue_()
        page.route("**/v1/me/prefs", hold_first)
        with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/me/prefs")):
            group.locator(".group-toggle").click()
        pw.expect(group.locator(".group-toggle")).to_have_attribute("aria-expanded", "false")
        group.locator(".group-toggle").click()
        assert responses
        route, response = responses[0]
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/me/prefs")
                                  and any(g["name"] == "Engineering" and not g["collapsed"]
                                          for g in r.request.post_data_json["channel_groups"])):
            route.fulfill(response=response)
        page.unroute("**/v1/me/prefs")
        pw.expect(group.locator(".group-toggle")).to_have_attribute("aria-expanded", "true")
        page.reload()
        pw.expect(group.locator(".group-toggle")).to_have_attribute("aria-expanded", "true")
        # Touch menu is visible and lets another browser move channels without dragging.
        touch = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        touch.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        touch.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        mobile = touch.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(env.url + "/#general")
        mobile.locator("#menu").tap()
        options = mobile.get_by_role("button", name="Move papers to group", exact=True)
        pw.expect(options).to_have_css("opacity", "1")
        options.tap()
        mobile.get_by_role("menuitem", name="Engineering", exact=True).tap()
        mobile.screenshot(path=str(tmp_path / "groups-phone.png"))
        engineering = mobile.locator(".channel-group").filter(has=mobile.locator(".group-name", has_text="Engineering"))
        pw.expect(engineering.locator(f'li[data-cid="{papers}"]')).to_be_visible()
        mobile.get_by_role("button", name="Engineering group options", exact=True).tap()
        mobile.once("dialog", lambda dialog: dialog.accept())
        mobile.get_by_role("menuitem", name="Remove group", exact=True).tap()
        pw.expect(mobile.locator(f'#channels-ungrouped li[data-cid="{papers}"]')).to_be_visible()
        assert mobile.locator("body").evaluate("el => el.scrollWidth <= innerWidth")
        # Saved layout cannot expose a hidden channel or pull a DM out of Direct messages.
        prefs = reader.me()["prefs"]
        prefs["channel_groups"][0]["channels"] = [secret["id"], dm["id"]]
        with env.http(token) as h:
            assert h.put("/me/prefs", json={"channel_groups": prefs["channel_groups"]}).status_code == 200
        mobile.reload()
        mobile.locator("#menu").tap()
        pw.expect(mobile.locator(f'#channels li[data-cid="{secret["id"]}"]')).to_have_count(0)
        pw.expect(mobile.locator("#dms li")).to_have_count(1)
        assert secret["id"] not in [c["id"] for c in reader.list_channels()]
        assert not errors
        browser.close()
