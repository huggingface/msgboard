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


def complete(input, before, after, key="Tab"):
    input.fill(before)
    input.press(key)
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
        complete(input, shortcode[:-1], emoji + " ")
    for shortcut, emoji in [(":D", "😄"), (":-D", "😄"), (":)", "🙂"), (":-)", "🙂"),
                            (";)", "😉"), (";-)", "😉"), (":(", "🙁"), (":-(", "🙁"),
                            ("<3", "❤️"), (":thumbs_up:", "👍"), (":rocket_ship_launch:", "🚀")]:
        space(input, shortcut, emoji + " ")
    input.fill(":")
    pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
    input.fill(":r")
    suggestions = page.get_by_role("listbox", name="Emoji suggestions")
    pw.expect(suggestions).to_be_visible()
    assert 1 <= suggestions.get_by_role("option").count() <= 7
    assert len(set(suggestions.locator(".glyph").all_text_contents())) == suggestions.get_by_role("option").count()
    input.fill("Choose :ro")
    assert all(name.startswith(":ro") for name in suggestions.locator(".opt > span:nth-child(2)").all_text_contents())
    input.press("ArrowDown")
    choice = suggestions.locator('[aria-selected="true"] .glyph').inner_text()
    input.press("Enter")
    pw.expect(input).to_have_value("Choose " + choice + " ")
    assert owner.read(channel["id"])["messages"] == []
    input.fill("")
    input.press_sequentially("Looks good :D")
    input.press("Tab")
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
    input.fill("Send this literal :coffee")
    pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).to_be_visible()
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        page.locator("#composer .btn").click()
    pw.expect(input).to_have_value("")
    pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
    input.press("Tab")
    pw.expect(input).to_have_value("")
    assert owner.read(channel["id"])["messages"][-1]["body"] == "Send this literal :coffee"


def test_literal_contexts_selection_composition_paste_caret_and_undo(dashboard):
    page, owner, _, channel, _ = dashboard
    input = page.locator("#composer textarea")
    for before in [
        "`:D", "``some ` :rocket:", "```text\n:D", "~~~\n:rocket:",
        "> ```\n:D", "- ~~~\n  :D", "    :rocket:", "\t:D", "\\:D", "prefix:D",
        "https://example.com/:rocket:", "<https://example.com/ :D",
        "https://example.com/(:rocket", "ftp://example.com/(:rocket",
        "//example.com/(:rocket", "/relative/(:rocket", "www.example.com/(:rocket",
        "mailto:person@example.com/(:rocket", "See (https://example.com/(:rocket",
        'See "https://example.com/(:rocket', "See 'https://example.com/(:rocket",
        "See *https://example.com/(:rocket", "See **https://example.com/(:rocket",
        "See _https://example.com/(:rocket", "See ~~https://example.com/(:rocket",
        'See (**"https://example.com/(:rocket', "See “https://example.com/(:rocket",
        "See ‘https://example.com/(:rocket", "See './files/(:rocket",
        "[link]( :rocket:", "[link](url \"title :D", "[ref]: :rocket:",
        ":unknown:", ":smile:",
    ]:
        input.fill(before)
        pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
        input.press("Tab")
        pw.expect(input).to_have_value(before)
        input.press("Space")
        pw.expect(input).to_have_value(before + " ")
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
        "- ~~~\n  code\n:D ",
        "1. ~~~\n   code\n:D ",
        "- ~~~\n  code\nlazy\n:D \n  ~~~",
        "- ~~~\n  - ~~~\n:D \n  ~~~",
        '[ref]: url "title\n[inner]: something\ntext :D "\n\n[link][ref]',
        "[ref]: url 'title\n[inner]: something\ntext :D '\n\n[link][ref]",
        "[book](url (O'Reilly :D ))",
        '[book](url (title "quoted" :D ))',
        '[book](url\n "title ) :D ")',
    ]:
        input.fill(body)
        caret = body.index(":D") + 2
        input.evaluate("(el, pos) => el.setSelectionRange(pos, pos)", caret)
        input.dispatch_event("click")
        pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
        input.press("Space")
        pw.expect(input).to_have_value(body[:caret] + " " + body[caret:])
    for before in [
        "> ~~~\n> code\n> ~~~\n:D", "- ~~~\n  code\n  ~~~\n:D",
        "- > ~~~\n  > code\n  > ~~~\n:D", "> ~~~\n> code\n\n:D",
        '[link](url "title )") :D', "[link](<https://example.com/)path>) :D",
        '[ref]: url\n "title )"\n\n:D',
        "- ~~~\n:D", "- ~~~\n  code\nlazy\n  ~~~\n:D",
        "- ~~~\n  code\n\n:D", "- ~~~\n  code\n# heading\n:D",
        "1. ~~~\n   code\n   ~~~\n:D",
        '[ref]: url "title\n[inner]: something\nclosed"\n\n:D',
        "[book](https://example.com/O'Reilly) :D",
        "[book](url (title O'Reilly)) :D", '[book](url (title "quoted")) :D',
        "[book](https://example.com/O'Reilly (title)) :D",
        "[book](url 'title \"quoted\"') :D", '[book]( "empty URL title") :D',
        '[book](https://example.com/path(foo) "title )") :D',
        '[book](https://example.com/a"b) :D', '[book]("url") :D',
        "O'Reilly :D", "[not a link](words with spaces) :D",
        "[book](url\n (title O'Reilly)) :D",
    ]:
        space(input, before, before[:-2] + "😄 ")

    input.fill("hello :rocket: tail")
    input.evaluate("el => el.setSelectionRange(14, 14)")
    input.press("Space")
    pw.expect(input).to_have_value("hello 🚀  tail")
    assert input.evaluate("el => el.selectionStart === 9 && el.selectionEnd === 9")
    input.fill("hello :rocket tail")
    input.evaluate("el => el.setSelectionRange(13, 13)")
    input.dispatch_event("click")
    input.press("Tab")
    pw.expect(input).to_have_value("hello 🚀  tail")
    assert input.evaluate("el => el.selectionStart === 9 && el.selectionEnd === 9")
    input.fill(":ro")
    input.press("Home")
    pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
    body = "First line\nSecond line\nchoose :rocket"
    for key in ["PageUp", "Control+ArrowUp"]:
        input.fill(body)
        pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).to_be_visible()
        input.press(key)
        assert input.evaluate("el => el.selectionStart") < len(body)
        pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
        input.press("Tab")
        pw.expect(input).to_have_value(body)
    # Caret movement can precede keyup; stale Enter must neither complete nor send.
    input.fill(body)
    page.keyboard.down("PageUp")
    input.press("Enter")
    page.keyboard.up("PageUp")
    pw.expect(input).to_have_value(body)
    assert owner.read(channel["id"])["messages"] == []
    complete(input, "Looks (:rocket", "Looks (🚀 ")
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
    input.fill("")
    input.press_sequentially(":rocket")
    input.press("Tab")
    pw.expect(input).to_have_value("🚀 ")
    input.press("Control+z")
    pw.expect(input).to_have_value(":rocket")
    input.press("Control+y")
    pw.expect(input).to_have_value("🚀 ")
    input.fill(":ro")
    input.press("Escape")
    pw.expect(page.get_by_role("listbox", name="Emoji suggestions")).not_to_be_visible()
    pw.expect(input).to_have_value(":ro")


