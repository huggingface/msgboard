from __future__ import annotations

import argparse
import asyncio
import random

import aiohttp
import httpx

from loadtest import LoadTest


class AioClient:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2600),
            timeout=aiohttp.ClientTimeout(total=45, connect=15),
            headers={"Accept-Encoding": "identity"},
        )

    async def request(self, method, path, **kwargs):
        if "content" in kwargs:
            kwargs["data"] = kwargs.pop("content")
        cursor = kwargs.get("params", {}).get("after")
        if isinstance(cursor, dict) and "random_max" in cursor:
            kwargs["params"] = {**kwargs["params"], "after": random.randrange(cursor["random_max"])}
        async with self.session.request(method, self.url + path, allow_redirects=False, **kwargs) as response:
            content = await response.read()
            headers = {name: value for name, value in response.headers.items()
                       if name.lower() not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status, content=content, headers=headers,
                                  request=httpx.Request(method, self.url + path))

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def aclose(self):
        await self.session.close()


class AioLoadTest(LoadTest):
    async def scenario(self, *args, **kwargs):
        await super().scenario(*args, **kwargs)
        if self.results["scenarios"][-1]["ingress_limited"]:
            raise RuntimeError("Stopping the sweep after an ingress rate-limit response")

    async def fixture(self, channel_count=32, token_count=32):
        existing_id = self.configuration.get("reuse_channel_id")
        if existing_id is None:
            await super().fixture(channel_count, token_count)
        else:
            visible = await self.control("GET", "/v1/channels")
            self.channels = [channel for channel in visible["channels"] if channel["id"] == existing_id]
            if len(self.channels) != 1:
                raise ValueError("reuse_channel_id must identify one visible channel")
            for token_index in range(token_count):
                agent = await self.control("POST", "/v1/agents", json={
                    "external_id": f"{self.prefix}-writer-{token_index}", "role": "worker",
                    "channels": [self.channels[0]["name"]]})
                issued = await self.control("POST", f"/v1/agents/{agent['id']}/token", json={"ttl_seconds": 7200})
                self.tokens.append(issued["token"])
        self.results["channel_ids"] = [channel["id"] for channel in self.channels]
        self.save()


async def main(arguments):
    benchmark = AioLoadTest(arguments)
    suite = benchmark.configuration["suite"]
    if suite in {"attachments", "unicode"}:
        await benchmark.client.aclose()
        await benchmark.control_client.aclose()
        if suite == "attachments":
            from attachments import main as attachment_main
            await attachment_main(arguments)
        else:
            from unicode_boundary import main as unicode_main
            await unicode_main(arguments)
        return
    await benchmark.client.aclose()
    benchmark.client = AioClient(arguments.url)
    benchmark.results["client"] = "aiohttp " + aiohttp.__version__
    await benchmark.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir")
    parser.add_argument("--hf-auth", action="store_true")
    asyncio.run(main(parser.parse_args()))
