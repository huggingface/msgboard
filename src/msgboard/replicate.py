"""Snapshot replication of a board to an object store (an HF bucket, or any directory).

The live database stays on local disk. A background thread periodically takes a
consistent SQLite snapshot (backup API) and uploads it, together with any new
attachment files, then moves the LATEST pointer. An upload call returns only
when the object is stored, so the pointer never names a snapshot that is not
there. On startup an empty data directory is restored from LATEST.

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

    def delete(self, paths: list[str]) -> None:
        for p in paths:
            (self.root / p).unlink(missing_ok=True)


class HfBucketStore:
    """Objects in a Hugging Face bucket through the Hub API (not a FUSE mount)."""

    def __init__(self, bucket_id: str, prefix: str = "", token: str | None = None):
        import huggingface_hub  # loaded only for bucket replicas

        self.hub = huggingface_hub
        self.bucket_id = bucket_id
        self.prefix = prefix.strip("/")
        self.token = token

    def _p(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def put(self, items):
        self.hub.batch_bucket_files(self.bucket_id, add=[(s, self._p(d)) for s, d in items], token=self.token)

    def get(self, remote: str, local) -> bool:
        local = Path(local)
        local.unlink(missing_ok=True)
        self.hub.download_bucket_files(self.bucket_id, [(self._p(remote), str(local))], token=self.token)
        return local.exists()

    def list(self, prefix: str) -> list[str]:
        full = self._p(prefix)
        try:
            entries = self.hub.list_bucket_tree(self.bucket_id, prefix=full, recursive=True, token=self.token)
            names = [e.path for e in entries if getattr(e, "type", "file") != "directory"
                     and type(e).__name__ != "BucketFolder"]
        except Exception as exc:  # missing prefix is not an error
            if "404" in str(exc) or "not found" in str(exc).lower():
                return []
            raise
        cut = len(self.prefix) + 1 if self.prefix else 0
        return sorted(n[cut:] for n in names)

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


def restore(store, data_dir: Path) -> dict | None:
    """Restore board.db and attachment files from the latest snapshot. Returns the manifest or None."""
    data_dir = Path(data_dir)
    if (data_dir / "board.db").exists():
        raise FileExistsError(f"{data_dir / 'board.db'} exists; refusing to overwrite it with a restore")
    latest = _read_json(store, LATEST)
    if latest is None:
        return None
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = data_dir / "board.db.restoring"
    tmp.unlink(missing_ok=True)
    if not store.get(latest["snapshot"], tmp):
        raise RuntimeError(f"LATEST names {latest['snapshot']}, which is missing")
    if _sha256(tmp) != latest["sha256"]:
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
    os.replace(tmp, data_dir / "board.db")
    log.info("restored snapshot %s (%s bytes)", latest["snapshot"], latest["size"])
    return latest


def wait_for_release(store, stale_after: float = 120.0, poll: float = 1.0, log_every: float = 10.0,
                     stop: threading.Event | None = None) -> str:
    """Before restoring, wait for the previous owner to push its final snapshot and release the lease.

    Platforms such as HF Spaces start the replacement container before stopping
    the old one, so restoring immediately would miss the old one's last writes.
    Returns why waiting ended: "free", "released", "stale" (previous owner
    stopped heartbeating, e.g. it crashed) or "stopped" (`stop` was set).
    """
    last_log = 0.0
    while stop is None or not stop.is_set():
        lease = _read_json(store, LEASE)
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

    def __init__(self, store, data_dir: Path, interval: float = 10.0, keep: int = 5, heartbeat: float = 30.0):
        self.store = store
        self.data_dir = Path(data_dir)
        self.interval = interval
        self.keep = keep
        self.heartbeat = heartbeat
        self._last_heartbeat = 0.0
        self.instance = uuid.uuid4().hex[:12]
        self.generation_fn = lambda: 0
        self._pushed_generation = None
        self._uploaded_blobs: set[str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.fenced = False
        self.events: list[tuple[float, str]] = []
        self.status = {"instance": self.instance, "last_push_at": None, "last_push_seconds": None,
                       "last_snapshot": None, "last_error": None, "pushes": 0, "fenced": False}

    # Lease: the newest instance owns the replica. An older instance that is
    # still running (overlapping restart) stops pushing instead of overwriting.
    def take_lease(self, released: bool = False) -> None:
        now = time.time()
        self.store.put([(json.dumps({"instance": self.instance, "heartbeat": now, "released": released,
                                     "snapshot": self.status["last_snapshot"]}).encode(), LEASE)])
        self._last_heartbeat = now

    def _still_owner(self) -> bool:
        lease = _read_json(self.store, LEASE)
        return lease is None or lease.get("instance") == self.instance

    def record(self, event: str, wait: bool = False) -> None:
        """Append a lifecycle event to events/<instance>.json in the replica (diagnostics)."""
        self.events.append((time.time(), event))
        body = json.dumps({"instance": self.instance, "events": self.events}).encode()

        def upload():
            try:
                self.store.put([(body, f"events/{self.instance}.json")])
            except Exception as exc:
                log.warning("could not record event %s: %s", event, exc)

        if wait:
            upload()
        else:
            threading.Thread(target=upload, daemon=True).start()

    def start(self, generation_fn) -> None:
        self.generation_fn = generation_fn
        self.take_lease()
        self.record("started")
        self._thread = threading.Thread(target=self._run, name="msgboard-replicator", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.push_if_changed()
                if not self.fenced and time.time() - self._last_heartbeat > self.heartbeat and self._still_owner():
                    self.take_lease()
            except Exception as exc:  # keep serving; report through metrics
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("replication push failed: %s", exc)

    def stop(self, final_push: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        if final_push:
            self.record("final-push-start")
            try:
                pushed = self.push_if_changed()
                if not self.fenced and self._still_owner():
                    self.take_lease(released=True)  # the next instance may restore now
                self.record("final-push-done" if pushed else
                            ("final-push-fenced" if self.fenced else "final-push-unchanged"), wait=True)
            except Exception as exc:
                log.error("final snapshot push failed: %s", exc)
                self.record(f"final-push-failed: {type(exc).__name__}", wait=True)

    def push_if_changed(self) -> bool:
        with self._lock:
            generation = self.generation_fn()
            if self.fenced or generation == self._pushed_generation:
                return False
            if not self._still_owner():
                self.fenced = self.status["fenced"] = True
                log.error("another instance owns the replica; this instance stops pushing")
                return False
            started = time.time()
            self._push(generation)
            self._pushed_generation = generation
            self.status.update(last_push_at=time.time(), last_push_seconds=round(time.time() - started, 3),
                               last_error=None, pushes=self.status["pushes"] + 1)
            return True

    def _push(self, generation: int) -> None:
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
            # 2. the snapshot, 3. then the pointer to it
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            name = f"snapshots/{stamp}-{self.instance}-{generation}.db"
            manifest = {"snapshot": name, "sha256": _sha256(snap), "size": snap.stat().st_size,
                        "created_at": time.time(), "instance": self.instance, "generation": generation}
            self.store.put([(snap, name)])
            self.store.put([(json.dumps(manifest).encode(), LATEST)])
            self.status["last_snapshot"] = name
        self._prune()

    def _prune(self) -> None:
        snaps = [p for p in self.store.list("snapshots/") if p.endswith(".db")]
        if len(snaps) > self.keep:
            self.store.delete(snaps[: len(snaps) - self.keep])
