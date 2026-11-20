"""Link button in the composer, and pasting a URL over selected text (browser; needs the optional browser extra)."""

import json
import os

import pytest

pw = pytest.importorskip("playwright.sync_api")

MOD = "Meta" if os.uname().sysname == "Darwin" else "Control"


@pytest.fixture
def dashboard(env):
    _, token, owner = env.human("alex")
    channel = owner.create_channel("link-checks")
    helper_id, _, helper = env.agent("helper", owner=owner, channels=[channel["name"]])
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ")")
        page = context.new_page()
        page.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page.goto(env.url + "/#link-checks")
        pw.expect(page.locator("#composer textarea")).to_be_visible()
        yield page, owner, helper, channel
        context.close()
        browser.close()


def select(input, value, start, end):
    input.fill(value)
    input.evaluate("(el, [s, e]) => { el.focus(); el.setSelectionRange(s, e); }", [start, end])


def paste(input, text):
    """Fire a paste with text/plain; returns True when the page took it over (preventDefault)."""
    return input.evaluate("""(el, text) => {
        const dt = new DataTransfer(); dt.setData("text/plain", text);
        const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
        el.dispatchEvent(ev); return ev.defaultPrevented; }""", text)


def test_button_sits_between_emoji_and_attach(dashboard):
    page, *_ = dashboard
    order = page.locator("#composer .bar .left > *").evaluate_all(
        "els => els.map(e => e.classList.contains('emoji-btn') ? 'emoji' : e.classList.contains('link-btn') ? 'link' : e.className)")
    assert order[:3] == ["emoji", "link", "attach"]
    btn = page.locator("#composer .link-btn")
    assert btn.get_attribute("aria-label") == "Add link"
    assert "Shift+U" in btn.get_attribute("title")


def test_selected_text_becomes_link_text_and_undo_restores_it(dashboard, tmp_path):
    page, owner, _, channel = dashboard
    input = page.locator("#composer textarea")
    before = "Here are the eval results for run 12"
    select(input, before, 9, 26)                       # "the eval results "
    page.locator("#composer .link-btn").click()
    pop = page.get_by_role("dialog", name="Add link")
    pw.expect(pop.locator(".link-text")).to_have_value("the eval results")
    pw.expect(pop.locator(".link-url")).to_be_focused()
    pw.expect(pop.locator(".link-save")).to_be_disabled()
    pop.locator(".link-url").fill("not a url")
    pw.expect(pop.locator(".link-save")).to_be_disabled()
    pop.locator(".link-url").fill("https://huggingface.co/datasets/org/evals")
    page.screenshot(path=str(tmp_path / "link-pop.png"))
    pop.locator(".link-url").press("Enter")
    pw.expect(pop).not_to_be_visible()
    after = "Here are [the eval results](https://huggingface.co/datasets/org/evals) for run 12"
    pw.expect(input).to_have_value(after)
    pw.expect(input).to_be_focused()
    page.screenshot(path=str(tmp_path / "link-inserted.png"))
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value(before)
    input.press(f"{MOD}+Shift+z")
    pw.expect(input).to_have_value(after)
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        input.press("Enter")
    assert owner.read(channel["id"])["messages"][-1]["body"] == after


def test_empty_text_bare_domain_selected_url_and_cancel(dashboard):
    page, *_ = dashboard
    input = page.locator("#composer textarea")
    pop = page.get_by_role("dialog", name="Add link")
    # nothing selected: Text starts focused; leaving it empty inserts just the URL; a bare domain gets https://
    select(input, "see ", 4, 4)
    page.locator("#composer .link-btn").click()
    pw.expect(pop.locator(".link-text")).to_be_focused()
    pop.locator(".link-url").fill("example.com/a b")
    pw.expect(pop.locator(".link-save")).to_be_disabled()      # spaces: not a URL
    pop.locator(".link-url").fill("example.com/docs")
    pop.locator(".link-save").click()
    pw.expect(input).to_have_value("see https://example.com/docs")
    # a selected URL goes into Link
    select(input, "go to https://hf.co/x now", 6, 21)
    input.press(f"{MOD}+Shift+u")                               # the shortcut opens the same box
    pw.expect(pop.locator(".link-url")).to_have_value("https://hf.co/x")
    pw.expect(pop.locator(".link-text")).to_have_value("")
    pop.locator(".link-text").fill("this")
    pop.locator(".link-text").press("Enter")
    pw.expect(input).to_have_value("go to [this](https://hf.co/x) now")
    # Escape and Cancel leave the text alone and put the selection back
    select(input, "keep me", 0, 4)
    page.locator("#composer .link-btn").click()
    pop.locator(".link-url").press("Escape")
    pw.expect(pop).not_to_be_visible()
    pw.expect(input).to_have_value("keep me")
    assert input.evaluate("el => [el.selectionStart, el.selectionEnd]") == [0, 4]
    page.locator("#composer .link-btn").click()
    pop.locator(".link-cancel").click()
    pw.expect(pop).not_to_be_visible()
    # editing the text while the box is open closes it, so Save can never overwrite newer text
    select(input, "the docs", 0, 8)
    page.locator("#composer .link-btn").click()
    pop.locator(".link-url").fill("https://example.com/keyboard")
    input.evaluate("el => { el.focus(); el.setSelectionRange(0, 0); }")
    input.press_sequentially("PREFIX ")
    pw.expect(pop).not_to_be_visible()
    pw.expect(input).to_have_value("PREFIX the docs")
    # clicking the button again closes the box
    page.locator("#composer .link-btn").click()
    page.locator("#composer .link-btn").click()
    pw.expect(pop).not_to_be_visible()


