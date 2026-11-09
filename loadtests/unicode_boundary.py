import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import aiohttp


async def main(arguments):
    token = Path(arguments.token_file).read_text().strip()
    headers = {"Authorization": "Bearer " + os.environ["HF_TOKEN"], "X-Msgboard-Token": token} if arguments.hf_auth else {"Authorization": "Bearer " + token}
    async with aiohttp.ClientSession(base_url=arguments.url, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=60)) as client:
        async with client.post("/v1/channels", json={"name": "unicode-" + uuid.uuid4().hex[:10]}) as response:
            response.raise_for_status()
            channel = await response.json()
        endpoint = f"/v1/channels/{channel['id']}/messages"
        body = "🌍" * 16384
        payload = json.dumps({"body": body}, ensure_ascii=False).encode()
        statuses = []
        for index in range(100):
            async with client.post(endpoint, data=payload, headers={"Content-Type": "application/json",
                                                                    "Idempotency-Key": uuid.uuid4().hex}) as response:
                statuses.append(response.status)
                await response.read()
        async with client.get(endpoint, params={"limit": 500}, headers={"Accept-Encoding": "identity"}) as response:
            content = await response.read()
            result = {"time": time.time(), "body_utf8_bytes": len(body.encode()), "body_codepoints": len(body),
                      "post_statuses": statuses, "read_status": response.status,
                      "returned_messages": len(json.loads(content)["messages"]),
                      "response_bytes": len(content), "configured_max_page_bytes": 4194304}
        Path(arguments.output).write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != "post_statuses"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", default="/home/node/local/msgboard-space-secrets/operator.token")
    parser.add_argument("--output", default=str(Path(__file__).parent / "results/unicode-boundary.json"))
    parser.add_argument("--hf-auth", action="store_true")
    asyncio.run(main(parser.parse_args()))
