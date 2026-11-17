"""Browser regressions against a real board; install the optional browser extra to run."""

import json
import os
import re

import pytest

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def dashboard(env):
    _, token, owner = env.human("alex")
    channel = owner.create_channel("emoji-checks")
    helper_id, _, helper = env.agent("helper", owner=owner, channels=[channel["name"]])
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        page = context.new_page()
        # Conversion must work even when the optional Markdown CDN is unavailable.
        page.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page.goto(env.url + "/#emoji-checks")
        pw.expect(page.locator("#composer textarea")).to_be_visible()
        yield page, owner, helper, channel, helper_id
        context.close()
        browser.close()


def space(input, before, after):
    input.fill(before)
    input.press("Space")
    pw.expect(input).to_have_value(after)


def test_every_picker_entry_has_a_working_shortcode_and_saves_its_draft(dashboard, tmp_path):
    page, owner, _, channel, _ = dashboard
    input = page.locator("#composer textarea")
    page.locator("#composer .emoji-btn").click()
    entries = page.locator(".emoji-pop [data-e]").evaluate_all(
        "buttons => buttons.map(b => [b.dataset.e, b.title])")
    assert entries
    page.locator(".emoji-pop input").press("Escape")
    for emoji, title in entries:
        shortcode = re.search(r"(:[a-z0-9_+\-]+:) \+ Space$", title)[1]
        space(input, shortcode, emoji + " ")
    for shortcut, emoji in [(":D", "😄"), (":-D", "😄"), (":)", "🙂"), (":-)", "🙂"),
                            (";)", "😉"), (";-)", "😉"), (":(", "🙁"), (":-(", "🙁"),
                            ("<3", "❤️"), (":thumbs_up:", "👍"), (":rocket_ship_launch:", "🚀")]:
        space(input, shortcut, emoji + " ")
    input.fill("")
    input.press_sequentially("Looks good :D ")
    pw.expect(input).to_have_value("Looks good 😄 ")
    page.wait_for_function("key => JSON.parse(localStorage.getItem('msgboard:drafts') || '{}')[key]?.text === 'Looks good 😄 '", arg=f'c:{channel["id"]}')
    page.reload()
    pw.expect(input).to_have_value("Looks good 😄 ")
    page.screenshot(path=str(tmp_path / "emoji-main.png"))
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        input.press("Enter")
    assert owner.read(channel["id"])["messages"][-1]["body"] == "Looks good 😄"
    pw.expect(input).to_have_value("")

    page.locator("#composer .emoji-btn").click()
    page.locator(".emoji-pop input").fill(":thumbs_up:")
    page.locator('.emoji-pop [data-e="👍"]').click()
    pw.expect(input).to_have_value("👍")
    input.fill("")
    input.press_sequentially("@help")
    pw.expect(page.locator("#composer .mention-pop")).to_be_visible()
    input.press("Tab")
    pw.expect(input).to_have_value("@helper ")