def test_thread_edit_dm_and_tasks_composers(dashboard, tmp_path):
    page, owner, helper, channel, helper_id = dashboard
    root = owner.post(channel["id"], "Review this change")
    page.reload()
    message = page.locator(f'#messages .msg[data-mid="{root["id"]}"]')
    message.hover()
    message.locator("[data-thread]").click()
    thread = page.locator("#panel-composer textarea")
    complete(thread, "Ready :rocket", "Ready 🚀 ", key="Enter")
    assert len(owner.read(channel["id"])["messages"]) == 1
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        thread.press("Enter")
    assert owner.read(channel["id"])["messages"][-1]["reply_to"] == root["id"]
    page.locator("#panel-close").click()
    message.hover()
    message.locator("[data-edit]").click()
    edit = page.get_by_role("textbox", name="Edit message")
    complete(edit, "Edited :D", "Edited 😄 ", key="Enter")
    assert owner.read(channel["id"])["messages"][0]["body"] == "Review this change"
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/messages/" in r.url):
        edit.press("Enter")
    assert owner.read(channel["id"])["messages"][0]["body"] == "Edited 😄"

    dm = owner.open_dm(helper_id["id"])
    helper.post(dm["id"], "Hello")
    page.reload()
    page.locator("#dms button").filter(has_text="helper").click()
    complete(page.locator("#composer textarea"), ":coffee", "☕ ")

    item = helper.put_task("emoji-answer", "Emoji answer check", channel_id=channel["id"])
    page.goto(page.url.split("#")[0] + "#tasks/" + str(item["id"]))
    answer = page.locator("#tasks-answer-composer textarea")
    complete(answer, "Approved :thumbs_up", "Approved 👍 ")
    with page.expect_response(lambda r: r.request.method == "POST" and "/actions" in r.url):
        page.locator("#tasks-answer-composer .btn").click()
    assert owner.task(item["id"])["state"] == "closed"
    assert owner.read(channel["id"])["messages"][-1]["body"] == "Approved 👍"
    tasks_dm = page.locator("#tasks-dm-composer textarea")
    complete(tasks_dm, ":wave_hello", "👋 ")
    page.screenshot(path=str(tmp_path / "emoji-tasks.png"))

    # The same beforeinput path works without a physical Space key on a touch layout.
    page.set_viewport_size({"width": 390, "height": 844})
    tasks_dm.fill(":rocket:")
    page.keyboard.insert_text(" ")
    pw.expect(tasks_dm).to_have_value("🚀 ")

    touch = page.context.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    touch.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(owner.token) + ")")
    touch.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
    phone = touch.new_page()
    phone.goto(page.url.split("#")[0] + "#emoji-checks")
    field = phone.locator("#composer textarea")
    field.fill(":rocket")
    phone.get_by_role("option").filter(has_text=":rocket:").tap()
    pw.expect(field).to_have_value("🚀 ")
    field.press("Enter")
    pw.expect(field).to_have_value("🚀 \n")
    touch.close()
