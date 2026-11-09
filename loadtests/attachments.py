import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path

import aiohttp

from loadtest import percentiles


async def main(arguments):
    token = Path(arguments.token_file).read_text().strip()
    headers = {"Authorization": "Bearer " + os.environ["HF_TOKEN"], "X-Msgboard-Token": token} if arguments.hf_auth else {"Authorization": "Bearer " + token}
    results = {"transport": "external_https" if arguments.hf_auth else "loopback_shared_cpu", "started_at": time.time(), "scenarios": []}
    async with aiohttp.ClientSession(base_url=arguments.url, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=120)) as client:
        async with client.post("/v1/channels", json={"name": "attachments-" + uuid.uuid4().hex[:10]}) as response:
            response.raise_for_status()
            channel = await response.json()

        async def upload(size, slow=False):
            chunk = os.urandom(65536)
            digest = hashlib.sha256()

            async def stream():
                remaining = size
                while remaining:
                    part = chunk[:min(remaining, len(chunk))]
                    digest.update(part)
                    remaining -= len(part)
                    if slow:
                        await asyncio.sleep(.02)
                    yield part

            started = time.perf_counter()
            async with client.put(f"/v1/channels/{channel['id']}/attachments/{uuid.uuid4().hex}",
                                  data=stream(), headers={"Content-Length": str(size)}) as response:
                data = await response.json()
                elapsed = time.perf_counter() - started
                outcome = {"status": response.status, "duration_seconds": elapsed}
                if response.status in (200, 201):
                    outcome["upload_checksum_valid"] = data["sha256"] == digest.hexdigest()
                    outcome["attachment_id"] = data["id"]
                    async with client.get("/v1/attachments/" + data["id"]) as download:
                        received = hashlib.sha256()
                        async for part in download.content.iter_chunked(65536):
                            received.update(part)
                        outcome["download_checksum_valid"] = received.hexdigest() == data["sha256"]
                else:
                    outcome["error"] = data
                return outcome

        for size, concurrency, slow in ((1048576, 1, False), (10485760, 1, False),
                                         (52428800, 1, False), (52428801, 1, False),
                                         (1048576, 8, True)):
            started = time.perf_counter()
            outcomes = await asyncio.gather(*(upload(size, slow) for index in range(concurrency)), return_exceptions=True)
            outcomes = [{"exception": type(value).__name__} if isinstance(value, Exception) else value for value in outcomes]
            result = {"size_bytes": size, "concurrency": concurrency, "slow_stream": slow,
                      "duration_seconds": time.perf_counter() - started, "outcomes": outcomes,
                      "statuses": dict(Counter(str(value.get("status", "exception")) for value in outcomes)),
                      "upload_latency_ms": percentiles([value["duration_seconds"] * 1000
                                                         for value in outcomes if "duration_seconds" in value])}
            results["scenarios"].append(result)
            Path(arguments.output).write_text(json.dumps(results, indent=2))
            print(json.dumps({key: value for key, value in result.items() if key != "outcomes"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", default="/home/node/local/msgboard-space-secrets/operator.token")
    parser.add_argument("--output", default=str(Path(__file__).parent / "results/attachments.json"))
    parser.add_argument("--hf-auth", action="store_true")
    asyncio.run(main(parser.parse_args()))
