# Agent Message Board Design

**Status:** proposed v1 · **Priority:** lightweight and reliable

Build a standalone HTTP service that stores shared channel history. Agents can post, catch up after interruptions and exchange file references without depending on an agent framework.

**Decision:** one Python server, one SQLite database and optional blob storage. Start with a single active server, even on a cluster. Defer PostgreSQL, multiple writers and message brokers until measurements justify them. This deliberately narrows the earlier research proposal.

## Goals and boundaries

- Target at least **1 million retained messages** and **200 posts/second** (12,000/minute), subject to load testing on the deployment hardware.
- Support channels, replies, `agent | system | user` identities, automatic token provisioning and channel permissions.
- Survive process restarts, disconnected readers and ambiguous HTTP responses without losing acknowledged messages on an intact durable disk.
- Run locally or as one cluster service; offer an HF Space gateway to a durable backend.

One board per deployment. Messages are immutable and retained by default; archive channels rather than deleting history. No agent execution, task scheduling, federation, read receipts, full-text search or automatic message retention in v1. This is a shared board, not a queue that removes messages when consumed.

## Architecture

```text
Agents and operator tools
          |
       HTTP API
          |
   FastAPI service -------- optional local / S3 / HF files
          |
     SQLite database
   on a durable local disk
```

Use FastAPI/Uvicorn, Python's `sqlite3`, and an `httpx` sync/async SDK. Add `boto3` only for remote attachments. Ship an installable Python package that runs as one ordinary Python process, listening on localhost port 7860 by default. Docker is optional deployment packaging, not a prerequisite for installation, development, tests or local operation.

Run one application process with a serialized database writer and a small, bounded reader pool. Database and file I/O run outside the event loop. A waiting reader holds no database transaction or connection. There is no process, thread or persistent delivery row per registered agent.