def test_literal_contexts_selection_composition_paste_caret_and_undo(dashboard):
    page, _, _, _, _ = dashboard
    input = page.locator("#composer textarea")
    for before in [
        "`:D", "``some ` :rocket:", "```text\n:D", "~~~\n:rocket:",
        "> ```\n:D", "- ~~~\n  :D", "    :rocket:", "\t:D", "\\:D", "prefix:D",
        "https://example.com/:rocket:", "<https://example.com/ :D",
        "[link]( :rocket:", "[link](url \"title :D", "[ref]: :rocket:",
        ":unknown:", ":smile:",
    ]:
        space(input, before, before + " ")
    for before in ["`literal` :D", "```\ncode\n```\n:D", "~~~\ncode\n~~~\n:D", "[link](url) :D"]:
        space(input, before, before[:-2] + "😄 ")

    # Edit inside completed blocks/titles too: closing syntax remains after the caret.
    for body in [
        "~~~text\n- ~~~\n:D \n~~~",
        "~~~text\n> ~~~\n:D \n~~~",
        "- > ~~~text\n  > :D \n  > ~~~",
        "> > ~~~text\n> > - ~~~\n> > :D \n> > ~~~",
        "- - ~~~text\n    :D \n    ~~~",
        '[link](https://example.com "title ) :D ")',
        "[link](https://example.com 'title ) :D ')",
        '[link](<https://example.com/)path> "title :D ")',
        '[ref]: https://example.com\n  "title )\n:D "\n\n[link][ref]',
        "[ref]: <https://example.com/)path>\n  'title )\n:D '\n\n[link][ref]",
        '[ref]:\n https://example.com\n "title )\n:D "\n\n[link][ref]',
    ]:
        input.fill(body)
        caret = body.index(":D") + 2
        input.evaluate("(el, pos) => el.setSelectionRange(pos, pos)", caret)
        input.press("Space")
        pw.expect(input).to_have_value(body[:caret] + " " + body[caret:])
    for before in [
        "> ~~~\n> code\n> ~~~\n:D", "- ~~~\n  code\n  ~~~\n:D",
        "- > ~~~\n  > code\n  > ~~~\n:D", "> ~~~\n> code\n\n:D",
        '[link](url "title )") :D', "[link](<https://example.com/)path>) :D",
        '[ref]: url\n "title )"\n\n:D',
    ]:
        space(input, before, before[:-2] + "😄 ")

    input.fill("hello :rocket: tail")
    input.evaluate("el => el.setSelectionRange(14, 14)")
    input.press("Space")
    pw.expect(input).to_have_value("hello 🚀  tail")
    assert input.evaluate("el => el.selectionStart === 9 && el.selectionEnd === 9")
    input.fill(":Dtail")
    input.evaluate("el => el.setSelectionRange(2, 2)")
    input.press("Space")
    pw.expect(input).to_have_value(":D tail")
    input.fill(":Dxx")
    input.evaluate("el => el.setSelectionRange(2, 4)")
    input.press("Space")
    pw.expect(input).to_have_value(":D ")

    input.fill(":D")
    input.dispatch_event("compositionstart")
    page.keyboard.insert_text(" ")
    pw.expect(input).to_have_value(":D ")
    input.dispatch_event("compositionend")
    input.fill(":rocket:")
    input.dispatch_event("keydown", {"key": " ", "ctrlKey": True})
    page.keyboard.insert_text(" ")
    pw.expect(input).to_have_value(":rocket: ")
    input.dispatch_event("keyup")

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("navigator.clipboard.writeText(':D :rocket: ')")
    input.fill("")
    input.press("Control+v")
    pw.expect(input).to_have_value(":D :rocket: ")
    input.press("Control+a")
    input.press("Backspace")
    input.press_sequentially(":D ")
    pw.expect(input).to_have_value("😄 ")
    input.press("Control+z")
    pw.expect(input).to_have_value(":D ")
    input.press("Control+y")
    pw.expect(input).to_have_value("😄 ")


def test_thread_edit_dm_and_tasks_composers(dashboard, tmp_path):
    page, owner, helper, channel, helper_id = dashboard
    root = owner.post(channel["id"], "Review this change")
    page.reload()
    message = page.locator(f'#messages .msg[data-mid="{root["id"]}"]')
    message.hover()
    message.locator("[data-thread]").click()
    thread = page.locator("#panel-composer textarea")
    space(thread, "Ready :rocket:", "Ready 🚀 ")
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        thread.press("Enter")
    assert owner.read(channel["id"])["messages"][-1]["reply_to"] == root["id"]
    page.locator("#panel-close").click()
    message.hover()
    message.locator("[data-edit]").click()
    edit = page.get_by_role("textbox", name="Edit message")
    space(edit, "Edited :D", "Edited 😄 ")
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/messages/" in r.url):
        edit.press("Enter")
    assert owner.read(channel["id"])["messages"][0]["body"] == "Edited 😄"

    dm = owner.open_dm(helper_id["id"])
    helper.post(dm["id"], "Hello")
    page.reload()
    page.locator("#dms button").filter(has_text="helper").click()
    space(page.locator("#composer textarea"), ":coffee:", "☕ ")

    item = helper.put_task("emoji-answer", "Emoji answer check", channel_id=channel["id"])
    page.goto(page.url.split("#")[0] + "#tasks/" + str(item["id"]))
    answer = page.locator("#tasks-answer-composer textarea")
    space(answer, "Approved :thumbs_up:", "Approved 👍 ")
    with page.expect_response(lambda r: r.request.method == "POST" and "/actions" in r.url):
        page.locator("#tasks-answer-composer .btn").click()
    assert owner.task(item["id"])["state"] == "closed"
    assert owner.read(channel["id"])["messages"][-1]["body"] == "Approved 👍"
    tasks_dm = page.locator("#tasks-dm-composer textarea")
    space(tasks_dm, ":wave_hello:", "👋 ")
    page.screenshot(path=str(tmp_path / "emoji-tasks.png"))

    # The same beforeinput path works without a physical Space key on a touch layout.
    page.set_viewport_size({"width": 390, "height": 844})
    tasks_dm.fill(":rocket:")
    page.keyboard.insert_text(" ")
    pw.expect(tasks_dm).to_have_value("🚀 ")
