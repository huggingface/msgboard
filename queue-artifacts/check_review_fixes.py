"""Browser regressions from the independent PR #38 review.

Run from a msgboard checkout with its environment's Python:
    python /path/to/check_review_fixes.py [--repo /path/to/checkout]

Uses a fresh real Env and public HTTP APIs. The stale-response check delays a
real HTTP response; it does not substitute application functions or fixtures.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from playwright.async_api import async_playwright, expect


async def check(env, owner, agent, token, agent_id, a, b):
    results = {}
    dm = owner.open_dm(agent_id)
    for number in range(45):
        agent.post(dm["id"], f"history-{number:02d}")

    async with async_playwright() as playwright:
        executable = os.environ.get(
            "QUEUE_TEST_CHROMIUM",
            "/opt/pw-browsers/chromium_headless_shell-1234/"
            "chrome-headless-shell-linux64/chrome-headless-shell",
        )
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, has_touch=True)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script('localStorage.setItem("msgboard:token", ' + json.dumps(token) + ');')
        await page.goto(env.url + "/#queue")
        await expect(page.locator("#queue-dm-composer textarea")).to_be_visible()
        messages = page.locator("#queue-dm-messages")
        await expect(messages).to_contain_text("history-44")
        assert await messages.locator("[data-mid]").count() == 40
        assert "history-00" not in await messages.inner_text()
        results["dm_loads_latest_40"] = "passed"

        await page.locator("#queue-dm-composer textarea").fill("new human send")
        await page.locator("#queue-dm-composer .btn").click()
        await expect(page.locator("#queue-dm-composer textarea")).to_have_value("")
        await expect(messages).to_contain_text("new human send")
        assert any(message["body"] == "new human send" for message in owner.read(dm["id"])["messages"])
        agent.post(dm["id"], "new incoming message")
        await page.evaluate("queueRefreshDm()")
        await expect(messages).to_contain_text("new incoming message")
        results["dm_new_sends_and_replies_visible"] = "passed"

        # Time descending, Time ascending, then the identical Custom item order.
        await page.locator("[data-queue-sort=latest]").click()
        await page.wait_for_function('queueState.sort === "latest" && queueState.direction === "desc" && !queueState.loading')
        await page.locator("[data-queue-sort=latest]").click()
        await page.wait_for_function('queueState.sort === "latest" && queueState.direction === "asc" && !queueState.loading')
        await page.locator("[data-queue-sort=manual]").click()
        await page.wait_for_function('queueState.sort === "manual" && !queueState.loading')
        assert await page.locator("#queue-list .queue-row[draggable=true]").count() == 2
        row_a = page.locator(f'.queue-row[data-qid="{a["id"]}"]')
        row_b = page.locator(f'.queue-row[data-qid="{b["id"]}"]')
        bounds = await row_b.bounding_box()
        await row_a.drag_to(row_b, target_position={"x": 100, "y": bounds["height"] - 3})
        await page.wait_for_function("queueState.items[0].id === " + str(b["id"]))
        assert [item["id"] for item in owner.queue()["items"]] == [b["id"], a["id"]]
        results["custom_mouse_drag_after_sort_switch"] = "passed"

        # Exercise the same restored row attributes through a real touch gesture.
        cdp = await context.new_cdp_session(page)
        source = await row_a.locator(".queue-row-open").bounding_box()
        target = await row_b.bounding_box()
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
            {"x": source["x"] + 90, "y": source["y"] + source["height"] / 2}
        ]})
        await asyncio.sleep(0.45)
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
            {"x": target["x"] + 90, "y": target["y"] + 3}
        ]})
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        await page.wait_for_function("queueState.items[0].id === " + str(a["id"]))
        assert [item["id"] for item in owner.queue()["items"]] == [a["id"], b["id"]]
        results["custom_touch_drag_after_sort_switch"] = "passed"

        # Native Escape closes only the inner picker; a later Escape closes the item.
        # A completed touch drag briefly suppresses its synthetic click.
        await page.wait_for_function("Date.now() >= queueIgnoreClickUntil")
        await page.locator(f'[data-queue-open="{a["id"]}"]').click()
        dialog = page.locator("#queue-dialog")
        await expect(dialog).to_be_visible()
        draft = page.locator("#queue-answer-composer textarea")
        await draft.fill("keep this answer draft")
        await page.locator("#queue-answer-composer .emoji-btn").click()
        await expect(page.locator("#queue-answer-composer .emoji-pop")).to_be_visible()
        await page.keyboard.press("Escape")
        await expect(page.locator("#queue-answer-composer .emoji-pop")).to_have_count(0)
        await expect(dialog).to_be_visible()
        await expect(draft).to_have_value("keep this answer draft")
        await expect(draft).to_be_focused()
        await page.keyboard.press("Escape")
        await expect(dialog).not_to_be_visible()
        results["emoji_escape_keeps_item_and_draft"] = "passed"

        # Hold an actual old response while a later refresh displays a new message.
        old_ready, release_old = asyncio.Event(), asyncio.Event()
        first = True

        async def delayed_response(route):
            nonlocal first
            if not first:
                await route.continue_()
                return
            first = False
            response = await route.fetch()
            old_ready.set()
            await release_old.wait()
            await route.fulfill(response=response)

        pattern = f"**/v1/channels/{dm['id']}/messages?*"
        await page.route(pattern, delayed_response)
        old_refresh = asyncio.create_task(page.evaluate("queueRefreshDm()"))
        try:
            await asyncio.wait_for(old_ready.wait(), 10)
            agent.post(dm["id"], "newest response must win")
            await page.evaluate("queueRefreshDm()")
            await expect(messages).to_contain_text("newest response must win")
        finally:
            release_old.set()
            await old_refresh
        await expect(messages).to_contain_text("newest response must win")
        await page.unroute(pattern, delayed_response)
        results["late_dm_response_cannot_replace_newer_history"] = "passed"
        assert not errors, errors
        results["browser_page_errors"] = errors
        await browser.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo.resolve() / "src"))
    sys.path.insert(0, str(args.repo.resolve() / "tests"))
    from conftest import Env

    with tempfile.TemporaryDirectory(prefix="queue-review-") as directory:
        env = Env(Path(directory) / "board")
        try:
            _, token, owner = env.human("review-owner")
            agent_identity, _, agent = env.agent("review-helper", owner=owner)
            channel = owner.create_channel("review-decisions")
            a = agent.put_queue_item("alpha", "Alpha", channel_id=channel["id"])
            b = agent.put_queue_item("beta", "Beta", channel_id=channel["id"])
            results = asyncio.run(check(env, owner, agent, token, agent_identity["id"], a, b))
            print(json.dumps(results, indent=2))
        finally:
            env.close()


if __name__ == "__main__":
    main()
