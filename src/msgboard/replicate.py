"""Snapshot replication of a board to an object store (an HF bucket, or any directory).

The live database stays on local disk. A background thread periodically takes a
consistent SQLite snapshot (backup API) and uploads it, together with any new
attachment files. The snapshot, the LATEST pointer to it and the lease go up in
one batch, so a push is one upload: a bucket rate-limits uploads, and a
rate-limited one can take minutes. On startup an empty data directory is
restored from the newest snapshot.

One instance owns the replica at a time. Owners are numbered: an instance that
takes the replica claims the next epoch (a file in claims/), and its snapshots
carry that epoch. A bucket has no compare-and-swap, so a late upload from an
instance that was replaced can still land, but it never outranks its
successor's claim or snapshots, and a restore picks the newest owner's snapshot.

An instance that wants the replica, such as the replacement container in a
restart, asks the owner first (HANDOVER.json, see request_handover). The owner
stops taking writes, pushes a final snapshot and releases the lease to it. That
has no deadline while the owner's uploads keep landing: during a slow one it
uploads a small heartbeat (beats/), and the instance asking counts it dead only
after `stale_after` seconds in which nothing from it landed. After SIGTERM there
is a deadline: HF Spaces kill a container about 10 s later.

Writes acknowledged since the last completed snapshot are lost if the machine
disappears; a graceful shutdown (SIGTERM) pushes a final snapshot first.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("msgboard.replicate")

LATEST = "LATEST.json"
LEASE = "LEASE.json"
HANDOVER = "HANDOVER.json"
CLAIMS = "claims/"   # claims/<epoch>-<since, ms>-<instance>.json, one per owner
BEATS = "beats/"     # beats/<instance>.json, uploaded while an upload is slow
KEEP_CLAIMS = 20
# Lease protocol. 2: owners answer HANDOVER.json requests. 1 (leases without "protocol"): an owner
# hands over only when it stops, so its successor must let the platform send it SIGTERM.
PROTOCOL = 2
SLOW_CALL = 10.0  # bucket calls slower than this many seconds are logged


# ------------------------------------------------------------------ object stores


class DirStore:
    """Objects as files under a directory (tests, or a mounted volume)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, items: list[tuple[str | Path | bytes, str]]) -> None:
        for source, dest in items:
            target = self.root / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
            if isinstance(source, bytes):
                tmp.write_bytes(source)
            else:
                shutil.copyfile(source, tmp)
            os.replace(tmp, target)

    def get(self, remote: str, local: str | Path) -> bool:
        src = self.root / remote
        if not src.exists():
            return False
        shutil.copyfile(src, local)
        return True

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and ".tmp-" not in p.name)

    def versions(self, paths: list[str]) -> dict[str, str]:
        """A version tag for each path that exists; it changes whenever the object does."""
        out = {}
        for path in paths:
            try:
                out[path] = hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            except FileNotFoundError:
                pass
        return out

    def delete(self, paths: list[str]) -> None:
        for p in paths:
            (self.root / p).unlink(missing_ok=True)


