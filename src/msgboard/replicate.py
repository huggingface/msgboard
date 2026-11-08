"""Snapshot replication of a board to an object store (an HF bucket, or any directory).

The live database stays on local disk. A background thread periodically takes a
consistent SQLite snapshot (backup API) and uploads it, together with any new
attachment files. The snapshot, the LATEST pointer to it and the lease go up in
one batch, so the pointer never names a snapshot that is not there, and a push is
one upload: a bucket rate-limits uploads, and a rate-limited one can take minutes.
On startup an empty data directory is restored from the newest snapshot.

One instance owns the replica at a time (LEASE.json). An instance that wants it,
such as the replacement container in a restart, asks the owner first
(HANDOVER.json, see request_handover). The owner stops taking writes, pushes a
final snapshot and releases the lease to it, with no deadline. After SIGTERM
there is one: HF Spaces kill a container about 10 s later.

Writes acknowledged since the last completed snapshot are lost if the machine
disappears; a graceful shutdown (SIGTERM) pushes a final snapshot first.
"""

from __future__ import annotations

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

    def read(self, *paths: str) -> list[dict | None]:
        versions = self.store.versions(list(paths))
        out = []
        for path in paths:
            version, seen = versions.get(path), self._seen.get(path)
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


def _snapshot_order(name: str) -> tuple:
    # snapshots/<UTC time>-<instance>-<generation>.db: by time, then by generation within one instance
    stem = name.rsplit("/", 1)[-1].removesuffix(".db")
    stamp, _, rest = stem.partition("-")
    generation = rest.rpartition("-")[2]
    return stamp, int(generation) if generation.isdigit() else -1, stem


