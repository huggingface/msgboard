"""Explicit thread following uses real boards, clients and the existing activity stream."""

import json
import os

import pytest

from msgboard.client import BoardError


def test_subscription_overrides_activity_counts_permissions_and_survives_restart(env):
    ch = env.op.create_channel("subscriptions")
    _, token, reader = env.human("reader")
    _, _, author = env.human("author")
    root = author.post(ch["id"], "A discussion")
    tid, cid = root["id"], ch["id"]
    old = author.post(cid, "Old reply", reply_to=tid)
    assert not reader.thread_subscription(cid, tid)["subscribed"]
    assert reader.activity() == []
    assert reader.subscribe_thread(cid, tid)["subscribed"]
    assert reader.activity() == []                 # subscribing follows future replies, not old history
    reply = author.post(cid, "New reply", reply_to=tid)
    reader.subscribe_thread(cid, tid)              # an identical retry must not hide that reply
    assert [m["id"] for m in reader.activity()] == [reply["id"]]
    assert reader.activity()[0]["reason"] == "reply"
    counts = next(c for c in reader.list_channels() if c["id"] == cid)
    assert (counts["for_me"], counts["threads_for_me"]) == (1, 1)
    assert reader.read(cid)["messages"][0]["replies"]["for_me_unread"] == 1
    assert reader.thread_subscriptions(cid, [tid, tid]) == [reader.thread_subscription(cid, tid)]
    assert reader.mark_read(cid, reply["seq"])["for_me"] == 1
    assert reader.mark_thread_read(cid, tid, reply["seq"])["for_me"] == 0

    reader.subscribe_thread(cid, tid, False)
    reader.post(cid, "I joined, but keep my explicit opt-out", reply_to=tid)
    quiet = author.post(cid, "No automatic alert", reply_to=tid)
    mention = author.post(cid, "@reader please look", reply_to=tid)
    assert [m["id"] for m in reader.activity()] == [mention["id"]]
    assert reader.thread_subscription(cid, tid)["for_me_unread"] == 1
    assert reader.read(cid)["messages"][0]["replies"]["for_me_unread"] == 1
    # A followed-channel listener still sees normal traffic, with the correct reason.
    reasons = {m["id"]: m["reason"] for m in reader.activity_page(0, channels=[cid])["messages"]}
    assert reasons[quiet["id"]] == "channel" and reasons[mention["id"]] == "mention"
    other_device = env.client(token)
    assert other_device.thread_subscription(cid, tid)["mode"] == "unsubscribed"
    other_device.subscribe_thread(cid, tid)
    later = author.post(cid, "Following again", reply_to=tid)
    assert [m["id"] for m in reader.activity()] == [later["id"], mention["id"]]

    # Automatic following stays the default for authors and participants.
    assert author.thread_subscription(cid, tid)["mode"] == "auto"
    assert author.thread_subscription(cid, tid)["subscribed"]
    author.subscribe_thread(cid, tid, False)
    reader.post(cid, "No alert to the author", reply_to=tid)
    assert author.activity() == []
    with env.http(token) as h:
        path = f"/channels/{cid}/threads/{tid}/subscription"
        for body in [{}, {"subscribed": 1}, {"subscribed": "true"}, {"subscribed": None}, {"subscribed": True, "principal_id": 1}]:
            assert h.put(path, json=body).status_code == 400
            assert reader.thread_subscription(cid, tid)["mode"] == "subscribed"
        assert h.put(f"/channels/{cid}/threads/{old['id']}/subscription", json={"subscribed": True}).status_code == 404
        for roots in ["", "x", "0", "-1", str(2**63), ",".join([str(tid)] * 201)]:
            assert h.get(f"/channels/{cid}/thread-subscriptions", params={"roots": roots}).status_code == 400
    private = author.create_channel("private-subscriptions", private=True)
    hidden = author.post(private["id"], "Hidden")
    assert [s["thread_id"] for s in reader.thread_subscriptions(cid, [tid, hidden["id"], old["id"]])] == [tid]
    with env.http(token) as h:
        assert h.get(f"/channels/{private['id']}/thread-subscriptions", params={"roots": hidden["id"]}).status_code == 404
    for method in ["GET", "PUT"]:
        with env.http(token) as h:
            r = h.request(method, f"/channels/{private['id']}/threads/{hidden['id']}/subscription",
                          **({"json": {"subscribed": True}} if method == "PUT" else {}))
            assert r.status_code == 404
    author.add_member(private["id"], reader.me()["id"])
    reader.subscribe_thread(private["id"], hidden["id"])
    author.remove_member(private["id"], reader.me()["id"])
    author.post(private["id"], "After access removed", reply_to=hidden["id"])
    assert all(m["channel_id"] != private["id"] for m in reader.activity())

    # A muted DM thread does not mute unrelated DMs or direct mentions.
    dm = author.open_dm(reader.me()["id"])
    dm_root = author.post(dm["id"], "Direct question")
    reader.subscribe_thread(dm["id"], dm_root["id"], False)
    dm_quiet = author.post(dm["id"], "Quiet DM reply", reply_to=dm_root["id"])
    dm_mention = author.post(dm["id"], "@reader explicit", reply_to=dm_root["id"])
    dm_other = author.post(dm["id"], "Another direct message")
    ids = {m["id"] for m in reader.activity()}
    assert dm_quiet["id"] not in ids and {dm_mention["id"], dm_other["id"]} <= ids
    env.restart()
    reader = env.client(token)
    assert reader.thread_subscription(cid, tid)["mode"] == "subscribed"
    assert reader.thread_subscription(dm["id"], dm_root["id"])["mode"] == "unsubscribed"
    env.op.delete_message(cid, tid)                  # subscriptions cannot block root/channel deletion
    with pytest.raises(BoardError) as exc:
        reader.thread_subscription(cid, tid)
    assert exc.value.status == 404
    env.op.delete_channel(private["id"], private["name"])