class HfBucketStore:
    """Objects in a Hugging Face bucket through the Hub API (not a FUSE mount)."""

    def __init__(self, bucket_id: str, prefix: str = "", token: str | None = None):
        import huggingface_hub  # optional dependency

        self.hub = huggingface_hub
        self.bucket_id = bucket_id
        self.prefix = prefix.strip("/")
        self.token = token

    def _p(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def _timed(self, what: str, fn, *args, **kwargs):
        # Uploads go through Xet, which retries rate limits (429) for minutes before failing.
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            took = time.monotonic() - started
            if took > SLOW_CALL:
                log.warning("bucket %s took %.0f s", what, took)

    def put(self, items):
        self._timed(f"upload of {', '.join(d for _, d in items)}", self.hub.batch_bucket_files, self.bucket_id,
                    add=[(s, self._p(d)) for s, d in items], token=self.token)

    def get(self, remote: str, local) -> bool:
        local = Path(local)
        local.unlink(missing_ok=True)
        self._timed(f"download of {remote}", self.hub.download_bucket_files, self.bucket_id,
                    [(self._p(remote), str(local))], token=self.token)
        return local.exists()

    def list(self, prefix: str) -> list[str]:
        full = self._p(prefix)
        try:
            entries = self._timed(f"listing of {prefix}", lambda: list(
                self.hub.list_bucket_tree(self.bucket_id, prefix=full, recursive=True, token=self.token)))
            names = [e.path for e in entries if getattr(e, "type", "file") != "directory"
                     and type(e).__name__ != "BucketFolder"]
        except Exception as exc:  # missing prefix is not an error
            if "404" in str(exc) or "not found" in str(exc).lower():
                return []
            raise
        cut = len(self.prefix) + 1 if self.prefix else 0
        return sorted(n[cut:] for n in names)

    def versions(self, paths: list[str]) -> dict[str, str]:
        """The Xet hash of each path that exists: one Hub API call, nothing downloaded."""
        cut = len(self.prefix) + 1 if self.prefix else 0
        infos = self._timed("paths-info", lambda: list(self.hub.get_bucket_paths_info(
            self.bucket_id, [self._p(p) for p in paths], token=self.token)))
        return {info.path[cut:]: info.xet_hash for info in infos}

    def delete(self, paths: list[str]) -> None:
        if paths:
            self.hub.batch_bucket_files(self.bucket_id, delete=[self._p(p) for p in paths], token=self.token)


def store_from_url(url: str, token: str | None = None):
    """`hf://buckets/<namespace>/<bucket>[/<prefix>]` or a plain directory path."""
    if url.startswith("hf://buckets/"):
        parts = url[len("hf://buckets/"):].strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("expected hf://buckets/<namespace>/<bucket>[/<prefix>]")
        return HfBucketStore("/".join(parts[:2]), "/".join(parts[2:]),
                             token or os.environ.get("HF_TOKEN"))
    return DirStore(url)


# -------------------------------------------------------------------- replicator


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(store, remote: str) -> dict | None:
    with tempfile.TemporaryDirectory() as d:
        local = Path(d) / "x.json"
        try:
            if not store.get(remote, local):
                return None
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise
        return json.loads(local.read_text())


class _Watch:
    """Reads small JSON files from a store, downloading each one only when its version changed."""

    def __init__(self, store):
        self.store = store
        self._seen: dict[str, tuple[str, dict]] = {}
        self.versions: dict[str, str] = {}   # of the last read

    def read(self, *paths: str) -> list[dict | None]:
        self.versions = self.store.versions(list(paths))
        out = []
        for path in paths:
            version, seen = self.versions.get(path), self._seen.get(path)
            if version is None:
                out.append(None)
            elif seen is not None and seen[0] == version:
                out.append(seen[1])
            else:
                data = _read_json(self.store, path)
                if data is not None:
                    self._seen[path] = (version, data)
                out.append(data)
        return out


# Ownership ranks: (epoch, since in ms, instance). A higher epoch wins, then the newer instance.


def _claim_name(epoch: int, since: float, instance: str) -> str:
    return f"{CLAIMS}{epoch:08d}-{int(since * 1000)}-{instance}.json"


def _lease_rank(lease: dict) -> tuple[int, int, str]:
    return lease.get("epoch") or 0, int(lease.get("since", 0) * 1000), lease.get("instance", "")


def _claim_ranks(store) -> list[tuple[int, int, str]]:
    ranks = []
    for path in store.list(CLAIMS):
        parts = path[len(CLAIMS):].removesuffix(".json").split("-", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            ranks.append((int(parts[0]), int(parts[1]), parts[2]))
    return ranks


def _top_claim(store, lease: dict | None) -> tuple[int, int, str] | None:
    """The best claim on the replica. Claims are separate files, so a late upload cannot overwrite one,
    as it can the lease; the lease counts as its writer's claim."""
    return max(_claim_ranks(store) + ([_lease_rank(lease)] if lease else []), default=None)


def _snapshot_order(name: str) -> tuple:
    # snapshots/<epoch>-<since, ms>-<instance>-<generation>.db: by owner rank, then by generation, which
    # counts one instance's writes. Names from before epochs (<UTC time>-<instance>-<generation>) come first.
    stem = name.rsplit("/", 1)[-1].removesuffix(".db")
    parts = stem.split("-")
    generation = int(parts[-1]) if parts[-1].isdigit() else -1
    if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1]), parts[2], generation, stem
    return 0, 0, parts[0], generation, stem


