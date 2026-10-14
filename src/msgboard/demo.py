"""Two-agent demo: the operator makes an agent invite; a launcher joins two agents with it."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

from .client import Client
from .config import OPERATOR_TOKEN_FILE
from .local import ThreadedServer


def run_demo(data_dir: str | None, port: int = 0) -> int:
    data_dir = Path(data_dir or tempfile.mkdtemp(prefix="msgboard-demo-"))
    from .cli import cmd_init

    if cmd_init(Namespace(data_dir=str(data_dir))) != 0:
        return 1
    operator_token = (data_dir / OPERATOR_TOKEN_FILE).read_text().strip()

    with ThreadedServer(data_dir, port=port) as server:
        print(f"\nBoard running at {server.url} (data in {data_dir})\n")
        with Client(server.url, operator_token) as operator:
            channel = operator.create_channel("demo", "two-agent demo")
            # The operator makes an agent invite for the launcher: agents joining with it belong to
            # the operator, see public channels and cannot create channels.
            invite = operator.create_invite("agent", role="member", max_uses=10, label="demo launcher")

        procs = []
        for name, mode in (("bob", "pong"), ("alice", "ping")):
            agent = Client.join(server.url, invite["code"], name, display_name=name.title())
            # The agent only gets its own token, through its environment.
            env = {k: v for k, v in os.environ.items() if not k.startswith("MSGBOARD_")}
            env.update(MSGBOARD_URL=server.url, MSGBOARD_TOKEN=agent.token, MSGBOARD_CHANNEL="demo")
            agent.close()
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "msgboard.demo_agent", "--mode", mode, "--rounds", "3"], env=env))
            print(f"[launcher] started {name} (agent id {agent.identity['id']}, owned by the operator)")

        codes = [p.wait(timeout=120) for p in procs]

        with Client(server.url, operator_token) as operator:
            page = operator.read(channel["id"], after=0, limit=100)
        print("\nChannel 'demo' history:")
        for m in page["messages"]:
            reply = f" (reply to #{m['reply_to']})" if m["reply_to"] else ""
            print(f"  seq {m['seq']:>2}  {m['sender']['external_id']:<6} {m['body']}{reply}")
    print("\nServer stopped. Run again with the same --data-dir to see history persist across restarts.")
    return 0 if all(c == 0 for c in codes) else 1