def test_subscription_buttons_share_state_on_desktop_and_touch(env, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    _, token, reader = env.human("person-a")
    _, _, writer = env.human("person-b")
    channel = writer.create_channel("subscription-checks")
    root = writer.post(channel["id"], "Let’s compare the two options here.")
    tid, cid = root["id"], channel["id"]
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        ctx.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(env.url + "/#subscription-checks")
        post = page.locator(f'#messages [data-mid="{tid}"] [data-thread-subscribe]')
        pw.expect(post).to_be_enabled()         # works on a post before its first reply
        pw.expect(post.locator("..")).to_have_css("pointer-events", "none")
        post.focus()
        pw.expect(post.locator("..")).to_have_css("pointer-events", "auto")
        post.press("Space")
        pw.expect(post).to_have_attribute("aria-pressed", "true")
        page.locator(f'#messages [data-mid="{tid}"]').hover()
        page.locator(f'#messages [data-thread="{tid}"]').click()
        header = page.locator("#panel-subscribe")
        pw.expect(header).to_have_text("Unsubscribe")
        pw.expect(header).to_have_attribute("aria-pressed", "true")
        page.screenshot(path=str(tmp_path / "subscriptions-desktop.png"))
        header.click()
        pw.expect(header).to_have_text("Subscribe")
        pw.expect(post).to_have_attribute("aria-pressed", "false")
        # Replying after an explicit opt-out does not silently subscribe again.
        field = page.locator("#panel-composer textarea")
        field.fill("I’m participating, but keeping reply alerts off.")
        field.press("Enter")
        pw.expect(field).to_have_value("")
        pw.expect(header).to_have_text("Subscribe")
        assert not reader.thread_subscription(cid, tid)["subscribed"]
        page.locator("#panel-close").click()
        # A different device changes this closed thread; focus must reconcile its bell.
        reader.subscribe_thread(cid, tid)
        with page.expect_response(lambda r: r.request.method == "GET" and "/thread-subscriptions?" in r.url):
            page.evaluate("window.dispatchEvent(new Event('focus'))")
        pw.expect(post).to_have_attribute("aria-pressed", "true")
        reader.subscribe_thread(cid, tid, False)
        writer.post(cid, "Quiet reply", reply_to=tid)
        pw.expect(page.locator(f'#messages [data-mid="{tid}"] .replies b')).to_have_text("2 replies")
        pw.expect(post).to_have_attribute("aria-pressed", "false")
        pw.expect(page.locator(f'#messages [data-mid="{tid}"] .rbadge')).to_have_count(0)
        assert next(c for c in reader.list_channels() if c["id"] == cid)["for_me"] == 0
        mention = writer.post(cid, "@person-a please look", reply_to=tid)
        pw.expect(page.locator(f'#messages [data-mid="{tid}"] .rbadge')).to_have_text("1 for you")
        assert [m["id"] for m in reader.activity()] == [mention["id"]]
        page.reload()
        pw.expect(post).to_have_attribute("aria-pressed", "false")
        post.focus()
        post.press("Enter")
        pw.expect(post).to_have_attribute("aria-pressed", "true")
        # A different browser reads the saved choice. A tap updates both copies.
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        phone.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        mobile = phone.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(env.url + f"/#msg/{tid}")
        mobile_post = mobile.locator(f'#messages [data-mid="{tid}"] [data-thread-subscribe]')
        pw.expect(mobile_post.locator("..")).to_have_css("opacity", "1")
        pw.expect(mobile_post.locator("..")).to_have_css("pointer-events", "auto")
        mobile_post.tap()                       # the control is visible before the first tap
        pw.expect(mobile_post).to_have_attribute("aria-pressed", "false")
        mobile_post.tap()
        pw.expect(mobile_post).to_have_attribute("aria-pressed", "true")
        mobile.locator(f'#messages .replies[data-thread="{tid}"]').tap()
        toggle = mobile.locator("#panel-subscribe")
        pw.expect(toggle).to_have_text("Unsubscribe")
        toggle.tap()
        pw.expect(toggle).to_have_text("Subscribe")
        assert not reader.thread_subscription(cid, tid)["subscribed"]
        assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
        mobile.screenshot(path=str(tmp_path / "subscriptions-phone.png"))
        page.reload()
        pw.expect(post).to_have_attribute("aria-pressed", "false")
        held = {}

        def hold_subscription_read(route):
            if route.request.method == "GET":
                held["response"] = route.fetch()   # delay an actual response from this board
                held["route"] = route
            else:
                route.continue_()

        page.route(f"**/channels/{cid}/threads/*/subscription", hold_subscription_read)
        composer = page.locator("#composer textarea")
        composer.fill("My new thread")
        with page.expect_request(lambda r: r.method == "GET" and r.url.endswith("/subscription")):
            composer.press("Enter")
        pw.expect(composer).to_have_value("")
        own = reader.read(cid)["messages"][-1]
        # A choice on another thread must not invalidate this thread's pending read.
        post.focus()
        post.press("Enter")
        pw.expect(post).to_have_attribute("aria-pressed", "true")
        held["route"].fulfill(response=held["response"])
        pw.expect(page.locator(f'#messages [data-mid="{own["id"]}"] [data-thread-subscribe]')).to_have_attribute("aria-pressed", "true")
        page.unroute(f"**/channels/{cid}/threads/*/subscription", hold_subscription_read)

        other = reader.create_channel("navigation-other")
        page.reload()                         # both channels are now in the sidebar
        pw.expect(page.locator("#composer textarea")).to_be_visible()
        for kind in ["single", "batch"]:
            delayed = {}
            pattern = (f"**/channels/{cid}/threads/*/subscription" if kind == "single"
                       else f"**/channels/{cid}/thread-subscriptions?*")

            def hold_navigation_read(route):
                if route.request.method == "GET" and not delayed:
                    delayed["response"] = route.fetch()
                    delayed["route"] = route
                else:
                    route.continue_()

            page.route(pattern, hold_navigation_read)
            if kind == "single":
                composer.fill("A new navigation check")
                with page.expect_request(lambda r: r.method == "GET" and r.url.endswith("/subscription")):
                    composer.press("Enter")
                pw.expect(composer).to_have_value("")
                target = reader.read(cid)["messages"][-1]
                desired = False               # the held automatic-follow response says true
            else:
                target, desired = root, False
                reader.subscribe_thread(cid, tid)
                with page.expect_request(lambda r: "/thread-subscriptions?" in r.url):
                    page.evaluate("window.dispatchEvent(new Event('focus'))")
            reader.subscribe_thread(cid, target["id"], desired)
            page.locator("#channels button").filter(has_text=other["name"]).click()
            pw.expect(page.locator("#composer textarea")).to_have_attribute("placeholder", "Message #" + other["name"])
            page.locator("#channels button").filter(has_text=channel["name"]).click()
            current = page.locator(f'#messages [data-mid="{target["id"]}"] [data-thread-subscribe]')
            pw.expect(current).to_have_attribute("aria-pressed", str(desired).lower())
            with page.expect_response(lambda r: (r.url.endswith("/subscription") if kind == "single"
                                                else "/thread-subscriptions?" in r.url)) as old_response:
                delayed["route"].fulfill(response=delayed["response"])
            old_response.value.finished()
            page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            pw.expect(current).to_have_attribute("aria-pressed", str(desired).lower())
            assert reader.thread_subscription(cid, target["id"])["subscribed"] is desired
            page.unroute(pattern, hold_navigation_read)
        assert errors == []
        phone.close()
        ctx.close()
        browser.close()