def restore(store, data_dir: Path) -> dict | None:
    """Restore board.db and attachment files from the newest snapshot. Returns its manifest or None.

    The newest is the latest by owner and generation among the stored snapshots and the one LATEST
    names. LATEST used to be written after its snapshot, and an instance killed in between left it behind.
    """
    data_dir = Path(data_dir)
    if (data_dir / "board.db").exists():
        raise FileExistsError(f"{data_dir / 'board.db'} exists; refusing to overwrite it with a restore")
    latest = _read_json(store, LATEST)
    names = [p for p in store.list("snapshots/") if p.endswith(".db")] + ([latest["snapshot"]] if latest else [])
    if not names:
        return None
    name = max(names, key=_snapshot_order)
    manifest = latest if latest is not None and latest["snapshot"] == name else {"snapshot": name}
    if manifest is not latest:
        log.warning("LATEST.json names %s, but %s is newer; restoring that", latest and latest["snapshot"], name)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = data_dir / "board.db.restoring"
    tmp.unlink(missing_ok=True)
    if not store.get(name, tmp):
        raise RuntimeError(f"snapshot {name} is missing")
    if "sha256" in manifest and _sha256(tmp) != manifest["sha256"]:
        tmp.unlink()
        raise RuntimeError("snapshot checksum mismatch")
    conn = sqlite3.connect(tmp)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("snapshot failed integrity_check")
        keys = [r[0] for r in conn.execute("SELECT storage_key FROM attachments WHERE state = 'ready'")]
    finally:
        conn.close()
    objects = data_dir / "blobs" / "objects"
    for key in keys:
        dest = objects / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not store.get(f"blobs/{key}", dest):
            log.warning("attachment %s missing from replica", key)
    size = tmp.stat().st_size
    os.replace(tmp, data_dir / "board.db")
    log.info("restored snapshot %s (%s bytes)", name, size)
    return manifest


def _await_owner(store, instance: str, since: float, stale_after: float, poll: float, log_every: float,
                 stop: threading.Event | None) -> tuple[str, dict | None]:
    # request_handover, plus the lease that ended the wait
    watch = _Watch(store)
    last_log = 0.0
    owner, seen, quiet_since = None, None, time.monotonic()
    while stop is None or not stop.is_set():
        try:
            beats = [f"{BEATS}{owner}.json"] if owner else []
            lease, request, *_ = watch.read(LEASE, HANDOVER, *beats)
            if lease is None:
                return "free", None
            owner = lease.get("instance")
            state = (watch.versions.get(LEASE), *map(watch.versions.get, beats))
            if state != seen:   # the owner uploaded something: it is alive, however old its timestamps
                seen, quiet_since = state, time.monotonic()
            if (lease.get("released") and lease.get("handover_to") in (None, instance)
                    and _lease_rank(lease) == _top_claim(store, lease)):   # not a late write from a replaced owner
                return "released", lease
            if lease.get("protocol", 1) < PROTOCOL:
                return "legacy", lease
            if time.monotonic() - quiet_since > stale_after:
                return "stale", lease
            mine = request is not None and request.get("instance") == instance
            newer = request is not None and request.get("since", 0) > since
            if lease.get("since", 0) < since and not mine and not newer:
                store.put([(json.dumps({"instance": instance, "since": since, "at": time.time()}).encode(),
                            HANDOVER)])
            if time.time() - last_log > log_every:
                log.info("waiting for instance %s to hand over the replica", owner)
                last_log = time.time()
        except Exception as exc:  # a failed Hub call: try again
            log.warning("hand-over request: %s", exc)
        if stop is not None and stop.wait(poll):
            break
        if stop is None:
            time.sleep(poll)
    return "stopped", None


