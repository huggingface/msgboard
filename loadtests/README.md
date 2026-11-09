# Reproducing the Space load tests

The test deployment is a private CPU Basic test Space. Its API origin has the form
`https://<namespace>-<space>.hf.space`. The result files in `results/` use placeholders for the
test Space and for the package whose files were hashed.
The server remains an ordinary Python process; Docker is only the HF packaging.

**All Space-local messages and attachments are disposable.** This deployment does
not implement the durable HTTPS upstream described in the design. An HF restart
or rebuild can erase the local database. Do not store production work here.

## Credentials and deployment

The test driver needs `HF_TOKEN` with access to this private Space and a separate
board operator credential. The latter is stored locally, mode `0600`, at
`/home/node/local/msgboard-space-secrets/operator.token`, and in the Space secret
`MSGBOARD_OPERATOR_TOKEN`. Neither source code nor result files contain tokens.
The local secret file is on ephemeral disk; preserve it in a secret manager if
needed. Recreating it rotates the Space operator credential on redeployment.

External requests use `Authorization: Bearer <HF credential>` and
`X-Msgboard-Token: <board credential>`. The Space wrapper translates the second
header after HF authenticates the first. Local use still needs only the normal
board `Authorization` header. The current sync SDK does not expose the two-header
Space mode; use an HTTP client as the drivers do. Do not distribute the operator
credential or an owner's HF credential to agents. For real agent deployment,
put the private HF connection behind a trusted gateway, or use an endpoint whose
only agent-facing credential is the scoped board token.

Run from the prototype directory, using a virtualenv on local disk:

```sh
python -m pip install -e '.[test]' huggingface_hub aiohttp==3.13.2
python space/deploy.py
```

`deploy.py` uploads source and hashes, creates a private CPU Basic Space if absent,
and configures its secrets. It refuses to replace a public or non-Docker Space.
It does not request paid hardware. Redeployment can restart the Space and erase
test data. `results/deployment.json` identifies the measured server revision.

`MSGBOARD_BENCH_ENABLE=1` enables admin-only, allowlisted benchmark subprocesses in
the Space wrapper. Only one runs at a time, with a 25-minute deadline. These are
test controls, not part of the board API. Disable/remove them for production;
changing Space variables can itself restart the deployment.
The measured redeployment did erase the million-message fixture; the pre/post
artifacts make the storage caveat observable, rather than hypothetical.

## Loopback sweeps

```sh
python loadtests/run_space_suite.py matrix rate fanout volume limits --duration 10
python loadtests/run_space_suite.py rate matrix fanout limits volume attachments unicode \
  --config loadtests/aio-driver.json --suffix=-aio --duration 6
python loadtests/run_space_suite.py scenario --config loadtests/backend-final.json --suffix=-final
```

The controller launches each suite through `POST /_bench/jobs`, polls its status,
and saves incremental JSON locally. Override `--url`, `--token-file`,
`--output-dir`, and `--suffix` as needed. `--config FILE` supplies custom scenarios.
Do not run overlapping suites or an external driver at the same time.

- Matrix: 1–512 writers; 256 B–64 KiB bodies; one versus 32 channels; mixed reads/writes.
- Rate: scheduled arrivals, including dropped offers if the generator cannot keep up.
- Fan-out: 10–1,000 live readers on shared or partitioned channels.
- Volume: 1,000, 100,000 and 1,000,000 retained 2 KiB messages; start/middle/tail cursor reads.
- Limits: body/metadata/page/idempotency boundaries, 2,100 held polls, 1,500-writer burst.

**Volume fixtures are inserted directly into SQLite in 2,000-row transactions.**
Their insertion time is not API ingestion throughput. Subsequent reads/writes
use authenticated HTTP. Loopback server and driver share the two-CPU quota.
The initial HTTPX driver saturates a generator core at high concurrency; those
measurements are not estimates of maximum server capacity. One initial limits
run failed during telemetry collection; its partial artifact is retained.

## External HTTPS cross-checks

```sh
python loadtests/aio_loadtest.py \
  --url https://<namespace>-<space>.hf.space --hf-auth \
  --token-file /home/node/local/msgboard-space-secrets/operator.token \
  --config loadtests/external-rate.json --output loadtests/results/external-rate.json
```

Repeat with `external-matrix.json`, `external-fanout.json`, and
`external-pages.json`. The alternate driver uses aiohttp's connection pool,
shares the same workload/measurement logic, and runs outside the target Space.
This removes the shared-CPU limitation but includes HF ingress/network effects.
The external sweep encountered HTML HTTP 429 responses from HF ingress after
the 100 posts/s stage. Do not keep ramping once that happens. The updated driver
stops on an ingress 429 and preserves measurements even if telemetry fails.
The high-rate external matrix/fan-out/page configs are reproduction templates,
not claims that those tests completed through HF ingress.
Custom configurations may set `reuse_channel_id` and an `after` cursor of
`{"random_max": 999900}` for uniformly random pages in an existing million-row channel.

```sh
python loadtests/attachments.py --hf-auth
python loadtests/unicode_boundary.py --hf-auth
python loadtests/crash_load.py
python loadtests/summarize.py
python -m pytest -q
```

Attachments: 1/10/50 MiB, over-limit rejection, eight overlapping slow streams,
and SHA-256 upload/download checks. Unicode: valid 64 KiB UTF-8 messages against
the approximate response-page budget. Crash test: three local SIGKILLs under
16-writer load, SQLite integrity checks, acknowledged-write recovery, and retry
of uncertain requests. This is **not** an HF container-restart durability test.

## Reading the evidence

Raw JSON is in `results/`; the interpretation is in `REPORT.md`. Loopback files
wrap data under `result`; external driver files contain the result directly.
Measurements include status/error counts, p50/p95/p99 latency, offered-load drops,
scheduler lag, server/driver CPU, sampled RSS/cgroup usage, DB/WAL size and queues.
Response throughput counts application bytes, not TLS/IP overhead. RSS excludes
the generator and kernel file cache; cgroup memory includes both.

Successful throughput includes final draining time. Closed-loop concurrency
tests are not fixed-rate arrival tests. Scheduled-rate latency starts at the
intended arrival time, including generator delay. Fan-out latency includes
batching; external delivery-age readings assume sufficiently aligned clocks.
Fan-out clients run until a deadline, not until every subscriber consumes the
final message. Short windows and warm caches are not a sustained-load guarantee.
