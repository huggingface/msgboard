import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import resource
import secrets
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import httpx


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    return {name: round(ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)], 3)
            for name, fraction in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}


def cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


class Samples:
    def __init__(self):
        self.statuses = Counter()
        self.errors = Counter()
        self.latency = []
        self.success_latency = []
        self.operations = Counter()
        self.received_bytes = 0
        self.sent_body_bytes = 0
        self.sequences = defaultdict(set)
        self.duplicate_sequences = 0
        self.deliveries = 0
        self.delivery_lag = []

    def record(self, response, elapsed, operation):
        self.latency.append(elapsed * 1000)
        if isinstance(response, Exception):
            self.statuses["exception"] += 1
            self.errors[type(response).__name__] += 1
            return
        self.statuses[str(response.status_code)] += 1
        self.received_bytes += len(response.content)
        if 200 <= response.status_code < 300:
            self.success_latency.append(elapsed * 1000)
            self.operations[operation] += 1
        else:
            try:
                error = response.json().get("error", {})
                self.errors[error.get("code", str(error)) if isinstance(error, dict) else str(error)] += 1
            except ValueError:
                self.errors["non_json_http_error"] += 1

    def summary(self, duration):
        succeeded = sum(self.operations.values())
        return {
            "duration_seconds": round(duration, 3), "requests": sum(self.statuses.values()),
            "successes": succeeded, "statuses": dict(self.statuses), "errors": dict(self.errors),
            "successful_requests_per_second": round(succeeded / duration, 2),
            "posts_per_second": round(self.operations["post"] / duration, 2),
            "operations": dict(self.operations), "all_latency_ms": percentiles(self.latency),
            "success_latency_ms": percentiles(self.success_latency),
            "response_mib_per_second": round(self.received_bytes / duration / 1048576, 3),
            "posted_body_mib": round(self.sent_body_bytes / 1048576, 3),
            "duplicate_acknowledged_sequences": self.duplicate_sequences,
            "sequence_gaps": sum(max(values) - min(values) + 1 - len(values)
                                 for values in self.sequences.values() if values),
            "deliveries": self.deliveries,
            "delivery_lag_ms": percentiles(self.delivery_lag),
        }