def request_handover(store, instance: str, since: float, *, stale_after: float = 300.0, poll: float = 2.0,
                     log_every: float = 10.0, stop: threading.Event | None = None) -> str:
    """Before serving, ask the replica's owner to hand it over to `instance`, and wait until it has.

    The owner (a Replicator) sees the request in HANDOVER.json within its `poll` interval, stops
    taking writes, pushes a final snapshot and releases the lease to `instance`. Only an instance
    that started after the owner (`since`) can ask, and the newest one asking wins.

    Returns why waiting ended: "free" (no owner), "released", "stale" (nothing from the owner, neither
    its lease nor a heartbeat, has changed for `stale_after` s by this caller's clock: it crashed or
    cannot reach the store), "legacy" (an owner that predates these requests and hands over only when
    it stops; see wait_for_release) or "stopped". Replicator.take_over also claims the replica.
    """
    return _await_owner(store, instance, since, stale_after, poll, log_every, stop)[0]


def wait_for_release(store, stale_after: float = 120.0, poll: float = 1.0, log_every: float = 10.0,
                     stop: threading.Event | None = None) -> str:
    """Before restoring, wait for the previous owner to push its final snapshot and release the lease.

    For owners that predate request_handover: they hand over when they stop. Platforms such as
    HF Spaces start the replacement container before stopping the old one, so restoring at once
    would miss the old one's last writes. Returns why waiting ended: "free", "released", "stale"
    (the lease has not changed for `stale_after` s, e.g. the owner crashed) or "stopped" (`stop` was set).
    """
    watch = _Watch(store)
    last_log = 0.0
    seen, quiet_since = None, time.monotonic()
    while stop is None or not stop.is_set():
        lease, = watch.read(LEASE)
        if lease is None:
            return "free"
        if lease.get("released"):
            return "released"
        if watch.versions.get(LEASE) != seen:
            seen, quiet_since = watch.versions.get(LEASE), time.monotonic()
        if time.monotonic() - quiet_since > stale_after:
            return "stale"
        if time.time() - last_log > log_every:
            log.info("waiting for instance %s to hand over the replica", lease.get("instance"))
            last_log = time.time()
        time.sleep(poll)
    return "stopped"


