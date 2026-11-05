"""Real-server browser regression for reaction lists.

Install the project with .[test], install playwright, then run:
    python tests/browser_reaction_people.py
Use BROWSER_EXECUTABLE for a system Chromium. Browser downloads are not automatic.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright

from conftest import Env


def run():
    with tempfile.TemporaryDirectory(prefix="msgboard-reaction-people-") as temporary:
        env = Env(Path(temporary) / "board")
        try:
            channel = env.op.create_channel("reaction-people")
            message = env.op.post(channel["id"], "Many people, several independent reactions.")
            second = env.op.post(channel["id"], "The same emoji on another message.")
            people = []
            for i in range(60):
                account = env.human if i % 2 == 0 else env.agent
                identity, _, client = account(f"{'person' if i % 2 == 0 else 'agent'}-{i:02}")
                people.append((identity, client))
                client.react(channel["id"], message["id"], "👍")
                if i < 12:
                    client.react(channel["id"], message["id"], "👀")
                if i < 3:
                    client.react(channel["id"], message["id"], "🎉")
                if i < 2:
                    client.react(channel["id"], second["id"], "👍")
            viewer, viewer_token, _ = env.human("read-only-person", role="viewer")
            reply = env.op.post(channel["id"], "Thread reply", reply_to=message["id"])
            people[0][1].react(channel["id"], reply["id"], "👀")
            with sync_playwright() as pw:
                kwargs = {"executable_path": os.environ["BROWSER_EXECUTABLE"]} if os.getenv("BROWSER_EXECUTABLE") else {}
                browser = pw.chromium.launch(args=["--no-sandbox"], **kwargs)
                errors, writes = [], []

                def context(token, **options):
                    ctx = browser.new_context(**options)
                    ctx.add_init_script("localStorage.setItem('msgboard:token', " + json.dumps(token) + ");")
                    ctx.route("https://cdnjs.cloudflare.com/**", lambda r: r.abort())
                    p = ctx.new_page()
                    p.on("pageerror", lambda e: errors.append(str(e)))
                    p.on("request", lambda r: writes.append(r.url) if r.method == "PUT" and "/reactions/" in r.url else None)
                    p.goto(env.url + "/#reaction-people")
                    expect(p.locator("#composer")).to_be_visible()
                    return p

                page = context(env.operator_token, viewport={"width": 1280, "height": 900})

                def chip(p, mid=message["id"], emoji="👍", pane="#messages"):
                    return p.locator(f'{pane} .rchip[data-mid="{mid}"][data-react="{emoji}"]')

                def count(p, n):
                    expect(p.locator("#reaction-people [data-react-person]")).to_have_count(n)
                    expect(p.locator("#reaction-people .reaction-people-head")).to_contain_text(f"{n} reacted")

                chip(page).hover()
                count(page, 60)
                assert page.locator("#reaction-people .reaction-people-list").evaluate("el=>el.scrollHeight>el.clientHeight")
                page.locator("#reaction-people").hover()
                page.wait_for_timeout(240)
                count(page, 60)
                page.locator("#reaction-people .reaction-people-list").evaluate("el=>el.scrollTop=el.scrollHeight")
                last = page.locator("#reaction-people [data-react-person]").last
                target_id = int(last.get_attribute("data-react-person"))
                last.click()
                page.wait_for_function("id=>state.panel?.type==='profile' && state.panel.id===id", arg=target_id)
                expect(page.locator("#reaction-people")).to_have_count(0)
                expect(page.locator("#panel-scroll .namerow")).to_be_visible()
                print("PASS 60 mixed reactors, bounded scrolling, hover transfer and last reactor's profile")
                page.evaluate("closePanel()")
                for emoji, n in [("👍", 60), ("👀", 12), ("🎉", 3)]:
                    chip(page, emoji=emoji).hover()
                    count(page, n)
                    expect(page.locator("#reaction-people")).to_have_count(1)
                chip(page, second["id"]).hover()
                count(page, 2)
                print("PASS parallel emoji and identical emoji on different messages stay separate")
                # Key entry, list navigation, Escape and Tab out are all real keyboard events.
                page.mouse.move(0, 0)
                chip(page).focus()
                chip(page).press("ArrowDown")
                expect(page.locator("#reaction-people [data-react-person]").first).to_be_focused()
                page.keyboard.press("ArrowDown")
                expect(page.locator("#reaction-people [data-react-person]").nth(1)).to_be_focused()
                page.keyboard.press("Escape")
                expect(page.locator("#reaction-people")).to_have_count(0)
                expect(chip(page)).to_be_focused()
                chip(page).press("ArrowDown")
                page.locator("#reaction-people [data-react-person]").last.focus()
                page.keyboard.press("Tab")
                expect(chip(page, emoji="👀")).to_be_focused()
                count(page, 12)
                print("PASS keyboard entry, arrow navigation, Escape and Tab to next reaction")
                # Keep the panel open while the server removes/adds a reactor and the feed redraws.
                chip(page).hover()
                page.locator("#reaction-people").hover()
                people[-1][1].unreact(channel["id"], message["id"], "👍")
                count(page, 59)
                people[-1][1].react(channel["id"], message["id"], "👍")
                count(page, 60)
                before = len(writes)
                chip(page).click()
                count(page, 61)
                assert len(writes) == before + 1
                chip(page).click()
                count(page, 60)
                print("PASS live updates rebind the anchor; normal click still toggles exactly once")
                page.evaluate("closeReactionPeople()")
                page.evaluate("id=>openThread(id)", message["id"])
                expect(page.locator("#panel-composer textarea")).to_be_visible()
                chip(page, reply["id"], "👀", "#panel-scroll").hover()
                count(page, 1)
                page.locator("#reaction-people [data-react-person]").click()
                page.wait_for_function("id=>state.panel?.type==='profile' && state.panel.id===id", arg=people[0][0]["id"])
                print("PASS thread-reply list opens its reactor's profile")
                page.evaluate("closePanel()")
                # Force an anchor near a viewport edge; the list must flip and stay inside it.
                chip(page).evaluate("el=>{el.style.position='fixed';el.style.right='8px';el.style.top=(document.querySelector('#scroll').getBoundingClientRect().bottom-30)+'px';el.style.zIndex='20'}")
                chip(page).hover()
                bounds = page.locator("#reaction-people").bounding_box()
                assert bounds["x"] >= 0 and bounds["y"] >= 0
                assert bounds["x"] + bounds["width"] <= 1280 and bounds["y"] + bounds["height"] <= 900
                page.evaluate("openSettings()")
                expect(page.locator("#reaction-people")).to_have_count(0)
                print("PASS viewport-edge placement and navigation cleanup")
                reader = context(viewer_token, viewport={"width": 1280, "height": 900})
                chip(reader).focus()
                reader.keyboard.press("ArrowDown")
                count(reader, 60)
                before = len(writes)
                chip(reader).press("Space")
                assert len(writes) == before
                reader.locator("#reaction-people [data-react-person]").first.click()
                reader.wait_for_function("state.panel?.type==='profile'")
                print("PASS read-only viewers can inspect/profile without writing reactions")
                phone = context(env.operator_token, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                touch = phone.context.new_cdp_session(phone)

                def gesture(hold=False, move=False):
                    el = chip(phone)
                    el.scroll_into_view_if_needed()
                    b = el.bounding_box()
                    x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
                    touch.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                    if move:
                        touch.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - 45}]})
                    phone.wait_for_timeout(650 if hold else 40)
                    touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

                before = len(writes)
                gesture(hold=True)
                count(phone, 60)
                assert len(writes) == before, "long press toggled a reaction"
                bounds = phone.locator("#reaction-people").bounding_box()
                assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
                phone.locator("#reaction-people [data-react-person]").last.scroll_into_view_if_needed()
                phone.locator("#reaction-people [data-react-person]").last.tap()
                phone.wait_for_function("state.panel?.type==='profile'")
                phone.evaluate("closePanel()")
                gesture()
                page.wait_for_timeout(150)
                assert len(writes) == before + 1, "short tap must react exactly once"
                phone.evaluate("closeReactionPeople()")
                before = len(writes)
                gesture(hold=True, move=True)
                expect(phone.locator("#reaction-people")).to_have_count(0)
                assert len(writes) == before, "scroll gesture toggled a reaction"
                print("PASS trusted touch long-press/profile, short tap, scroll cancellation and phone fit")
                if os.getenv("REACTION_SCREENSHOT_DIR"):
                    out = Path(os.environ["REACTION_SCREENSHOT_DIR"])
                    out.mkdir(parents=True, exist_ok=True)
                    page.evaluate("id=>openChannel(id)", channel["id"])
                    chip(page).hover()
                    page.screenshot(path=str(out / "reaction-people-desktop.png"))
                    gesture(hold=True)
                    phone.screenshot(path=str(out / "reaction-people-phone.png"))
                assert not errors, errors
                print("PASS no browser page errors")
                browser.close()
        finally:
            env.close()


if __name__ == "__main__":
    run()
