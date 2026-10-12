"""One demo agent. It only knows MSGBOARD_URL, MSGBOARD_TOKEN and MSGBOARD_CHANNEL from its environment."""

from __future__ import annotations

import argparse
import os
import sys

from .client import BoardError, Client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["ping", "pong"], required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    with Client.from_env(timeout=10) as client:
        me = client.me()
        name = me["external_id"]
        channel = client.channel_id(os.environ["MSGBOARD_CHANNEL"])

        # Workers cannot create channels or mint tokens; show that the board refuses.
        if args.mode == "ping":
            try:
                client.create_channel("not-allowed")
            except BoardError as exc:
                print(f"[{name}] create_channel refused as expected: {exc.status} {exc.code}", flush=True)
            try:
                client.issue_token(me["id"])
            except BoardError as exc:
                print(f"[{name}] issue_token for itself refused as expected: {exc.status} {exc.code}", flush=True)

        if args.mode == "ping":
            for i in range(1, args.rounds + 1):
                # A request ID derived from the agent's own state survives restarts of this process.
                sent = client.post(channel, f"ping {i}", request_id=f"{name}-ping-{i}", metadata={"round": i})
                print(f"[{name}] posted seq={sent['seq']}: {sent['body']}", flush=True)
                # Replies always come after the ping, so start looking right after it.
                for msg in client.follow(channel, after=sent["seq"], wait=10):
                    if msg["reply_to"] == sent["id"]:
                        print(f"[{name}] got reply seq={msg['seq']} from {msg['sender']['external_id']}: "
                              f"{msg['body']}", flush=True)
                        break
        else:
            answered = 0
            for msg in client.follow(channel, after=0, wait=10):
                if msg["sender"]["id"] != me["id"] and msg["body"].startswith("ping"):
                    reply = client.post(channel, msg["body"].replace("ping", "pong"), reply_to=msg["id"],
                                        request_id=f"{name}-reply-{msg['id']}")
                    print(f"[{name}] replied seq={reply['seq']}: {reply['body']}", flush=True)
                    answered += 1
                    if answered == args.rounds:
                        break
    return 0


if __name__ == "__main__":
    sys.exit(main())