class Replicator:
    """Background snapshot uploader owned by a running Board."""

    def __init__(self, store, data_dir: Path, interval: float = 10.0, keep: int = 5, heartbeat: float = 30.0,
                 poll: float = 5.0, reclaim_after: float = 120.0, regrant_after: float | None = None,
                 confirm_after: float = 10.0, stop: threading.Event | None = None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.interval = interval
        self.keep = keep
        self.heartbeat = heartbeat
        self.poll = poll                      # how often to look for hand-over requests
        self.reclaim_after = reclaim_after    # take the replica back if its new owner never takes it
        # release it to a newer instance that asks if the one it was released to has not taken it by then
        self.regrant_after = 3 * poll if regrant_after is None else regrant_after
        self.confirm_after = confirm_after    # after claiming, while a newer instance is asking too
        self._last_heartbeat = 0.0
        self.instance = uuid.uuid4().hex[:12]
        self.since = time.time()              # the newest instance wins the replica
        self.epoch: int | None = None         # this instance's owner number, once it has claimed the replica
        self._grant: int | None = None        # the epoch its release offers the next owner
        self.generation_fn = lambda: 0
        self._pushed_generation = None
        self._uploaded_blobs: set[str] | None = None
        self._stop = stop or threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watch = _Watch(store)
        self._refused: set[str] = set()       # instances handed the replica that never took it
        self._uploading: float | None = None  # when the upload (or hand-over drain) in progress started
        self._last_beat = 0.0
        self._beats_stop = threading.Event()
        self._tidied = False
        self._serving = False                 # started serving; until then its writes are nobody's
        self.fenced = False
        self.handed_over = False
        # Set by the Board: stop taking writes and return once accepted ones are committed; take them again.
        self.quiesce = lambda: None
        self.resume = lambda: None
        self.events: list[tuple[float, str]] = []
        self._events_sent = 0
        self.status = {"instance": self.instance, "epoch": None, "last_push_at": None, "last_push_seconds": None,
                       "last_snapshot": None, "last_error": None, "pushes": 0, "fenced": False,
                       "handed_over": False}

    # Ownership: the instance with the best claim (epoch, then the newest) owns the replica. One that
    # finds a better claim than its own stops pushing and taking writes instead of overwriting.
    def _rank(self) -> tuple[int, int, str]:
        return self.epoch, int(self.since * 1000), self.instance

    def _owns(self, lease: dict | None) -> bool:
        return lease is not None and lease.get("instance") == self.instance and lease.get("epoch") == self.epoch

    def _replaced_by(self, lease: dict | None) -> tuple | None:
        top = _top_claim(self.store, lease)
        return top if top is not None and top > self._rank() else None

    def _lease(self, released: bool = False, handover_to: str | None = None,
               snapshot: str | None = None) -> tuple[bytes, str]:
        return json.dumps({"protocol": PROTOCOL, "instance": self.instance, "since": self.since,
                           "epoch": self.epoch, "serving": self._serving, "heartbeat": time.time(),
                           "released": released, "handover_to": handover_to,
                           "grant": self._grant if released else None,
                           "snapshot": snapshot or self.status["last_snapshot"]}).encode(), LEASE

    def _events(self) -> tuple[list[tuple[bytes, str]], int]:
        # lifecycle events ride along with the next lease write instead of costing an upload each
        n = len(self.events)
        if n == self._events_sent:
            return [], n
        return [(json.dumps({"instance": self.instance, "events": self.events[:n]}).encode(),
                 f"events/{self.instance}.json")], n

    def _upload(self, items: list) -> None:
        self._uploading = time.monotonic()
        try:
            self.store.put(items)
        finally:
            self._uploading = None

    def _put_lease(self, released: bool = False, handover_to: str | None = None, claim: bool = False) -> None:
        events, n = self._events()
        items = [self._lease(released, handover_to)] + events
        if claim:  # first: a batch that fails halfway still records the claim
            items.insert(0, (json.dumps({"instance": self.instance, "since": self.since, "epoch": self.epoch,
                                         "at": time.time()}).encode(), _claim_name(self.epoch, self.since,
                                                                                   self.instance)))
        self._upload(items)
        self._last_heartbeat = time.time()
        self._events_sent = n

    def _beat(self) -> None:
        # While an upload is slow (a rate-limited one takes minutes), upload a small heartbeat now and then:
        # an instance waiting for the replica counts this one dead only when nothing from it lands for a while.
        while not self._beats_stop.wait(self.heartbeat / 4):
            started = self._uploading
            if self.fenced or started is None or time.monotonic() - max(started, self._last_beat) < self.heartbeat:
                continue
            self._last_beat = time.monotonic()
            try:
                self.store.put([(json.dumps({"instance": self.instance, "epoch": self.epoch,
                                             "at": time.time()}).encode(), f"{BEATS}{self.instance}.json")])
            except Exception as exc:
                log.warning("heartbeat upload failed: %s", exc)

    def _fence(self, by: tuple) -> None:
        if not self.fenced:
            self.fenced = self.status["fenced"] = True
            log.error("instance %s has the replica (epoch %s); this instance stops pushing and taking writes",
                      by[2], by[0])
            self.quiesce()

    def record(self, event: str) -> None:
        """Append a lifecycle event to events/<instance>.json in the replica (diagnostics)."""
        self.events.append((time.time(), event))

    def _retrying(self, what: str, fn):
        while True:  # an upload can fail after minutes of rate limiting
            try:
                return fn()
            except Exception as exc:
                log.warning("%s failed, retrying: %s", what, exc)
                if self._stop.wait(self.poll):
                    raise RuntimeError(f"stopped before {what} succeeded") from exc

    def claim(self, expect: dict | None = None) -> bool | None:
        """Take the replica under the next epoch, which this instance's snapshots then carry.

        Returns False if another instance has a better claim (a newer instance took the replica, or
        it was released to one): this instance must not serve or push. With `expect`, the lease that
        request_handover judged stale, returns None if the replica changed hands since.
        """
        def take():
            lease, = self._watch.read(LEASE)
            top = _top_claim(self.store, lease)
            if expect is not None:
                # Changed: its owner is alive after all. A claim newer than it that never showed in the lease
                # while it looked dead is from an instance just as dead.
                if lease != expect:
                    return None
            elif lease is not None and top is not None:
                if lease.get("released") and top == _lease_rank(lease):
                    if lease.get("handover_to") not in (None, self.instance):
                        log.warning("the replica was released to instance %s", lease["handover_to"])
                        return False
                elif top[1] > int(self.since * 1000):
                    log.warning("newer instance %s has the replica", top[2])
                    return False
            granted = lease.get("grant") if lease and lease.get("handover_to") == self.instance else None
            self.epoch = self.status["epoch"] = max((top[0] if top else 0) + 1, granted or 0)
            try:
                self._put_lease(claim=True)
            except BaseException:
                self.epoch = self.status["epoch"] = None
                raise
            return True

        claimed = self._retrying("taking the replica lease", take)
        if not claimed:
            return claimed
        lease, request = self._retrying("checking the replica lease", lambda: self._watch.read(LEASE, HANDOVER))
        if request is not None and request.get("since", 0) > self.since:
            # A newer instance asked too, so the previous owner may have released the replica to it while
            # this claim was uploading. Give such a release time to land before serving.
            self._stop.wait(self.confirm_after)
            lease, = self._retrying("checking the replica lease", lambda: self._watch.read(LEASE))
        replaced = self._retrying("checking the replica lease", lambda: self._replaced_by(lease))
        if replaced is not None:
            log.warning("instance %s took the replica (epoch %s)", replaced[2], replaced[0])
            self.fenced = self.status["fenced"] = True   # so stop() does not release what it does not own
            return False
        log.info("took the replica: epoch %s", self.epoch)
        return True

    def take_over(self, stale_after: float = 300.0, poll: float = 2.0) -> str:
        """Before serving: have the replica handed over (request_handover), then claim it.

        Returns how: "free", "released", "stale", "legacy" (claim it in start(), once the old owner has
        stopped: see wait_for_release), "superseded" (a newer instance has it: do not serve) or "stopped".
        """
        while True:
            how, lease = _await_owner(self.store, self.instance, self.since, stale_after, poll, 10.0, self._stop)
            if how in ("legacy", "stopped"):
                return how
            claimed = self.claim(expect=lease if how == "stale" else None)
            if claimed is not None:
                return how if claimed else "superseded"
            log.info("the replica changed hands while instance %s looked dead; asking again", lease.get("instance"))

    def start(self, generation_fn) -> None:
        self.generation_fn = generation_fn
        self.record("started")
        if self.epoch is None:
            self._serving = True
            owner = self.claim()
        else:
            # Claimed in take_over. Tell the previous owner, which watches for it, that this instance serves
            # now, then check that nobody took the replica meanwhile (while this one restored it, say).
            def mark_serving():
                if self._replaced_by(self._watch.read(LEASE)[0]) is not None:
                    return False
                self._serving = True
                self._put_lease()
                return self._replaced_by(self._watch.read(LEASE)[0]) is None

            owner = self._retrying("checking the replica lease", mark_serving)
        if not owner:
            self._serving = False
            self.fenced = self.status["fenced"] = True
            log.error("this instance does not own the replica: it does not push or take writes")
            self.quiesce()
            return
        self._thread = threading.Thread(target=self._run, name="msgboard-replicator", daemon=True)
        self._thread.start()
        threading.Thread(target=self._beat, name="msgboard-heartbeat", daemon=True).start()

    def _run(self) -> None:
        next_push = time.monotonic() + self.interval
        while not self._stop.wait(self.poll):
            try:
                lease, request = self._watch.read(LEASE, HANDOVER)
                if not self._owns(lease):
                    replaced = self._replaced_by(lease)
                    if replaced is not None:
                        self._fence(replaced)
                        return
                    self._put_lease()  # a late write from an instance this one replaced
                    continue
                if (request is not None and request.get("since", 0) > self.since
                        and request.get("instance") not in self._refused):
                    if self._hand_over(request):
                        return
                    continue
                if time.monotonic() >= next_push:
                    next_push = time.monotonic() + self.interval
                    if self.push_if_changed():
                        continue
                if time.time() - self._last_heartbeat > self.heartbeat:
                    self._put_lease()
            except Exception as exc:  # keep serving; report through metrics
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("replication push failed: %s", exc)

    def _hand_over(self, request: dict) -> bool:
        """A newer instance asked for the replica: stop taking writes, push a final snapshot and release
        the lease to it. Unlike after SIGTERM, there is no deadline while this instance's uploads (or its
        heartbeats, see _beat) keep landing. Before each attempt it checks that nobody took the replica
        meanwhile (the instance asking counts this one dead after stale_after s without either): then it
        must not publish. Returns False if nobody took the replica and this instance took it back."""
        log.info("instance %s asked for the replica; handing over", request["instance"])
        self.record(f"handover-requested {request['instance']}")
        self._uploading = time.monotonic()  # writes in progress can take a while too: heartbeats meanwhile
        try:
            self.quiesce()
        finally:
            self._uploading = None
        self._grant = self.epoch + 1
        while True:
            try:
                with self._lock:
                    lease, = self._watch.read(LEASE)
                    replaced = None if self._owns(lease) else self._replaced_by(lease)
                    if replaced is not None:
                        self._fence(replaced)
                        return True
                    self._push(self.generation_fn(), released=True, handover_to=request["instance"])
                break
            except Exception as exc:  # nothing new can be written now, so keep trying
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("final snapshot for the hand-over failed, retrying: %s", exc)
                if self._stop.wait(self.poll):
                    return True  # stopping: stop() makes the final push
        self.handed_over = self.status["handed_over"] = True
        log.info("released the replica to instance %s", request["instance"])
        released_at = changed_at = time.monotonic()
        version = None
        while not self._stop.wait(self.poll):
            try:
                lease, newer = self._watch.read(LEASE, HANDOVER)
                if self._watch.versions.get(LEASE) != version:
                    version, changed_at = self._watch.versions.get(LEASE), time.monotonic()
                waited = time.monotonic() - released_at
                # the new owner's claim shows in the lease; look for it in claims/ too before changing anything
                replaced = None
                if not self._owns(lease) or waited > min(self.regrant_after, self.reclaim_after):
                    replaced = self._replaced_by(lease)
                if replaced is not None:
                    if lease is not None and lease.get("instance") == replaced[2] and lease.get("serving"):
                        log.info("instance %s took the replica over", replaced[2])
                        return True
                    # Claimed but not serving yet, so it has no writes of its own. If it stops changing the
                    # lease for reclaim_after s, it died first (a platform discards a replaced container):
                    # take the replica back, or nobody would hand it on.
                    if time.monotonic() - changed_at > self.reclaim_after:
                        return self._reclaim(request, replaced[0])
                    continue
                if not self._owns(lease) or not lease.get("released") or lease.get("handover_to") != request["instance"]:
                    self._put_lease(released=True, handover_to=request["instance"])  # a late write hid it
                elif (newer is not None and newer.get("since", 0) > request.get("since", 0)
                        and newer.get("instance") not in self._refused and waited > self.regrant_after):
                    log.info("instance %s asked too, and %s has not taken the replica; releasing it to %s instead",
                             newer["instance"], request["instance"], newer["instance"])
                    self._refused.add(request["instance"])
                    request = newer
                    self._grant += 1  # a late claim from the first one loses to the second one's
                    self._put_lease(released=True, handover_to=request["instance"])
                    released_at = time.monotonic()
                elif waited > self.reclaim_after:
                    return self._reclaim(request, 0)
            except Exception as exc:
                log.warning("waiting for the hand-over: %s", exc)
        return True

    def _reclaim(self, request: dict, above: int) -> bool:
        log.warning("instance %s did not take the replica over and serve within %.0f s; taking it back",
                    request["instance"], self.reclaim_after)
        self._refused.add(request["instance"])
        self.epoch = self.status["epoch"] = max(self._grant, above) + 1  # above any claim its releases allowed
        self._grant = None
        self._put_lease(claim=True)
        with contextlib.suppress(Exception):  # instances still waiting ask again, this time this one
            self.store.delete([HANDOVER])
        self.handed_over = self.status["handed_over"] = False
        self.resume()
        return False

    def stop(self, final_push: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        if final_push and self.epoch is not None and not self.handed_over and not self.fenced:
            self.record("final-push")
            try:
                with self._lock:
                    lease, = self._watch.read(LEASE)
                    replaced = None if self._owns(lease) else self._replaced_by(lease)
                    if replaced is not None:
                        self._fence(replaced)
                    else:
                        self._grant = self.epoch + 1
                        if self._serving and self.generation_fn() != self._pushed_generation:
                            self._push(self.generation_fn(), released=True)  # the next instance may restore now
                        else:  # e.g. stopped after claiming, before serving: nothing of its own to push
                            self._put_lease(released=True)
            except Exception as exc:
                log.error("final snapshot push failed: %s", exc)
        self._beats_stop.set()

    def push_if_changed(self) -> bool:
        with self._lock:
            generation = self.generation_fn()
            if self.epoch is None or self.fenced or self.handed_over or generation == self._pushed_generation:
                return False
            lease, = self._watch.read(LEASE)
            replaced = None if self._owns(lease) else self._replaced_by(lease)
            if replaced is not None:
                self._fence(replaced)
                return False
            self._push(generation)
            self._prune()
            return True

    def _push(self, generation: int, released: bool = False, handover_to: str | None = None) -> None:
        started = time.time()
        with tempfile.TemporaryDirectory(dir=self.data_dir) as d:
            snap = Path(d) / "snapshot.db"
            src = sqlite3.connect(self.data_dir / "board.db")
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)  # one step: a consistent point-in-time copy
                keys = [r[0] for r in dst.execute("SELECT storage_key FROM attachments WHERE state = 'ready'")]
            finally:
                dst.close()
                src.close()
            # 1. attachment files referenced by the snapshot
            if self._uploaded_blobs is None:
                self._uploaded_blobs = {p[len("blobs/"):] for p in self.store.list("blobs/")}
            new = [k for k in keys if k not in self._uploaded_blobs]
            objects = self.data_dir / "blobs" / "objects"
            for i in range(0, len(new), 50):
                batch = [(objects / k, f"blobs/{k}") for k in new[i:i + 50] if (objects / k).exists()]
                if batch:
                    self._upload(batch)
                self._uploaded_blobs.update(new[i:i + 50])
            # 2. the snapshot, the pointer to it and the lease, in one upload
            name = f"snapshots/{self.epoch:08d}-{int(self.since * 1000)}-{self.instance}-{generation}.db"
            manifest = {"snapshot": name, "sha256": _sha256(snap), "size": snap.stat().st_size,
                        "created_at": time.time(), "instance": self.instance, "epoch": self.epoch,
                        "generation": generation}
            events, n = self._events()
            self._upload([(snap, name), (json.dumps(manifest).encode(), LATEST),
                          self._lease(released, handover_to, snapshot=name)] + events)
            self._last_heartbeat = time.time()
            self._events_sent = n
        self._pushed_generation = generation
        self.status.update(last_snapshot=name, last_push_at=time.time(),
                           last_push_seconds=round(time.time() - started, 3), last_error=None,
                           pushes=self.status["pushes"] + 1)

    def _prune(self) -> None:
        snaps = sorted((p for p in self.store.list("snapshots/") if p.endswith(".db")), key=_snapshot_order)
        old = snaps[: max(len(snaps) - self.keep, 0)]
        if not self._tidied:  # once per instance: older claims, and heartbeats of instances that are gone
            self._tidied = True
            claims = sorted(self.store.list(CLAIMS))   # zero-padded epochs: by epoch
            old += claims[: max(len(claims) - KEEP_CLAIMS, 0)]
            old += [p for p in self.store.list(BEATS) if p != f"{BEATS}{self.instance}.json"]
        if old:
            self.store.delete(old)
