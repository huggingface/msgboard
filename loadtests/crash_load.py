from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp


async def run(data_dir):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    initializer = await asyncio.create_subprocess_exec(sys.executable, "-m", "msgboard", "init", "--data-dir", str(data_dir),
                                                      stdout=asyncio.subprocess.DEVNULL)
    assert await initializer.wait() == 0
    headers = {"Authorization": "Bearer " + (data_dir / "operator.token").read_text().strip()}
    url = f"http://127.0.0.1:{port}"

    async def start():
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "msgboard", "serve",
            "--data-dir", str(data_dir), "--port", str(port), "--log-level", "warning")
        async with aiohttp.ClientSession() as client:
            for attempt in range(200):
                try:
                    async with client.get(url + "/v1/health") as response:
                        if response.status == 200:
                            return process
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(.05)
        process.kill()
        await process.wait()
        raise RuntimeError("server failed to start")

    process = await start()
    results = {"environment": "local process, local disk; not a Space restart test", "cycles": []}
    try:
        async with aiohttp.ClientSession(base_url=url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.post("/v1/channels", json={"name": "crash-under-load"}) as response:
                channel = await response.json()
            endpoint = f"/v1/channels/{channel['id']}/messages"
            for cycle in range(3):
                acknowledged = {}
                uncertain = []
                stop = asyncio.Event()
                body = "a" * 2048

                async def writer():
                    while not stop.is_set():
                        request_id = uuid.uuid4().hex
                        try:
                            async with client.post(endpoint, json={"body": body},
                                                   headers={"Idempotency-Key": request_id}) as response:
                                if response.status == 201:
                                    acknowledged[request_id] = (await response.json())["seq"]
                                else:
                                    uncertain.append(request_id)
                        except aiohttp.ClientError:
                            uncertain.append(request_id)

                writers = [asyncio.create_task(writer()) for index in range(16)]
                await asyncio.sleep(3)
                stop.set()
                process.kill()
                await process.wait()
                await asyncio.gather(*writers)
                process = await start()
                connection = sqlite3.connect(data_dir / "board.db")
                try:
                    stored = dict(connection.execute("SELECT request_id, seq FROM messages"))
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    connection.close()
                lost = [key for key, sequence in acknowledged.items() if stored.get(key) != sequence]
                replays = 0
                for request_id in uncertain:
                    async with client.post(endpoint, json={"body": body},
                                           headers={"Idempotency-Key": request_id}) as response:
                        assert response.status in (200, 201)
                        replays += response.status == 200
                summary = {"cycle": cycle + 1, "acknowledged": len(acknowledged),
                           "uncertain_at_kill": len(uncertain), "already_committed_retries": replays,
                           "lost_acknowledged": len(lost), "integrity_check": integrity}
                results["cycles"].append(summary)
                print(json.dumps(summary), flush=True)
                assert not lost and integrity == "ok"
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    results["finished_at"] = time.time()
    (Path(__file__).parent / "results/crash-load.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="msgboard-crash-load-", dir=os.environ.get("AM_LOCAL", "/tmp")) as directory:
        asyncio.run(run(Path(directory)))