def restore(store, data_dir: Path) -> dict | None:
    """Restore board.db and attachment files from the newest snapshot. Returns its manifest or None.

    The newest is the latest by name among the stored snapshots and the one LATEST names. LATEST
    used to be written after its snapshot, and an instance killed in between left it behind.
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


def request_handover(store, instance: str, since: float, *, stale_after: float = 300.0, poll: float = 2.0,
                     log_every: float = 10.0, stop: threading.Event | None = None) -> str:
    """Before serving, ask the replica's owner to hand it over to `instance`, and wait until it has.

    The owner (a Replicator) sees the request in HANDOVER.json within its `poll` interval, stops
    taking writes, pushes a final snapshot and releases the lease to `instance`. Only an instance
    that started after the owner (`since`) can ask, and the newest one asking wins.

    Returns why waiting ended: "free" (no owner), "released", "stale" (the owner has not renewed
    its lease for `stale_after` s: it crashed or cannot reach the store), "legacy" (an owner that
    predates these requests and hands over only when it stops; see wait_for_release) or "stopped".
    """
    watch = _Watch(store)
    last_log = 0.0
    while stop is None or not stop.is_set():
        try:
            lease, request = watch.read(LEASE, HANDOVER)
            if lease is None:
                return "free"
            if lease.get("released") and lease.get("handover_to") in (None, instance):
                return "released"
            if time.time() - lease.get("heartbeat", 0) > stale_after:
                return "stale"
            if lease.get("protocol", 1) < PROTOCOL:
                return "legacy"
            mine = request is not None and request.get("instance") == instance
            newer = request is not None and request.get("since", 0) > since
            if lease.get("since", 0) < since and not mine and not newer:
                store.put([(json.dumps({"instance": instance, "since": since, "at": time.time()}).encode(),
                            HANDOVER)])
            if time.time() - last_log > log_every:
                log.info("waiting for instance %s to hand over the replica", lease.get("instance"))
                last_log = time.time()
        except Exception as exc:  # a failed Hub call: try again
            log.warning("hand-over request: %s", exc)
        if stop is not None and stop.wait(poll):
            break
        if stop is None:
            time.sleep(poll)
    return "stopped"


def wait_for_release(store, stale_after: float = 120.0, poll: float = 1.0, log_every: float = 10.0,
                     stop: threading.Event | None = None) -> str:
    """Before restoring, wait for the previous owner to push its final snapshot and release the lease.

    For owners that predate request_handover: they hand over when they stop. Platforms such as
    HF Spaces start the replacement container before stopping the old one, so restoring at once
    would miss the old one's last writes. Returns why waiting ended: "free", "released", "stale"
    (previous owner stopped heartbeating, e.g. it crashed) or "stopped" (`stop` was set).
    """
    watch = _Watch(store)
    last_log = 0.0
    while stop is None or not stop.is_set():
        lease, = watch.read(LEASE)
        if lease is None:
            return "free"
        if lease.get("released"):
            return "released"
        if time.time() - lease.get("heartbeat", lease.get("since", 0)) > stale_after:
            return "stale"
        if time.time() - last_log > log_every:
            log.info("waiting for instance %s to hand over the replica", lease.get("instance"))
            last_log = time.time()
        time.sleep(poll)
    return "stopped"


class Replicator:
    """Background snapshot uploader owned by a running Board."""

    def __init__(self, store, data_dir: Path, interval: float = 10.0, keep: int = 5, heartbeat: float = 30.0,
                 poll: float = 5.0, reclaim_after: float = 120.0, stop: threading.Event | None = None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.interval = interval
        self.keep = keep
        self.heartbeat = heartbeat
        self.poll = poll                      # how often to look for hand-over requests
        self.reclaim_after = reclaim_after    # take the replica back if its new owner never takes it
        self._last_heartbeat = 0.0
        self.instance = uuid.uuid4().hex[:12]
        self.since = time.time()              # the newest instance wins the replica
        self.generation_fn = lambda: 0
        self._pushed_generation = None
        self._uploaded_blobs: set[str] | None = None
        self._stop = stop or threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watch = _Watch(store)
        self._refused: set[str] = set()       # instances handed the replica that never took it
        self.fenced = False
        self.handed_over = False
        # Set by the Board: stop taking writes and return once accepted ones are committed; take them again.
        self.quiesce = lambda: None
        self.resume = lambda: None
        self.events: list[tuple[float, str]] = []
        self._events_sent = 0
        self.status = {"instance": self.instance, "last_push_at": None, "last_push_seconds": None,
                       "last_snapshot": None, "last_error": None, "pushes": 0, "fenced": False,
                       "handed_over": False}

    # Lease: the newest instance owns the replica. An older instance that is
    # still running (overlapping restart) stops pushing instead of overwriting.
    def _lease(self, released: bool = False, handover_to: str | None = None,
               snapshot: str | None = None) -> tuple[bytes, str]:
        return json.dumps({"protocol": PROTOCOL, "instance": self.instance, "since": self.since,
                           "heartbeat": time.time(), "released": released, "handover_to": handover_to,
                           "snapshot": snapshot or self.status["last_snapshot"]}).encode(), LEASE

    def _events(self) -> tuple[list[tuple[bytes, str]], int]:
        # lifecycle events ride along with the next lease write instead of costing an upload each
        n = len(self.events)
        if n == self._events_sent:
            return [], n
        return [(json.dumps({"instance": self.instance, "events": self.events[:n]}).encode(),
                 f"events/{self.instance}.json")], n

    def take_lease(self, released: bool = False, handover_to: str | None = None) -> None:
        events, n = self._events()
        self.store.put([self._lease(released, handover_to)] + events)
        self._last_heartbeat = time.time()
        self._events_sent = n

    def _newer_owner(self, lease: dict | None) -> bool:
        # A lease from an older instance is a late write (or the one this instance took over from).
        return lease is not None and lease.get("instance") != self.instance and lease.get("since", 0) > self.since

    def _fence(self, lease: dict) -> None:
        if not self.fenced:
            self.fenced = self.status["fenced"] = True
            log.error("instance %s owns the replica; this instance stops pushing and taking writes",
                      lease.get("instance"))
            self.quiesce()

    def record(self, event: str) -> None:
        """Append a lifecycle event to events/<instance>.json in the replica (diagnostics)."""
        self.events.append((time.time(), event))

    def start(self, generation_fn) -> None:
        self.generation_fn = generation_fn
        self.record("started")
        while True:  # an upload can fail after minutes of rate limiting; the board must not start unowned
            try:
                self.take_lease()
                break
            except Exception as exc:
                log.warning("could not take the replica lease, retrying: %s", exc)
                if self._stop.wait(self.poll):
                    raise RuntimeError("stopped before taking the replica lease") from exc
        self._thread = threading.Thread(target=self._run, name="msgboard-replicator", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        next_push = time.monotonic() + self.interval
        while not self._stop.wait(self.poll):
            try:
                lease, request = self._watch.read(LEASE, HANDOVER)
                if self._newer_owner(lease):
                    self._fence(lease)
                    return
                if (request is not None and request.get("since", 0) > self.since
                        and request.get("instance") not in self._refused):
                    if self._hand_over(request):
                        return
                    continue
                if time.monotonic() >= next_push:
                    next_push = time.monotonic() + self.interval
                    if self.push_if_changed():
                        continue
                if lease is None or lease.get("instance") != self.instance:  # e.g. an older instance's late write
                    self.take_lease()
                elif time.time() - self._last_heartbeat > self.heartbeat:
                    self.take_lease()
            except Exception as exc:  # keep serving; report through metrics
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("replication push failed: %s", exc)

    def _hand_over(self, request: dict) -> bool:
        """A newer instance asked for the replica: stop taking writes, push a final snapshot and release
        the lease to it. Unlike after SIGTERM, nothing is in a hurry here. Returns False if it never took
        the replica and this instance took it back."""
        log.info("instance %s asked for the replica; handing over", request["instance"])
        self.record(f"handover-requested {request['instance']}")
        self.quiesce()
        while True:
            try:
                with self._lock:
                    self._push(self.generation_fn(), released=True, handover_to=request["instance"])
                break
            except Exception as exc:  # nothing new can be written now, so keep trying
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("final snapshot for the hand-over failed, retrying: %s", exc)
                if self._stop.wait(self.poll):
                    return True  # stopping: stop() makes the final push
        self.handed_over = self.status["handed_over"] = True
        log.info("released the replica to instance %s", request["instance"])
        released_at = time.monotonic()
        while not self._stop.wait(self.poll):
            try:
                lease, newer = self._watch.read(LEASE, HANDOVER)
                if lease is not None and lease.get("instance") != self.instance:
                    log.info("instance %s took the replica over", lease.get("instance"))
                    return True
                if newer is not None and newer.get("since", 0) > request.get("since", 0):
                    log.info("instance %s asked too; releasing the replica to it instead", newer["instance"])
                    self._refused.add(request["instance"])
                    request = newer
                    self.take_lease(released=True, handover_to=request["instance"])
                    released_at = time.monotonic()
                elif time.monotonic() - released_at > self.reclaim_after:
                    log.warning("instance %s did not take the replica over in %.0f s; taking it back",
                                request["instance"], self.reclaim_after)
                    self._refused.add(request["instance"])
                    self.take_lease()
                    self.handed_over = self.status["handed_over"] = False
                    self.resume()
                    return False
            except Exception as exc:
                log.warning("waiting for the hand-over: %s", exc)
        return True

    def stop(self, final_push: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        if final_push and not self.handed_over and not self.fenced:
            self.record("final-push")
            try:
                with self._lock:
                    lease, = self._watch.read(LEASE)
                    if self._newer_owner(lease):
                        self._fence(lease)
                    elif self.generation_fn() != self._pushed_generation:
                        self._push(self.generation_fn(), released=True)  # the next instance may restore now
                    else:
                        self.take_lease(released=True)
            except Exception as exc:
                log.error("final snapshot push failed: %s", exc)

    def push_if_changed(self) -> bool:
        with self._lock:
            generation = self.generation_fn()
            if self.fenced or self.handed_over or generation == self._pushed_generation:
                return False
            lease, = self._watch.read(LEASE)
            if self._newer_owner(lease):
                self._fence(lease)
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
                    self.store.put(batch)
                self._uploaded_blobs.update(new[i:i + 50])
            # 2. the snapshot, the pointer to it and the lease, in one upload
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            name = f"snapshots/{stamp}-{self.instance}-{generation}.db"
            manifest = {"snapshot": name, "sha256": _sha256(snap), "size": snap.stat().st_size,
                        "created_at": time.time(), "instance": self.instance, "generation": generation}
            events, n = self._events()
            self.store.put([(snap, name), (json.dumps(manifest).encode(), LATEST),
                            self._lease(released, handover_to, snapshot=name)] + events)
            self._last_heartbeat = time.time()
            self._events_sent = n
        self._pushed_generation = generation
        self.status.update(last_snapshot=name, last_push_at=time.time(),
                           last_push_seconds=round(time.time() - started, 3), last_error=None,
                           pushes=self.status["pushes"] + 1)

    def _prune(self) -> None:
        snaps = sorted((p for p in self.store.list("snapshots/") if p.endswith(".db")), key=_snapshot_order)
        if len(snaps) > self.keep:
            self.store.delete(snaps[: len(snaps) - self.keep])
