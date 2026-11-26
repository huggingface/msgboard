"""Task attention, DM history and thread reading positions on a real board."""

import json
import os

import pytest

pw = pytest.importorskip("playwright.sync_api")


def test_thread_panel_preserves_channel_reading_position(env):
    _, token, owner = env.human("reader")
    channel = owner.create_channel("reading")
    messages = [owner.post(channel["id"], f"Message {i}: " + "A longer paragraph wraps differently when the thread opens. " * 16)
                for i in range(45)]
    for message in (messages[22], messages[-1]):
        owner.post(channel["id"], "A reply", reply_to=message["id"])
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
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
        page.locator('#panel-close').click()
        page.wait_for_function("({id,top}) => Math.abs(document.querySelector('#messages .msg[data-mid=\"'+id+'\"]').getBoundingClientRect().top-top)<3", arg={"id":root_id,"top":top}, timeout=3000)
        # At the end of a channel, opening and closing a thread keeps the end visible.
        page.locator('#scroll').evaluate("el => el.scrollTop=el.scrollHeight")
        last = messages[-1]["id"]
        page.locator(f'#messages .replies[data-thread="{last}"]').click()
        page.wait_for_function("(()=>{const s=document.getElementById('scroll');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
        page.locator('#panel-close').click()
        page.wait_for_function("(()=>{const s=document.getElementById('scroll');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
        assert errors == []
        browser.close()


def test_tasks_sidebar_history_drafts_attention_and_personal_settings(env, tmp_path):
    _, token, owner = env.human("task-owner", role="admin")
    channel = owner.create_channel("tasks-ui")
    a, _, agent = env.agent("first-helper", owner=owner, channels=["tasks-ui"])
    b, _, _ = env.agent("second-helper", owner=owner)
    agent.put_task("one", "First decision", channel_id=channel["id"])
    agent.put_task("two", "Second decision", channel_id=channel["id"])
    dm = owner.open_dm(a["id"])
    for i in range(46):
        agent.post(dm["id"], f"History message {i}")
    errors = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(env.url + "/#tasks-ui")
        pw.expect(page.locator('#tasks-open .count')).to_have_text('2')
        page.locator('#tasks-open').click()
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(2)
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0)
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
        draft.fill('A draft for the second agent')
        page.locator('#tasks-dm-agent').select_option(str(a['id']))
        pw.expect(draft).to_have_value('A draft for the first agent')
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 0')
        # Live additions wait behind a button; existing rows keep their order.
        third = agent.put_task('three', 'Arrived while reading', channel_id=channel['id'])
        pw.expect(page.locator('#tasks-new')).to_be_visible(timeout=12000)
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(2)
        pw.expect(page.locator('#tasks-open .count')).to_have_text('1')
        page.locator('#tasks-new').click()
        pw.expect(page.locator('#tasks-list .tasks-row')).to_have_count(3)
        pw.expect(page.locator('#tasks-open .count')).to_have_count(0)
        assert owner.task(third['id'])['state'] == 'open'
        # Loading newer messages retains the older pages and doesn't pull the reader down.
        history = page.locator('#tasks-dm-history')
        history.evaluate('el => el.scrollTop=0')
        agent.post(dm['id'], 'A new reply while reading earlier history')
        pw.expect(page.locator('#tasks-dm-new')).to_be_visible(timeout=12000)
        pw.expect(page.locator('#tasks-dm-messages')).to_contain_text('History message 0')
        assert history.evaluate('el => el.scrollTop') < 10
        page.locator('#tasks-dm-new').click()
        page.wait_for_function("(()=>{const s=document.getElementById('tasks-dm-history');return s.scrollHeight-s.scrollTop-s.clientHeight<3})()")
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
        context.close()
        mobile = browser.new_context(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
        mobile.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        mobile.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        phone = mobile.new_page()
        phone.on('pageerror', lambda e: errors.append(str(e)))
        phone.goto(env.url+'/#tasks')
        pw.expect(phone.locator('#tasks-list .tasks-row')).to_have_count(3)
        pw.expect(phone.locator('#tasks-dm')).not_to_be_visible()
        phone.locator('#tasks-agent-tab').tap()
        pw.expect(phone.locator('#tasks-dm-composer textarea')).to_be_visible()
        assert phone.locator('#tasks-dm').bounding_box()['width'] <= 390
        assert not phone.evaluate('document.documentElement.scrollWidth>innerWidth')
        phone.screenshot(path=str(tmp_path/'tasks-phone.png'))
        phone.locator('#tasks-dm-hide').tap()
        pw.expect(phone.locator('#tasks-dm')).not_to_be_visible()
        assert errors == []
        browser.close()
