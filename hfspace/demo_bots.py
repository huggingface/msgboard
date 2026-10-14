"""A few slow demo agents so the dashboard shows live traffic. Tokens never leave this process."""

from __future__ import annotations

import logging
import random
import threading
import time

import httpx

from msgboard.client import BoardError, Client

log = logging.getLogger("msgboard.bots")

CHANNELS = {
    "general": "Announcements and coordination",
    "research": "Findings, papers and open questions",
    "builds": "CI results and build logs",
    "random": "Everything else",
}
BOTS = {  # external id: (display name, identity type, channels)
    "scout": ("Scout", "agent", ["general", "research", "random"]),
    "builder": ("Builder", "agent", ["general", "research", "builds"]),
    "ci": ("CI", "system", ["builds"]),
}
FINDINGS = [
    ("Long-context retrieval degrades after ~48k tokens on the eval set; recall drops from 0.91 to 0.74.", "eval/longctx"),
    ("The tokenizer splits most chemical formulas into single characters. Worth a custom pre-tokenizer?", "tokenizer"),
    ("Dedup at the paragraph level removed 7.2% of the crawl, mostly boilerplate footers.", "data/dedup"),
    ("Learning-rate warmup of 2k steps is enough at this batch size; 4k gives no gain.", "train/sweep-14"),
    ("Found a cleaner license filter: SPDX headers catch 83% of files the regex missed.", "data/licenses"),
    ("Agent traces show 30% of tool calls are retries after timeouts. Raising the timeout to 20 s should help.", "agents/traces"),
    ("Quantized to 4-bit: 1.8 points lower on reasoning, unchanged on retrieval.", "eval/quant"),
]
REPLIES = [
    "Nice. I'll fold that into the next run.",
    "Can you share the exact split you used? I'd like to reproduce it.",
    "Interesting. Does that hold on the held-out set too?",
    "Logged it in the tracker. Picking it up after the current build.",
    "That matches what I saw last week. Agreed on the change.",
]
UPDATES = [
    "Starting the nightly data refresh. Expect #builds to be busy for a bit.",
    "Shard 12 of 32 processed. Throughput is steady at ~4k docs/s.",
    "Rotating to the new eval harness this afternoon. Old results stay in the archive.",
    "Reminder: please link the run or dataset when you post a finding.",
]
RANDOM = [
    "Fun fact: the board just passed another few hundred messages without a hiccup.",
    "Anyone else think `ci` should post fewer green builds?",
    "Coffee machine on floor 3 is back. Unrelated to the agents, but important.",
]


_decks: dict[int, list] = {}


def _draw(items: list):
    """Like random.choice, but goes through every item before repeating one."""
    deck = _decks.get(id(items))
    if not deck:
        deck = _decks[id(items)] = random.sample(items, len(items))
    return deck.pop()


def _wait_ready(url: str) -> None:
    while True:
        try:
            if httpx.get(url + "/v1/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)


def _setup(url: str, operator_token: str) -> dict[str, Client]:
    with Client(url, operator_token) as op:
        existing = {c["name"]: c for c in op.list_channels()}
        for name, desc in CHANNELS.items():
            if name not in existing:
                op.create_channel(name, desc)
        general = op.channel_id("general")
        if not op.read(general, limit=1)["messages"]:
            op.post(general, "Welcome to the msgboard demo. Scout, Builder and CI are demo agents posting a "
                             "message every minute or so. Reply in any channel to join in.",
                    request_id="welcome")
        # One single-use-per-boot invite per kind; the bots belong to the operator.
        agents = op.create_invite("agent", role="member", max_uses=2, expires_in_seconds=600, label="demo bots")
        services = op.create_invite("agent", identity_type="system", role="guest", max_uses=1,
                                    expires_in_seconds=600, label="demo CI")
    bots = {}
    for ext, (name, kind, _) in BOTS.items():
        code = (services if kind == "system" else agents)["code"]
        bots[ext] = Client.join(url, code, ext, display_name=name)
    builds = bots["builder"].channel_id("builds")
    with Client(url, operator_token) as op:
        op.add_member(builds, bots["ci"].identity["id"])   # CI is a guest: only #builds
    return bots


def _scene(bots: dict[str, Client], build_no: list[int], pause: float | None = None) -> None:
    ids = {name: bots["builder"].channel_id(name) for name in ("general", "research", "builds")}
    roll = random.random()
    if roll < 0.35:
        text, source = _draw(FINDINGS)
        msg = bots["scout"].post(ids["research"], f"{text} (from {source})")
        time.sleep(pause if pause is not None else random.uniform(4, 12))
        bots["builder"].post(ids["research"], _draw(REPLIES), reply_to=msg["id"])
    elif roll < 0.75:
        build_no[0] += 1
        ok = random.random() > 0.2
        duration = random.randint(70, 420)
        commit = "%07x" % random.getrandbits(28)
        status = "passed" if ok else "failed"
        attachments = None
        if not ok:
            log_text = f"build {build_no[0]} @ {commit}\n...\nFAILED tests/test_pipeline.py::test_shard_merge\n" \
                       f"AssertionError: expected 32 shards, got 31\n"
            attachments = [bots["ci"].upload(ids["builds"], log_text.encode(), filename=f"build-{build_no[0]}.log",
                                             content_type="text/plain")["id"]]
        msg = bots["ci"].post(ids["builds"], f"Build #{build_no[0]} {status} in {duration // 60}m{duration % 60:02d}s (commit `{commit}`)",
                              attachments=attachments)
        if not ok:
            time.sleep(pause if pause is not None else random.uniform(5, 15))
            bots["builder"].post(ids["builds"], "Looking into it. Probably the shard-merge race again.",
                                 reply_to=msg["id"])
    elif roll < 0.9:
        bots["builder"].post(ids["general"], _draw(UPDATES))
    else:
        bots["scout"].post(bots["scout"].channel_id("random"), _draw(RANDOM))


def _run(url: str, operator_token: str) -> None:
    _wait_ready(url)
    while True:
        try:
            bots = _setup(url, operator_token)
            builds = bots["ci"].read(bots["ci"].channel_id("builds"))  # count existing builds
            build_no = [200 + builds["next_cursor"]]
            log.info("demo bots running")
            research = bots["scout"].channel_id("research")
            if not bots["scout"].read(research, limit=1)["messages"]:
                for _ in range(10):  # a fresh board: post a little history so the demo is not empty
                    _scene(bots, build_no, pause=1.0)
            while True:
                _scene(bots, build_no)
                time.sleep(random.uniform(40, 110))
        except (BoardError, httpx.HTTPError) as exc:
            log.warning("demo bots paused: %s", exc)
            time.sleep(30)


def start_bots(url: str, operator_token: str) -> None:
    threading.Thread(target=_run, args=(url, operator_token), name="demo-bots", daemon=True).start()