class LoadTest:
    def __init__(self, arguments):
        self.arguments = arguments
        self.token = Path(arguments.token_file).read_text().strip()
        self.configuration = json.loads(Path(arguments.config).read_text())
        self.prefix = "load-" + uuid.uuid4().hex[:10]
        self.client = httpx.AsyncClient(
            base_url=arguments.url.rstrip("/"), timeout=httpx.Timeout(45, connect=15, pool=10),
            limits=httpx.Limits(max_connections=2600, max_keepalive_connections=2600),
            follow_redirects=False,
        )
        self.channels = []
        self.tokens = []
        self.control_client = httpx.AsyncClient(base_url=arguments.url.rstrip("/"), timeout=20)
        self.results = {"started_at": time.time(), "target": arguments.url,
                        "transport": "external_https" if arguments.hf_auth else "loopback_shared_cpu",
                        "configuration": self.configuration, "scenarios": []}

    def headers(self, token=None):
        token = token or self.token
        if self.arguments.hf_auth:
            return {"Authorization": "Bearer " + os.environ["HF_TOKEN"], "X-Msgboard-Token": token}
        return {"Authorization": "Bearer " + token}

    async def control(self, method, path, **kwargs):
        response = await self.control_client.request(method, path, headers=self.headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    def save(self):
        destination = Path(self.arguments.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.results, indent=2))
        temporary.replace(destination)

    async def fixture(self, channel_count=32, token_count=32):
        for channel_index in range(channel_count):
            self.channels.append(await self.control("POST", "/v1/channels", json={"name": f"{self.prefix}-{channel_index}"}))
        # Writers join with one agent invite; as member agents they see every public channel.
        invite = await self.control("POST", "/v1/invites", json={"kind": "agent", "label": f"{self.prefix} writers"})
        for token_index in range(token_count):
            issued = await self.control("POST", "/v1/join", json={
                "code": invite["code"], "name": f"{self.prefix}-writer-{token_index}"})
            self.tokens.append(issued["token"])

    async def observe(self, stop, snapshots):
        while not stop.is_set():
            try:
                snapshots.append({"system": await self.control("GET", "/_bench/system"),
                                  "metrics": await self.control("GET", "/v1/metrics")})
            except Exception as error:
                snapshots.append({"error": type(error).__name__})
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def scenario(self, name, concurrency=32, size=1024, duration=10, channel_count=1,
                       write_fraction=1.0, read_limit=100, after=0, rate=None, readers=0,
                       poll_capacity=False, total_requests=None):
        duration = min(max(float(duration), 1), 45)
        concurrency = min(max(int(concurrency), 1), 1500)
        readers = min(max(int(readers), 0), 2100)
        channel_count = min(channel_count, len(self.channels))
        size = min(max(int(size), 0), 65537)
        body = secrets.token_urlsafe(size)[:size]
        content = json.dumps({"body": body}, separators=(",", ":")).encode()
        samples = Samples()
        snapshots = []
        stop = asyncio.Event()
        observer = asyncio.create_task(self.observe(stop, snapshots))
        before = await self.control("GET", "/_bench/system")
        generator_before = cpu_seconds()
        start = time.perf_counter()
        deadline = start + duration
        attempts = 0
        offered_dropped = 0
        scheduler_lag = []
        request_prefix = uuid.uuid4().hex
        issued = 0
        ingress_limited = asyncio.Event()

        async def request_once(worker_index, operation="post", planned=None):
            nonlocal attempts
            attempts += 1
            request_number = attempts
            channel_id = self.channels[worker_index % channel_count]["id"]
            headers = self.headers(self.tokens[worker_index % len(self.tokens)])
            request_start = time.perf_counter()
            measurement_start = planned if planned is not None else request_start
            try:
                if operation == "post":
                    headers.update({"Idempotency-Key": f"{request_prefix}-{request_number}", "Content-Type": "application/json"})
                    response = await self.client.post(f"/v1/channels/{channel_id}/messages", headers=headers, content=content)
                else:
                    response = await self.client.get(f"/v1/channels/{channel_id}/messages", headers=headers,
                                                     params={"after": after, "limit": read_limit})
            except Exception as error:
                response = error
            samples.record(response, time.perf_counter() - measurement_start, operation)
            if not isinstance(response, Exception) and response.status_code == 429 and "text/html" in response.headers.get("content-type", ""):
                ingress_limited.set()
            if not isinstance(response, Exception) and response.status_code == 201 and operation == "post":
                message = response.json()
                sequence = message["seq"]
                if sequence in samples.sequences[channel_id]:
                    samples.duplicate_sequences += 1
                samples.sequences[channel_id].add(sequence)
                samples.sent_body_bytes += len(body.encode())

        async def worker(worker_index):
            nonlocal issued
            generator = random.Random(worker_index)
            while time.perf_counter() < deadline and not ingress_limited.is_set():
                if total_requests is not None:
                    if issued >= total_requests:
                        break
                    issued += 1
                operation = "post" if generator.random() < write_fraction else "read"
                await request_once(worker_index, operation)

        async def subscriber(reader_index):
            channel = self.channels[reader_index % channel_count]
            cursor = channel["last_seq"]
            headers = self.headers(self.tokens[reader_index % len(self.tokens)])
            while time.perf_counter() < deadline and not ingress_limited.is_set():
                request_start = time.perf_counter()
                try:
                    response = await self.client.get(
                        f"/v1/channels/{channel['id']}/messages", headers=headers,
                        params={"after": cursor, "limit": 100, "wait": 10 if poll_capacity else 2},
                    )
                except Exception as error:
                    response = error
                samples.record(response, time.perf_counter() - request_start, "poll")
                if not isinstance(response, Exception) and response.status_code == 429 and "text/html" in response.headers.get("content-type", ""):
                    ingress_limited.set()
                if not isinstance(response, Exception) and response.status_code == 200:
                    batch = response.json()
                    cursor = batch["next_cursor"]
                    samples.deliveries += len(batch["messages"])
                    samples.delivery_lag.extend(max(0, (time.time() - item["created_at"]) * 1000)
                                                for item in batch["messages"])
                else:
                    if poll_capacity:
                        break
                    await asyncio.sleep(.25)
                if poll_capacity:
                    break

        async def paced():
            nonlocal offered_dropped
            pending = set()
            offered = max(1, int(rate * duration))
            for request_index in range(offered):
                if ingress_limited.is_set():
                    break
                planned = start + request_index / rate
                delay = planned - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                scheduler_lag.append(max(0, (time.perf_counter() - planned) * 1000))
                if len(pending) >= concurrency:
                    offered_dropped += 1
                    continue
                task = asyncio.create_task(request_once(request_index, planned=planned))
                pending.add(task)
                task.add_done_callback(pending.discard)
            if pending:
                await asyncio.gather(*pending)

        try:
            if readers:
                visible = await self.control("GET", "/v1/channels")
                last_sequences = {channel["id"]: channel["last_seq"] for channel in visible["channels"]}
                for channel in self.channels:
                    channel["last_seq"] = last_sequences[channel["id"]]
                subscribers = [asyncio.create_task(subscriber(reader_index)) for reader_index in range(readers)]
                if not poll_capacity:
                    await asyncio.sleep(.5)
                    await paced()
                await asyncio.gather(*subscribers)
            elif rate is not None:
                await paced()
            else:
                await asyncio.gather(*(worker(worker_index) for worker_index in range(concurrency)))
        finally:
            elapsed = time.perf_counter() - start
            generator_cpu = cpu_seconds() - generator_before
            stop.set()
            await observer
        telemetry_error = None
        after_snapshot, metrics = {}, {}
        if not ingress_limited.is_set():
            try:
                after_snapshot = await self.control("GET", "/_bench/system")
                metrics = await self.control("GET", "/v1/metrics")
            except Exception as error:
                telemetry_error = type(error).__name__
        entry = {"name": name, "parameters": {"concurrency": concurrency, "body_bytes": size,
                 "duration": duration, "channels": channel_count, "write_fraction": write_fraction,
                 "read_limit": read_limit, "after": after, "offered_posts_per_second": rate,
                 "readers": readers, "poll_capacity": poll_capacity, "total_requests": total_requests},
                 **samples.summary(elapsed), "offered_dropped_by_generator": offered_dropped,
                 "scheduler_lag_ms": percentiles(scheduler_lag),
                 "server_cpu_cores": round((after_snapshot["cpu_seconds"] - before["cpu_seconds"]) / elapsed, 3) if after_snapshot else None,
                 "generator_cpu_cores": round(generator_cpu / elapsed, 3),
                 "ingress_limited": ingress_limited.is_set(), "telemetry_error": telemetry_error,
                 "system_before": before, "system_after": after_snapshot,
                 "metrics_after": metrics, "observations": snapshots}
        self.results["scenarios"].append(entry)
        self.save()
        print(json.dumps({"name": name, "rps": entry["successful_requests_per_second"],
                          "p95_ms": entry["success_latency_ms"].get("p95"), "statuses": entry["statuses"]}), flush=True)

    async def seed(self, target, size=2048):
        if not self.arguments.data_dir:
            raise ValueError("volume suite needs an on-Space data directory")
        from msgboard import db, store

        channel_id = self.channels[0]["id"]
        body = secrets.token_urlsafe(size)[:size]
        _, digest = store.normalize_payload(channel_id, {"body": body})
        started = time.perf_counter()

        def populate():
            connection = db.connect(Path(self.arguments.data_dir) / "board.db")
            try:
                principal = store.authenticate(connection, self.token, time.time())
                next_sequence = connection.execute("SELECT next_seq FROM channels WHERE id=?", (channel_id,)).fetchone()[0]
                added = 0
                while next_sequence <= target:
                    end = min(target + 1, next_sequence + 2000)
                    connection.execute("BEGIN IMMEDIATE")
                    connection.executemany(
                        "INSERT INTO messages (channel_id,seq,sender_id,body,request_id,payload_hash,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        ((channel_id, sequence, principal.id, body, f"seed-{channel_id}-{sequence}", digest, time.time())
                         for sequence in range(next_sequence, end)),
                    )
                    connection.execute("UPDATE channels SET next_seq=? WHERE id=?", (end, channel_id))
                    connection.execute("COMMIT")
                    added += end - next_sequence
                    next_sequence = end
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                return {"added": added, "count": connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)).fetchone()[0]}
            finally:
                connection.close()

        counts = await asyncio.to_thread(populate)
        self.results.setdefault("seeding", []).append({"method": "direct_SQL_in_2000_row_transactions_not_API_throughput",
            "target": target, "body_bytes": size, "duration_seconds": time.perf_counter() - started, **counts})
        self.save()
        print(json.dumps({"seed_target": target, **counts}), flush=True)

    async def limits(self):
        channel_id = self.channels[0]["id"]
        checks = []
        for size in (65536, 65537):
            response = await self.client.post(f"/v1/channels/{channel_id}/messages",
                headers={**self.headers(self.tokens[0]), "Idempotency-Key": uuid.uuid4().hex}, json={"body": "a" * size})
            checks.append({"check": "body_size", "bytes": size, "status": response.status_code})
        for limit in (500, 501):
            response = await self.client.get(f"/v1/channels/{channel_id}/messages",
                headers=self.headers(self.tokens[0]), params={"limit": limit})
            checks.append({"check": "page_limit", "limit": limit, "status": response.status_code})
        request_id = uuid.uuid4().hex
        for text in ("deduplication", "deduplication", "changed"):
            response = await self.client.post(f"/v1/channels/{channel_id}/messages",
                headers={**self.headers(self.tokens[0]), "Idempotency-Key": request_id}, json={"body": text})
            checks.append({"check": "idempotency", "body": text, "status": response.status_code,
                           "seq": response.json().get("seq")})
        self.results["boundary_checks"] = checks
        self.save()
        await self.scenario("open_poll_limit_2100", readers=2100, duration=15, poll_capacity=True)
        await self.scenario("burst_1500_connections", concurrency=1500, duration=30, total_requests=6000)

    async def run(self):
        suite = self.configuration["suite"]
        duration = min(float(self.configuration.get("duration", 10)), 30)
        self.results["system"] = await self.control("GET", "/_bench/system")
        self.save()
        try:
            await self.fixture(channel_count=32, token_count=32)
            if suite == "smoke":
                await self.scenario("smoke", concurrency=2, duration=2)
            elif suite == "matrix":
                for concurrency in (1, 8, 32, 128, 512):
                    await self.scenario(f"writers_{concurrency}", concurrency=concurrency, duration=duration)
                for size in (256, 4096, 16384, 65536):
                    await self.scenario(f"payload_{size}", size=size, duration=duration)
                await self.scenario("distributed_32_channels", channel_count=32, duration=duration)
                for fraction in (.1, .5):
                    await self.scenario(f"mixed_{int(fraction*100)}pct_posts", write_fraction=fraction,
                                        read_limit=10, channel_count=32, duration=duration)
            elif suite == "rate":
                for rate in self.configuration.get("rates", [100, 200, 400, 800]):
                    await self.scenario(f"offered_{rate}_posts_s", concurrency=512, rate=rate,
                                        size=2048, duration=duration)
            elif suite == "fanout":
                for readers in self.configuration.get("readers", [10, 100, 500, 1000]):
                    await self.scenario(f"fanout_{readers}_shared", readers=readers, rate=20,
                                        concurrency=128, duration=duration)
                await self.scenario("fanout_1000_partitioned", readers=1000, rate=20,
                                    channel_count=32, concurrency=128, duration=duration)
            elif suite == "volume":
                for target in (1000, 100000, 1000000):
                    if target:
                        await self.seed(target)
                    for label, cursor in (("start", 0), ("middle", target // 2), ("tail", max(0, target - 100))):
                        await self.scenario(f"history_{target}_{label}", concurrency=16, write_fraction=0,
                                            after=cursor, read_limit=100, duration=duration)
                    await self.scenario(f"write_at_{target}", size=2048, duration=duration)
            elif suite == "limits":
                await self.limits()
            elif suite == "scenario":
                for configuration in self.configuration.get("scenarios", []):
                    await self.scenario(**configuration)
            self.results["finished_at"] = time.time()
            self.results["final_metrics"] = await self.control("GET", "/v1/metrics")
        finally:
            self.save()
            await self.client.aclose()
            await self.control_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir")
    parser.add_argument("--hf-auth", action="store_true")
    asyncio.run(LoadTest(parser.parse_args()).run())
