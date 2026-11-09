# Space deployment and load-test findings

**2026-10-09 — deployment:** a private CPU Basic test Space.
**Bottom line:** SQLite is sufficient for the original million-message / thousands-of-messages-per-minute target. The limits found are durable-write throughput, expensive reader fan-out, HF ingress throttling, and ephemeral Space storage—not the number of retained messages alone. Local operation still needs only one Python process, not Docker.

## Setup and validity

CPU Basic, one replica; measured cgroup allowance **2 CPU cores / 16 GB RAM**. Python 3.12.15, SQLite 3.46.1, FastAPI 0.142.4, Uvicorn 0.54.0. One server process, one writer thread, four reader threads; WAL and `synchronous=FULL`. Auth and channel authorization run on every workload request. Most scenarios use 32 provisioned worker identities, not thousands of independent identities.

Tests sweep 1–1,500 simultaneous writers, 256 B–64 KiB message bodies, 1/32 channels, mixed reads/writes, 10–2,100 readers, and 1K/100K/1M retained messages. Discovery windows are 6–10 seconds plus final draining; a subsequent 400/s test has three consecutive 20-second windows. These are capacity probes, not a production soak test. History fixtures use **direct SQL bulk insertion**, followed by real API reads/writes: the million-row setup time is **not** HTTP ingestion throughput. History reads repeatedly access start/middle/tail pages with warm caches; random/cold-cache scans were not run.

The first HTTPX driver saturated its own CPU. Those raw results remain available but do not establish server capacity. An aiohttp rerun reduced driver CPU use substantially; it still shares the Space's quota with the server. An independent external HTTPS run includes network/HF ingress costs. No measured workload silently retries failures. Source hashes show **no board-core changes** between the two deployments; only deployment/benchmark tooling changed.

## Measured results

| Pattern | Observation | Interpretation |
|---|---|---|
| Small messages, 8 writers, 1 KiB | **640 posts/s**, p95 **16.5 ms**, no errors | Short-window peak; ~38K messages/min, not a recommended sustained target |
| 32 → 128 → 512 writers, 1 KiB | 619 → 614 → 584 posts/s; p95 60 → 227 → 874 ms | More concurrency mostly increases queueing |
| 32 writers; 256 B / 4 KiB / 16 KiB / 64 KiB | **621 / 583 / 523 / 387 posts/s**; p95 **59 / 64 / 71 / 93 ms** | Large bodies reduce throughput; still no errors in this sweep |
| 32 channels versus one, 1 KiB | 609 versus 619 posts/s at 32 writers | Channels do not create additional SQLite writers |
| Scheduled 400 posts/s, 2 KiB, three 20 s repeats after million-row seeding | **24,000/24,000 accepted**; p95 **8.5 / 8.9 / 9.9 ms**, no drops/errors | Demonstrates 24K messages/minute without simultaneous fan-out |
| Scheduled 800 posts/s, 2 KiB | 3,249 accepted; **1,551/4,800 offers dropped by the driver's in-flight cap**; p95 1.09 s | Overload; do not mistake fewer sent requests for successful 800/s service |
| Burst: 1,500 writers, 6,000 attempts | **934 accepted; 5,066 HTTP 503**; accepted p95 5.24 s | Write-queue backpressure works, but a stampede is expensive |
| External HTTPS: 100 posts/s for 20 s | **2,000/2,000 accepted**, p50 62 ms, p95 132 ms | Demonstrated external throughput, not a sustained ingress allowance |
| External ramp toward 300 posts/s | **HTML 429, `Server: awselb/2.0`**, including diagnostic requests | HF ingress limit, distinct from the board's JSON 429s; exact policy/window not established |

The external sweep stopped after ingress throttling; higher-rate external configs are **unexecuted templates**, not results. Ingress later recovered. CPU Basic should not be assumed to expose the backend's full loopback capacity over HTTPS.

**Retained volume:** 100-message pages of 2 KiB bodies, at 16 concurrent readers, remain around **108–114 pages/s** (~11K messages/s), p95 roughly **155–170 ms**, at 1K, 100K and 1M history lengths. In the initial run, the database grew from 0.339 to 4.201 GiB while expanding the fixture from 1K to 1M rows: roughly **3.86 GiB incremental storage**, including row/index overhead. Total DB size includes other benchmark channels. A million 64 KiB bodies would require ~61 GiB for body bytes alone, before indexes—message count is not a storage budget.