def test_link_text_and_targets_survive_markdown(dashboard):
    from markdown_it import MarkdownIt
    page, *_ = dashboard
    input = page.locator("#composer textarea")
    pop = page.get_by_role("dialog", name="Add link")
    cases = [  # text, link typed, Markdown inserted, link text and target as rendered
        ("[draft] notes", "https://en.wikipedia.org/wiki/Foo_(bar)",
         "[&#91;draft&#93; notes](https://en.wikipedia.org/wiki/Foo_%28bar%29)",
         "[draft] notes", "https://en.wikipedia.org/wiki/Foo_%28bar%29"),
        ("a ] b \\ c", "https://a.co/x", "[a &#93; b &#92; c](https://a.co/x)", "a ] b \\ c", "https://a.co/x"),
        # a backslash is a slash to a browser: the link keeps pointing at good.example
        ("docs", "https://good.example\\@evil.example/",
         "[docs](https://good.example/@evil.example/)", "docs", "https://good.example/@evil.example/"),
        ("q", "https://x.co/?a=1&copy;=2", "[q](https://x.co/?a=1&amp;copy;=2)", "q", "https://x.co/?a=1&amp;copy;=2"),
        # text that looks like another link stays text: the only link goes where the box says
        ("see [inner](https://other.example) <https://other2.example>", "https://dest.example",
         "[see &#91;inner&#93;(https://other.example) &lt;https://other2.example&gt;](https://dest.example/)",
         "see [inner](https://other.example) &lt;https://other2.example&gt;", "https://dest.example/"),
    ]
    for text, url, inserted, shown, href in cases:
        select(input, text, 0, len(text))
        page.locator("#composer .link-btn").click()
        pop.locator(".link-url").fill(url)
        pop.locator(".link-save").click()
        pw.expect(input).to_have_value(inserted)
        # CommonMark reads it as one link with the intended text and target, even next to a reference definition
        html = MarkdownIt().render(inserted + "\n\n[inner]: https://other.example\n[draft]: https://other.example")
        assert f'<a href="{href}">{shown}</a>' in html and html.count("<a ") == 1, inserted


def test_paste_url_over_selection(dashboard):
    page, *_ = dashboard
    input = page.locator("#composer textarea")
    select(input, "read the docs please", 5, 14)               # "the docs " (trailing space stays outside)
    assert paste(input, "  https://hf.co/docs\n")
    pw.expect(input).to_have_value("read [the docs](https://hf.co/docs) please")
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value("read the docs please")
    # not taken over: plain text, no selection, a selected URL, or a selection across lines
    select(input, "read the docs", 5, 13)
    assert not paste(input, "the guide")
    select(input, "https://a.co", 0, 12)
    assert not paste(input, "https://b.co")
    select(input, "one\ntwo", 0, 7)
    assert not paste(input, "https://hf.co")
    # inside code or a link target the selection is simply replaced, as usual
    for value, start, end in [("`curl ORIGINAL`", 6, 14), ("```\nORIGINAL\n```", 4, 12), ("[x](ORIGINAL)", 4, 12), ("a `b` c", 0, 7)]:
        select(input, value, start, end)
        assert not paste(input, "https://example.com"), value


