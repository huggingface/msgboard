"""Two-agent demo: operator -> provisioner (launcher) -> two worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

from .client import BoardError, Client
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
            # The operator creates a restricted provisioner for the launcher.
            launcher_identity = operator.provision_agent(
                "demo-launcher", role="provisioner", identity_type="system",
                policy={"roles": ["worker"], "channels": ["demo"], "max_ttl_seconds": 3600, "max_agents": 10},
            )
            launcher_token = operator.issue_token(launcher_identity["id"], ttl_seconds=3600)["token"]

        procs = []
        with Client(server.url, launcher_token) as launcher:
            try:
                launcher.provision_agent("sneaky", role="provisioner")
            except BoardError as exc:
                print(f"[launcher] creating another provisioner refused as expected: {exc.status} {exc.code}")
            for name, mode in (("bob", "pong"), ("alice", "ping")):
                agent = launcher.provision_agent(name, role="worker", channels=["demo"], display_name=name.title())
                token = launcher.issue_token(agent["id"], ttl_seconds=600)["token"]
                # The worker only gets its own runtime token, through its environment.
                env = {k: v for k, v in os.environ.items() if not k.startswith("MSGBOARD_")}
                env.update(MSGBOARD_URL=server.url, MSGBOARD_TOKEN=token, MSGBOARD_CHANNEL="demo")
                procs.append(subprocess.Popen(
                    [sys.executable, "-m", "msgboard.demo_agent", "--mode", mode, "--rounds", "3"], env=env))
                print(f"[launcher] started {name} (agent id {agent['id']}, role worker)")

        codes = [p.wait(timeout=120) for p in procs]

        with Client(server.url, operator_token) as operator:
            page = operator.read(channel["id"], after=0, limit=100)
        print("\nChannel 'demo' history:")
        for m in page["messages"]:
            reply = f" (reply to #{m['reply_to']})" if m["reply_to"] else ""
            print(f"  seq {m['seq']:>2}  {m['sender']['external_id']:<6} {m['body']}{reply}")
    print("\nServer stopped. Run again with the same --data-dir to see history persist across restarts.")
    return 0 if all(c == 0 for c in codes) else 1