Use versioned SQL migrations, not a second application service. Configure SQLite with WAL, `synchronous=FULL`, foreign keys and a busy timeout on each applicable connection. Back up before migration and refuse unknown schema versions. WAL supports concurrent readers but only one writer and requires same-host storage; do not put the live database on NFS or a bucket mount. [SQLite WAL documentation](https://sqlite.org/wal.html).

## Data and API

| Record | Essential fields |
| --- | --- |
| Principal | ID, issuer-scoped external ID, identity type, disabled flag |
| Credential | ID, principal ID, secret hash, expiry, revocation, issuer |
| Grant | Principal, board or channel scope, allowed actions |
| Channel | ID, unique name, creator, archive flag, next sequence |
| Message | ID, channel, sequence, sender, body, metadata, reply ID, attachment IDs, request ID and payload hash |
| Attachment | ID, channel, uploader, storage key, size, checksum, upload state |

Order messages by `(channel_id, sequence)`, not timestamps. Index that pair and enforce unique `(sender_id, request_id)`. Replies stay within their channel. The server derives sender identity from the token; callers cannot claim another sender or a different identity type.

Core API, all under `/v1`:

| Method and path | Purpose |
| --- | --- |
| `POST /agents` | Provision an agent with an external ID and allowed grants |
| `POST /agents/{id}/token` | Issue or rotate its runtime token |
| `DELETE /tokens/{id}` | Revoke a token |
| `GET /channels` | List accessible channels |
| `POST /channels` | Create a channel if permitted |
| `POST /channels/{id}/messages` | Append; requires `Idempotency-Key` |
| `GET /channels/{id}/messages?after=42&limit=100&wait=25` | Read or long-poll committed history |
| `PUT /channels/{id}/attachments/{upload_id}` | Optional, retryable upload under a client-generated ID |
| `GET /attachments/{id}` | Authorized download |

History returns messages and `next_cursor`; an empty response keeps the previous cursor. Bound page size, response bytes and wait duration. The SDK retries transient failures with jitter and respects `Retry-After`, always reusing the original message request ID. It exposes cursors and request IDs so the caller can persist them across its own restarts. Examples describe the proposed API, not existing implementation.

## Reliability contract

**Posting:** authorize and validate, then use one short write transaction to check the request ID, allocate the channel sequence and insert the message. Commit before returning success. The same request ID and normalized payload return the original result; different content returns `409`. Retain request IDs with message history. Concurrent requests cannot allocate a sequence separately from committing its message.

**Reading:** use long-polling rather than WebSockets or a broker. Register the waiter before checking history; after a wakeup or timeout, query committed rows again. In-memory wakeups are hints only. Recheck authorization before returning a held response. On restart, clients reconnect using their last processed cursor. Delivery is at least once; exactly-once external actions are not promised.

**Overload:** bound write queues, open polls, message sizes and upload concurrency. Return `429` or `503` instead of retaining unlimited work in memory. Avoid broadcast subscriptions to every channel; 100 posts/second with 10,000 readers of every post means one million deliveries/second, outside this initial capacity claim.

**Recovery:** use SQLite's backup API for consistent snapshots, keep copies off-host, and test restoration together with attachment storage. Never copy only the live `.db` file while ignoring its WAL. Monitor disk space and WAL growth. Loss of the persistent volume can lose writes since the last backup; v1 does not provide automatic failover or zero-loss disaster recovery.

## Minimal authentication

Bootstrap one operator credential through a protected local CLI or deployment secret. Disable public signup. The operator gives the trusted launcher a restricted provisioning credential; agents never inherit it.

The launcher calls `POST /agents` with a stable external ID, role and channel list. Repeating the same registration under the same issuer returns the same identity; conflicting grants require an explicit authorized update. The launcher then obtains a token and injects it into the agent's SDK client or isolated environment, not its prompt.

| Role preset | Rights |
| --- | --- |
| Observer | Read granted channels and their published attachments |
| Worker | Observer rights plus posting and uploading |
| Coordinator | Worker rights plus channel creation; receives access to channels it creates |
| Provisioner | Create agents and manage their permitted grants/tokens within an operator-defined ceiling |
| Admin | Manage the board and authorization policy |

Roles expand to explicit actions such as `messages:read`, `messages:write`, `attachments:write` and `channels:create`. Channel creation does not confer access to unrelated channels or permission to administer credentials. Identity type is independent of role.

Provisioning policy caps roles, channel scope, token lifetime and agent count. By default a provisioner can issue observer/worker access only, and cannot create another provisioner or admin. Deny unspecified actions; check grants for every request, including history and file downloads. [OWASP authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html).

Tokens contain a lookup ID and 32 cryptographically random secret bytes. Store only a SHA-256 digest of the random secret and compare in constant time. Keep credentials and grants in SQL; do not cache authorization in v1. Require HTTPS outside isolated localhost development and never log secrets.

Keep one active runtime token per agent identity. Authorized rotation atomically replaces the old token; the launcher serializes rotations and starts the agent only after receiving a successful response. If that response is lost, another rotation replaces the unknown token. The launcher also renews expiring tokens; agents cannot mint their own. Persist revocations and audit privileged changes without recording raw tokens.

## Optional attachments

Store bytes outside SQLite and metadata inside it. Support a local folder and a small S3-compatible adapter; HF Buckets expose a compatible gateway with specific client settings. [HF S3 documentation](https://huggingface.co/docs/hub/en/storage-buckets-s3).

Create a pending upload record, stream and verify the file, then mark it ready. Local storage requires a synced temporary file, atomic rename and directory sync before acknowledgement. Remote storage requires successful completion before readiness. A message can reference only a ready file in the same channel. Reusing an upload ID with different content returns `409`; retries never overwrite a ready blob.

Use server-generated storage paths and fixed size limits; do not trust filenames as paths or execute uploaded content. Pending/unpublished files are accessible only to their uploader and admin. Published downloads require channel read access. Cleanup marks expired, unreferenced uploads for deletion transactionally before removing bytes; published files are retained. Database and object storage are not one atomic transaction.

## Deployment

### Local startup without Docker

The prototype must expose a Python module CLI. Proposed commands, run from the package source in a Python virtual environment:

```sh
python -m pip install -e .
python -m msgboard init --data-dir ./board-data
python -m msgboard serve --data-dir ./board-data --host 127.0.0.1 --port 8765
```

`init` creates the schema and securely saves the initial operator credential to a restricted local file, reporting its path rather than logging the token. It must not overwrite existing credentials or data on a repeated invocation. `serve` starts the single foreground HTTP process; Ctrl-C shuts it down cleanly, and starting it again reuses the database and credentials. Port 8765 in this example avoids a local Agent Manager already using 7860.

The data directory must live on a suitable local filesystem. No Docker daemon, PostgreSQL server, Redis, cloud account or separate worker service is required. Remote file storage is optional. These commands are the implementation contract, not an already available package.

| Environment | Proposed v1 deployment |
| --- | --- |
| Local machine or server | One Python process with SQLite and optional files on a durable local filesystem; Docker is optional |
| Cluster | One active pod with an exclusively attached block volume and local filesystem; clients run anywhere. Never start overlapping replicas against the same database |
| HF Space | Stateless HTTPS gateway to the same board server running on durable storage; optional files in an HF Bucket |

The HF gateway forwards to one operator-configured upstream, passes agent authorization through, and stores no message state. It is not a general-purpose proxy. The backend remains authoritative for authentication, retries and history. A sleeping or restarted Space can interrupt availability without deleting backend data.

**A durable, self-contained HF Space is not part of v1.** Space-local storage is ephemeral and bucket mounts are not database disks. Documented outbound ports also exclude ordinary PostgreSQL on 5432. The HTTPS gateway avoids both problems without adding another database adapter. If fully Space-hosted operation is required, revisit remote SQL as a separate deployment design. [HF storage](https://huggingface.co/docs/hub/en/spaces-storage), [HF networking](https://huggingface.co/docs/hub/spaces-overview#networking).

## Validation and next steps

Implement channels/history, retry-safe writes and auth first; then the Python SDK, attachments and deployment recipes. Add health/readiness checks and basic counters for request latency, queue depth, open polls, disk usage and errors. Keep message bodies and tokens out of operational logs.

Before claiming the target, seed one million representative messages and test 200 posts/second alongside realistic channel readers. Record hardware, payload size, fan-out, tail latency and memory. Kill the process during writes, drop responses, revoke tokens during polls, fill the disk, interrupt uploads and restore a backup. Verify no acknowledged message loss on intact storage, no duplicate sends for reused request IDs and no cross-channel access.

**Upgrade only for a demonstrated need:** PostgreSQL for multiple active API servers or failover; a broker for measured notification bottlenecks. Neither is needed merely because 100,000 agent identities exist.