def test_paste_url_with_nothing_selected(dashboard):
    page, owner, _, channel = dashboard
    input = page.locator("#composer textarea")
    sel = lambda: input.evaluate("el => el.value.slice(el.selectionStart, el.selectionEnd)")
    sent = lambda: owner.read(channel["id"])["messages"][-1]["body"]

    def send():
        with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
            input.press("Enter")

    def pasted(before, url="https://hf.co/x"):
        select(input, before, len(before), len(before))
        assert paste(input, url)
        pw.expect(input).to_have_value(f"{before}[{url}]({url})")
        assert sel() == url

    # [url](url) with the text selected: typing a word renames the link
    pasted("see ")
    input.press_sequentially("the docs")
    pw.expect(input).to_have_value("see [the docs](https://hf.co/x)")
    send()
    assert sent() == "see [the docs](https://hf.co/x)"

    # anything else turns it straight back into the plain URL, so the box holds what will be sent
    pasted("see ")
    input.press_sequentially(" now")
    pw.expect(input).to_have_value("see https://hf.co/x now")
    pasted("")
    input.press(".")
    pw.expect(input).to_have_value("https://hf.co/x.")
    pasted("")
    input.press("Shift+Enter")
    pw.expect(input).to_have_value("https://hf.co/x\n")
    pasted("a ")
    input.press("ArrowLeft")
    pw.expect(input).to_have_value("a https://hf.co/x")
    pasted("b ")
    page.locator(".topbar").first.click()    # leaving the box; undo still works afterwards
    pw.expect(input).to_have_value("b https://hf.co/x")
    input.focus()
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value("b [https://hf.co/x](https://hf.co/x)")
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value("b ")
    # sending right away sends the plain URL; Markdown already written with the same URL is left alone
    pasted("![https://hf.co/x](https://hf.co/x) and ")
    send()
    assert sent() == "![https://hf.co/x](https://hf.co/x) and https://hf.co/x"

    # Backspace takes the whole paste out (never an invisible [](url) link); undo takes it back
    pasted("x ")
    input.press("Backspace")
    pw.expect(input).to_have_value("x ")
    pasted("undo ")
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value("undo ")
    # an emptied link, however it happened, is sent with its URL as the text; code and images are left alone
    pasted("c ")
    input.press("x")
    input.press(f"{MOD}+z")
    pw.expect(input).to_have_value("c [https://hf.co/x](https://hf.co/x)")
    input.evaluate("el => el.setSelectionRange(3, 18)")
    input.press("Backspace")
    pw.expect(input).to_have_value("c [](https://hf.co/x)")
    send()
    assert sent() == "c [https://hf.co/x](https://hf.co/x)"
    for blank in [" ", "\n"]:
        input.fill(f"d [{blank}](https://hf.co/y)")
        send()
        assert sent() == "d [https://hf.co/y](https://hf.co/y)", blank
    for kept in ["`[](https://a.co)` ![](https://b.co)", "\\[](https://c.co)", "Literal\\\\[](https://c.co)"]:
        input.fill(kept)
        send()
        assert sent() == kept

    # inside code, or in a link target, the URL pastes as is
    for before in ["run `curl ", "```\n", "[docs]("]:
        select(input, before, len(before), len(before))
        assert not paste(input, "https://hf.co/e"), before

    # the saved draft holds the plain URL, so reopening it never brings back the temporary Markdown
    pasted("r ")
    page.wait_for_function("() => (localStorage.getItem('msgboard:drafts') || '').includes('r https://hf.co/x')")
    page.reload()
    pw.expect(page.locator("#composer textarea")).to_have_value("r https://hf.co/x")


def test_thread_and_tasks_composers_have_it(dashboard):
    page, owner, helper, channel = dashboard
    root = owner.post(channel["id"], "Review this change")
    page.reload()
    message = page.locator(f'#messages .msg[data-mid="{root["id"]}"]')
    message.hover()
    message.locator("[data-thread]").click()
    thread = page.locator("#panel-composer textarea")
    select(thread, "the PR", 0, 6)
    page.locator("#panel-composer .link-btn").click()
    pop = page.locator("#panel-composer").get_by_role("dialog", name="Add link")
    pop.locator(".link-url").fill("https://github.com/org/repo/pull/1")
    pop.locator(".link-url").press("Enter")
    pw.expect(thread).to_have_value("[the PR](https://github.com/org/repo/pull/1)")
    pw.expect(page.locator("#composer textarea")).to_have_value("")   # the channel box is untouched
    with page.expect_response(lambda r: r.request.method == "POST" and "/messages" in r.url):
        thread.press("Enter")
    reply = owner.read(channel["id"])["messages"][-1]
    assert reply["reply_to"] == root["id"] and reply["body"] == "[the PR](https://github.com/org/repo/pull/1)"
    page.locator("#panel-close").click()

    item = helper.put_task("link-answer", "Link answer check", channel_id=channel["id"])
    page.goto(page.url.split("#")[0] + "#tasks/" + str(item["id"]))
    answer = page.locator("#tasks-answer-composer textarea")
    select(answer, "see notes", 4, 9)
    assert paste(answer, "https://hf.co/notes")
    pw.expect(answer).to_have_value("see [notes](https://hf.co/notes)")
    pw.expect(page.locator("#tasks-answer-composer .link-btn")).to_be_visible()