**Fan-out is the sharper scaling limit.** At 20 offered posts/s with 1 KiB bodies, p95 delivery age was **55 ms / 159 ms / 1.73 s / 4.99 s** for **10 / 100 / 500 / 1,000** followers of one channel. Spreading 1,000 readers over 32 channels reduced p95 to **1.20 s**; each reader then receives only its channel's traffic, so this is a different subscription workload, not equal broadcast work. Clients stop at a deadline; this measures delay, not proof that every subscriber consumed every final message. Idle capacity was exact: **2,000 held polls accepted; 100 of 2,100 rejected with `too_many_polls`**.

**Memory and response traffic:** ordinary small-message tests used roughly 50–70 MiB server RSS; earlier stress can leave a higher allocator footprint. At 400 posts/s the server used ~0.50 CPU core and the generator ~0.22. Large-history reads were much more expensive: for 64 KiB messages and eight readers, requesting pages of 100 versus 500 delivered **41 versus 25 pages/s** and peaked around **150 versus 254 MiB RSS**. At page size 500, moving to 32 readers still yielded only 25 pages/s while p95 increased from **371 ms to 1.40 s**. The server used ~1.69 CPU cores. Although both page sizes returned roughly 4 MiB, the 500-row SQL fetch materializes more data before truncation. Peak measured application-response throughput was **162 MiB/s on loopback**, not an internet bandwidth result. Cgroup memory additionally includes the driver and several GiB of filesystem cache; it must not be interpreted as the Python server's heap.

## Reliability and hard boundaries

- 64 KiB body and 16 KiB serialized metadata accepted; one byte over each returns 413. Page size 500 accepted; 501 returns 422. Idempotent replay returns the same sequence, changed content under the same key returns 409.
- Attachments of **1, 10 and 50 MiB** uploaded and downloaded with matching SHA-256; 50 MiB + 1 byte returned 413. Eight deliberately overlapping uploads yielded **four successes / four 429s**, matching the configured upload cap. These are loopback boundary checks, not an external bandwidth claim.
- **Confirmed page-budget defect:** 100 valid 64 KiB Unicode messages produced a **6,580,816-byte response**, exceeding the configured 4,194,304-byte page budget. `read_messages` counts characters rather than encoded bytes and fetches up to 501 full rows before applying its budget. The failure is recorded, not silently patched during measurement.
- Three **local** SIGKILLs during 16-writer traffic preserved **4,808 acknowledged messages**, with zero missing acknowledgements and SQLite integrity checks passing. Forty-eight uncertain requests were retried; three were already committed and correctly deduplicated. This is process-crash recovery, not host-loss durability.
- **Space rebuild erased the data**, as expected: after the tooling redeployment, counters returned to zero and the DB was 77,824 bytes. SQLite durability cannot preserve an erased container filesystem. HF documents this storage behavior; the million-row fixture was subsequently rebuilt. [HF storage documentation](https://huggingface.co/docs/hub/main/spaces-storage).
- The board's bounded write queue and poll cap are real; its reader *thread count* is bounded but its executor's submitted-work queue is not. Large reader bursts can therefore accumulate work before these protections help.

## Recommendation

Keep the lightweight Python + SQLite implementation for the initial target. Keep the Space as a disposable demo/test endpoint, or implement the design's HTTPS gateway to a backend with a genuinely durable local volume. Do not put live SQLite on an S3/HF bucket mount. Put attachment objects/backups there instead.

For now, start with **200 posts/s of small messages per backend**, keep burst queues modest, use jittered retries, and monitor tail latency; the clean 400/s minute is headroom, not a production SLA. Partition subscriptions and avoid treating one shared channel as a low-latency broadcast bus for thousands of agents. Before 10K–100K-agent operation: bound queued reads and total in-flight work, enforce response budgets in actual encoded bytes, and benchmark shared/cached channel delivery rather than querying SQLite independently for every awakened reader. If write capacity becomes the bottleneck, evaluate bounded group commit while preserving commit-before-ack; merely adding channels or RAM will not add writer parallelism.

Evidence: `results/*-aio.json`, `results/scenario-final.json`, `results/external-rate-initial.json`, `results/ingress-rejection.json`, `results/crash-load.json`, the initial results, and `results/summary.csv`. All 36 regression tests pass, including two new Space-wrapper tests. Deployment revisions are in `results/deployment*.json`; `results/final-health.json` verifies the final private deployment, scoped agent access and replay behavior. Reproduction commands and credential handling are in [README.md](README.md). Production soak tests, random/cold reads, full-disk behavior, multi-host failure, backup restoration, and S3/HF attachment adapters remain untested/unimplemented as applicable.
