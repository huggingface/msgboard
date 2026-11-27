"""Task attention, DM history and thread reading positions on a real board."""

import json
import os
import re

import pytest

pw = pytest.importorskip("playwright.sync_api")


def test_thread_panel_preserves_channel_reading_position(env):
    _, token, owner = env.human("reader")
    channel = owner.create_channel("reading")
    for i in range(18):
        owner.create_channel(f"scroll-sidebar-{i}")
    messages = [owner.post(channel["id"], f"Message {i}: " + "A longer paragraph wraps differently when the thread opens. " * 16)
                for i in range(45)]
    for message in (messages[22], messages[-1]):
        owner.post(channel["id"], "A reply that needs scrolling. " * 60, reply_to=message["id"])
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
                                    ignore_default_args=["--hide-scrollbars"])
        context = browser.new_context(viewport={"width": 1440, "height": 850})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(env.url + "/#reading")
        pw.expect(page.locator('#messages .msg')).to_have_count(45)
        root_id = messages[22]["id"]
        root = page.locator(f'#messages .msg[data-mid="{root_id}"]')
        root.evaluate("el => { const sc=document.getElementById('scroll'); sc.scrollTop += el.getBoundingClientRect().top-sc.getBoundingClientRect().top+30; }")
        top = root.bounding_box()["y"]
        # A native click, rather than calling the dashboard's internal functions.
        root.locator(f'.replies[data-thread="{root_id}"]').click()
        pw.expect(page.locator('#panel-composer textarea')).to_be_visible()
        pw.expect(root).to_be_in_viewport()
        page.wait_for_function("({id,top}) => Math.abs(document.querySelector('#messages .msg[data-mid=\"'+id+'\"]').getBoundingClientRect().top-top)<3", arg={"id":root_id,"top":top}, timeout=3000)
        # Reading a pane under the pointer must not keep its scrollbar visible.
        hidden = "rgba(0, 0, 0, 0) rgba(0, 0, 0, 0)"
        for selector in ('.nav-scroll', '#scroll', '#panel-scroll'):
            pane = page.locator(selector)
            assert pane.evaluate('el => el.scrollHeight > el.clientHeight')
            pane.evaluate('el => el.scrollTop = el.scrollHeight / 2')
            pane.hover(position={'x': 30, 'y': 40})
            pw.expect(pane).to_have_css('scrollbar-color', hidden)
            before_width = pane.evaluate('el => el.clientWidth')
            page.mouse.wheel(0, -120)
            pw.expect(pane).not_to_have_css('scrollbar-color', hidden)
            pw.expect(pane).to_have_css('scrollbar-color', hidden)
            assert pane.evaluate('el => el.clientWidth') == before_width
            box = pane.bounding_box()
            page.mouse.move(box['x'] + box['width'] - 18, box['y'] + 40)
            pw.expect(pane).not_to_have_css('scrollbar-color', hidden)
            page.mouse.move(box['x'] + box['width'] - 8, box['y'] + 40)
            pw.expect(pane).to_have_css('scrollbar-color', re.compile(r'^rgb\('))
            # Exercise the real native thumb, not only its computed CSS color.
            before_drag = pane.evaluate('el => el.scrollTop = (el.scrollHeight - el.clientHeight) / 2')
            thumb_y = box['y'] + pane.evaluate('el => el.clientHeight / 2')
            page.mouse.move(box['x'] + box['width'] - 18, thumb_y)
            page.mouse.move(box['x'] + box['width'] - 8, thumb_y)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] - 8, thumb_y + 80, steps=4)
            page.mouse.up()
            page.wait_for_function("({selector, before}) => document.querySelector(selector).scrollTop > before",
                                   arg={"selector": selector, "before": before_drag}, timeout=3000)
            pane.hover(position={'x': 30, 'y': 40})
            pw.expect(pane).to_have_css('scrollbar-color', hidden)

        # Escape changes the pane geometry without moving the pointer.
        feed = page.locator('#scroll')
        feed.hover(position={'x': 30, 'y': 80})
        pw.expect(feed).to_have_css('scrollbar-color', hidden)
        box = feed.bounding_box()
        page.mouse.move(box['x'] + box['width'] - 18, box['y'] + 80)
        pw.expect(feed).not_to_have_css('scrollbar-color', hidden)
        root.evaluate("el => { const sc=document.getElementById('scroll'); sc.scrollTop += el.getBoundingClientRect().top-sc.getBoundingClientRect().top+30; }")
        page.keyboard.press('Escape')
        pw.expect(page.locator('#panel')).not_to_be_visible()
        pw.expect(feed).to_have_css('scrollbar-color', hidden)
        page.wait_for_function("({id,top}) => Math.abs(document.querySelector('#messages .msg[data-mid=\"'+id+'\"]').getBoundingClientRect().top-top)<3", arg={"id":root_id,"top":top}, timeout=3000)
        # Window widening must also reconsider a stationary pointer's old edge.
        box = feed.bounding_box()
        page.mouse.move(box['x'] + box['width'] - 18, box['y'] + 80)
        pw.expect(feed).not_to_have_css('scrollbar-color', hidden)
        page.set_viewport_size({'width': 1700, 'height': 850})
        pw.expect(feed).to_have_css('scrollbar-color', hidden)
        page.set_viewport_size({'width': 1440, 'height': 850})
        feed.hover(position={'x': 30, 'y': 40})
        # At the end of a channel, opening and closing a thread keeps the end visible.
        page.locator('#scroll').evaluate("el => el.scrollTop=el.scrollHeight")
        last = messages[-1]["id"]
        page.locator(f'#messages .replies[data-thread="{last}"]').click()
        page.wait_for_function("(()=>{const s=document.getElementById('scroll');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
        # Exit through the native gutter without blurring the window, then reflow with Escape.
        panel = page.locator('#panel-scroll')
        box = panel.bounding_box()
        page.mouse.move(box['x'] + box['width'] - 18, box['y'] + 80)
        pw.expect(panel).not_to_have_css('scrollbar-color', hidden)
        page.mouse.move(1500, box['y'] + 80)
        page.keyboard.press('Escape')
        pw.expect(feed).to_have_css('scrollbar-color', hidden)
        page.wait_for_function("(()=>{const s=document.getElementById('scroll');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
        # Clicking in the sidebar is not keyboard navigation and must not pin its bar.
        page.locator('#activity-open').click()
        nav = page.locator('.nav-scroll')
        assert nav.evaluate("el => el.matches(':focus-within')")
        pw.expect(nav).to_have_css('scrollbar-color', hidden)
        page.keyboard.press('Tab')
        page.wait_for_function("getComputedStyle(document.querySelector('.nav-scroll')).scrollbarColor !== 'rgba(0, 0, 0, 0) rgba(0, 0, 0, 0)'")
        page.emulate_media(forced_colors='active')
        pw.expect(nav).to_have_css('scrollbar-color', 'auto')
        page.emulate_media(forced_colors='none')
        # Settings width changes can move a profile panel without resizing an observed pane.
        page.set_viewport_size({'width': 1440, 'height': 420})
        page.locator('#settings-open').click()
        page.locator('#me-who').click()
        profile = page.locator('#panel-scroll')
        pw.expect(profile.locator('.profile')).to_be_visible()
        assert profile.evaluate('el => el.scrollHeight > el.clientHeight')
        profile.hover(position={'x': 30, 'y': 80})
        pw.expect(profile).to_have_css('scrollbar-color', hidden)
        box = profile.bounding_box()
        page.mouse.move(box['x'] + box['width'] - 18, box['y'] + 80)
        pw.expect(profile).not_to_have_css('scrollbar-color', hidden)
        page.set_viewport_size({'width': 1600, 'height': 420})
        pw.expect(profile).to_have_css('scrollbar-color', hidden)
        page.locator('#panel-close').click()
        page.set_viewport_size({'width': 1440, 'height': 850})
        # Text fields retain :focus-visible even after a mouse click; reading search results stays idle.
        page.keyboard.press('Control+k')
        query = page.locator('#search-q')
        query.fill('Message')
        query.press('Enter')
        pw.expect(page.locator('#search-results .sres')).to_have_count(30)
        search = page.locator('#search')
        assert search.evaluate('el => el.scrollHeight > el.clientHeight')
        search.hover(position={'x': 40, 'y': 350})
        assert query.evaluate('el => el.matches(":focus-visible")')
        pw.expect(search).to_have_css('scrollbar-color', hidden)
        assert errors == []
        browser.close()


def test_tasks_sidebar_history_drafts_attention_and_personal_settings(env, tmp_path):
    _, token, owner = env.human("task-owner", role="admin")
    channel = owner.create_channel("tasks-ui")
    a, _, agent = env.agent("first-helper", owner=owner, channels=["tasks-ui"])
    b, _, helper = env.agent("second-helper", owner=owner)
    agent.put_task("one", "First decision", channel_id=channel["id"])
    agent.put_task("two", "Second decision", channel_id=channel["id"])
    dm = owner.open_dm(a["id"])
    history_messages = [agent.post(dm["id"], f"History message {i}") for i in range(46)]
    errors = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = context.new_page()
        read_failures = [503, 404]
        def fail_read_once(route):
            if read_failures:
                status = read_failures.pop(0)
                route.fulfill(status=status, content_type="application/json", body=json.dumps({"error": {"code": "unavailable", "message": "Try refreshing"}}))
            else:
                route.continue_()
        page.route("**/v1/tasks/read", fail_read_once)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(env.url + "/#tasks-ui")
        pw.expect(page.locator('#tasks-open .count')).to_have_text('2')
        page.locator('#tasks-open').click()
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(2)
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0, timeout=12000)
        assert read_failures == []
        assert owner.tasks()["counts"]["open"] == 2
        pw.expect(page.locator('#tasks-dm-history')).to_be_visible()
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 45')
        page.locator('#tasks-dm-older').click()
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 0')
        assert page.locator('#tasks-dm-history').bounding_box()['height'] > 400
        draft = page.locator('#tasks-dm-composer textarea')
        draft.fill('A draft for the first agent')
        page.locator('#tasks-dm-agent').select_option(str(b['id']))
        pw.expect(draft).to_have_value('')
        # The first message and reply in an empty DM must appear normally.
        draft.fill('First message in a fresh DM')
        page.locator('#tasks-dm-composer .btn').click()
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('First message in a fresh DM', timeout=12000)
        dm_b = owner.open_dm(b['id'])
        helper.post(dm_b['id'], 'First reply in the fresh DM')
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('First reply in the fresh DM', timeout=12000)
        draft.fill('A draft for the second agent')
        page.locator('#tasks-dm-agent').select_option(str(a['id']))
        pw.expect(draft).to_have_value('A draft for the first agent')
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 0')
        # A delayed B open must not save B's Loading scroll position into A.
        history = page.locator('#tasks-dm-history')
        history.evaluate('el => el.scrollTop=700')
        saved_scroll = history.evaluate('el => el.scrollTop')
        delayed = []
        def hold_b(route):
            if route.request.post_data_json['with'] == b['id']:
                delayed.append(route)
            else:
                route.continue_()
        page.route('**/v1/dms', hold_b)
        with page.expect_request('**/v1/dms'):
            page.locator('#tasks-dm-agent').select_option(str(b['id']))
        pw.expect(page.locator('#tasks-dm-messages')).to_have_text('Loading…')
        page.locator('#tasks-dm-agent').select_option(str(a['id']))
        pw.expect(draft).to_have_value('A draft for the first agent')
        with page.expect_response('**/v1/dms'):
            delayed[0].continue_()
        page.unroute('**/v1/dms', hold_b)
        page.wait_for_function('expected => Math.abs(document.getElementById("tasks-dm-history").scrollTop-expected)<3', arg=saved_scroll)
        # Live additions wait behind a button; existing rows keep their order.
        first_row_top = page.locator('#tasks-list .tasks-row').first.bounding_box()['y']
        third = agent.put_task('three', 'Arrived while reading', channel_id=channel['id'])
        pw.expect(page.locator('#tasks-new')).to_be_visible(timeout=12000)
        assert abs(page.locator('#tasks-list .tasks-row').first.bounding_box()['y'] - first_row_top) < 1
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(2)
        pw.expect(page.locator('#tasks-open .count')).to_have_text('1')
        page.locator('#tasks-new').click()
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(3)
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0)
        assert owner.task(third['id'])['state'] == 'open'
        # Loading newer messages retains the older pages and doesn't pull the reader down.
        history = page.locator('#tasks-dm-history')
        history.evaluate('el => el.scrollTop=el.scrollHeight')
        held_history = []
        history_url = f'**/v1/channels/{dm["id"]}/messages?*'
        def hold_history(route):
            if not held_history:
                held_history.append(route)
            else:
                route.continue_()
        page.route(history_url, hold_history)
        with page.expect_request(history_url, timeout=12000):
            agent.post(dm['id'], 'A new reply while reading earlier history')
        # Scroll only after the request has started at the bottom.
        history.evaluate('el => el.scrollTop=0')
        with page.expect_response(history_url):
            held_history[0].continue_()
        page.unroute(history_url, hold_history)
        pw.expect(page.locator('#tasks-dm-new')).to_be_visible(timeout=12000)
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 0')
        assert history.evaluate('el => el.scrollTop') < 10
        page.locator('#tasks-dm-new').click()
        page.wait_for_function("(()=>{const s=document.getElementById('tasks-dm-history');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
        # Deletions in both a loaded older page and the latest page are reconciled.
        for message in (history_messages[0], history_messages[-1]):
            owner.delete_message(dm['id'], message['id'])
        agent.post(dm['id'], 'After deleting old and recent messages')
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('After deleting old and recent messages', timeout=12000)
        for message in (history_messages[0], history_messages[-1]):
            pw.expect(page.locator(f'#tasks-dm-messages [data-mid="{message["id"]}"]')).to_have_count(0)
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 1')
        page.screenshot(path=str(tmp_path/'tasks-sidebar.png'))
        page.locator('#tasks-dm-hide').click()
        pw.expect(page.locator('#tasks-dm-history')).not_to_be_visible()
        page.locator('#tasks-agent-tab').click()
        pw.expect(draft).to_have_value('A draft for the first agent')
        page.locator('#settings-open').click()
        pw.expect(page.locator('#sbody #notify-row')).to_be_visible()
        pw.expect(page.locator('#sbody #show-private-row')).to_be_visible()
        assert page.locator('aside #notify-row').count() == 0
        page.locator('[data-tab="agents"]').click()
        page.locator('[data-tab="personal"]').click()
        pw.expect(page.locator('#sbody #notify-row')).to_be_visible()
        # A late Invites response may not replace the active Personal tab.
        delayed_invites = []
        page.route('**/v1/invites', lambda route: delayed_invites.append(route))
        with page.expect_request('**/v1/invites'):
            page.locator('[data-tab="invites"]').click()
        page.locator('[data-tab="personal"]').click()
        with page.expect_response('**/v1/invites'):
            delayed_invites[0].continue_()
        page.unroute('**/v1/invites')
        pw.expect(page.locator('#sbody #notify-row')).to_be_visible()
        page.locator('#show-private').check()
        page.wait_for_function('document.getElementById("show-private").checked')
        pw.expect(page.locator('#tasks-dm-history')).not_to_be_visible()  # Tasks only
        page.screenshot(path=str(tmp_path/'personal-settings.png'))
        # Native keyboard resizing and focusable history work without grabbing a mouse.
        page.locator('#tasks-open').click()
        handle = page.locator('#tasks-dm-resize')
        width = page.locator('#tasks-dm').bounding_box()['width']
        handle.press('ArrowLeft')
        assert page.locator('#tasks-dm').bounding_box()['width'] > width
        page.set_viewport_size({'width':900,'height':850})
        assert page.locator('#tasks-dm').bounding_box()['width'] <= 900
        agent.put_task('covered', 'Not yet presented behind the Agent pane')
        page.reload()
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(4)
        pw.expect(page.locator('#tasks-dm-composer textarea')).to_be_visible()
        # Wait for one successful list refresh; covered rows must still be unread.
        with page.expect_response(lambda r: '/v1/tasks?' in r.url and r.request.method == 'GET', timeout=12000):
            pass
        assert owner.tasks_unread() == 1
        pw.expect(page.locator('#tasks-open .count')).to_have_text('1')
        page.locator('#tasks-dm-hide').click()
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0)
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        # A list response arriving under New channel must be observed after Cancel.
        page.locator('#settings-open').click()
        agent.put_task('under-dialog', 'Unseen while New channel covers the list')
        held_tasks = []
        tasks_url = '**/v1/tasks?*'
        page.route(tasks_url, lambda route: held_tasks.append(route))
        with page.expect_request(tasks_url):
            page.locator('#tasks-open').click()
        page.locator('#new-channel').click()
        page.get_by_role('menuitem', name='New channel…', exact=True).click()
        pw.expect(page.locator('#channel-dialog')).to_be_visible()
        with page.expect_response(tasks_url):
            held_tasks[0].continue_()
        page.unroute(tasks_url)
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(5)
        assert owner.tasks_unread() == 1
        page.locator('#channel-cancel').click()
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0)
        context.close()
        mobile = browser.new_context(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
        mobile.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        mobile.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        phone = mobile.new_page()
        phone.on('pageerror', lambda e: errors.append(str(e)))
        phone.goto(env.url+'/#tasks')
        pw.expect(phone.locator('#tasks-list .tasks-row')).to_have_count(5)
        pw.expect(phone.locator('#tasks-dm')).not_to_be_visible()
        phone.locator('#tasks-agent-tab').tap()
        pw.expect(phone.locator('#tasks-dm-composer textarea')).to_be_visible()
        assert phone.locator('#tasks-dm').bounding_box()['width'] <= 390
        assert not phone.evaluate('document.documentElement.scrollWidth>innerWidth')
        phone.screenshot(path=str(tmp_path/'tasks-phone.png'))
        phone.set_viewport_size({'width':320,'height':720})
        assert not phone.evaluate('document.documentElement.scrollWidth>innerWidth')
        phone.locator('#tasks-dm-hide').tap()
        pw.expect(phone.locator('#tasks-dm')).not_to_be_visible()
        assert errors == []
        browser.close()
