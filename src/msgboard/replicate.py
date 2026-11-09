"""Snapshot replication of a board to an object store (an HF bucket, or any directory).

The live database stays on local disk. A background thread periodically takes a
consistent SQLite snapshot (backup API) and uploads it, together with any new
attachment files. The snapshot, the LATEST pointer to it and the lease go up in
one batch, so a push is one upload: a bucket rate-limits uploads, and a
rate-limited one can take minutes. On startup an empty data directory is
restored from the newest snapshot.

One instance owns the replica at a time. Owners are numbered: an instance that
takes the replica wins the next epoch in an election in claims/ ("Ownership"
below), and its snapshots carry that epoch. A bucket has no compare-and-swap, but
of the instances claiming an epoch at most one wins, however slow their uploads.
A late upload from an instance that was replaced can still land, but it never
outranks its successor's claim or snapshots, and a restore picks the newest
owner's snapshot.

An instance that wants the replica, such as the replacement container in a
restart, asks the owner first (HANDOVER.json, see request_handover). The owner
stops taking writes, pushes a final snapshot and releases the lease to it. That
has no deadline while the owner's uploads keep landing: during a slow one it
uploads a small heartbeat (beats/), and the instance asking counts it dead only
after `stale_after` seconds in which nothing from it landed. After SIGTERM there
is a deadline: HF Spaces kill a container about 10 s later. If the instance it
released the replica to goes as quiet as that before it serves (it uploads
heartbeats while it claims and restores), the owner takes the replica back by
winning a newer epoch the same way; it gives way if that instance started
serving meanwhile, so a slow successor keeps what it acknowledged.

The one timing assumption is that window: an owner that was serving when nothing
from it landed for `stale_after` seconds counts as dead. If it was alive after
all, it loses what it acknowledged since its last snapshot, as in a crash, and
whatever it acknowledges until it sees the newer claim.

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
KEEP_CLAIMS = 20    # epochs of claims kept below the owner's
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


# Ownership. An instance takes the replica in two uploads: a claim, claims/<epoch>-<since ms>-<instance>.json,
# then, once no other claim stands in its way, a .won marker next to it; one that gives way uploads a
# .withdrawn marker instead. Ranks are (epoch, since in ms, instance), and the owner is the best claim that
# won and was not withdrawn. An owner uploads a .serving marker with its lease before it takes writes, then
# checks the claims once more: see Replicator._acquire for why that pair of steps needs no timing.


def _claim_path(rank: tuple, kind: str = "json") -> str:
    return f"{CLAIMS}{rank[0]:08d}-{rank[1]}-{rank[2]}.{kind}"


def _lease_rank(lease: dict) -> tuple[int, int, str]:
    return lease.get("epoch") or 0, int(lease.get("since", 0) * 1000), lease.get("instance", "")


def _claims(store) -> dict[tuple[int, int, str], set[str]]:
    """claims/ as {rank: the markers it has ("json", "won", "withdrawn", "serving")}."""
    claims: dict[tuple[int, int, str], set[str]] = {}
    for path in store.list(CLAIMS):
        stem, _, kind = path[len(CLAIMS):].rpartition(".")
        parts = stem.split("-", 2)
        if (kind in ("json", "won", "withdrawn", "serving") and len(parts) == 3 and parts[0].isdigit()
                and parts[1].isdigit()):
            claims.setdefault((int(parts[0]), int(parts[1]), parts[2]), set()).add(kind)
    return claims


def _authority(claims: dict, lease: dict | None) -> tuple[int, int, str] | None:
    """The owner's rank: the best claim that won and was not withdrawn. The lease counts as its writer's,
    which writes one only once it owns the replica (and owners from before won markers wrote none)."""
    won = [rank for rank, kinds in claims.items() if "won" in kinds and "withdrawn" not in kinds]
    return max(won + ([_lease_rank(lease)] if lease and lease.get("instance") else []), default=None)


def _contenders(claims: dict, parent: int) -> dict[tuple[int, int, str], set[str]]:
    # claims on the epochs after `parent` that still stand
    return {rank: kinds for rank, kinds in claims.items() if rank[0] > parent and "withdrawn" not in kinds}


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
                 stop: threading.Event | None) -> tuple[str, tuple | None]:
    # request_handover, plus what ended the wait: (lease, owner rank, the claims above it) for "stale"
    watch = _Watch(store)
    me = int(since * 1000)
    last_log = 0.0
    seen, quiet_since = None, time.monotonic()
    while stop is None or not stop.is_set():
        try:
            lease, request = watch.read(LEASE, HANDOVER)
            claims = _claims(store)
            owner = _authority(claims, lease)
            if owner is None:
                return "free", None
            # Signs of life: from the owner (its lease, markers, heartbeat) and from instances claiming after it.
            above = _contenders(claims, owner[0])
            beats = [f"{BEATS}{i}.json" for i in sorted({owner[2]} | {rank[2] for rank in above})]
            state = (owner, tuple(sorted(claims.get(owner, ()))), watch.versions.get(LEASE),
                     tuple(sorted((rank, tuple(sorted(kinds))) for rank, kinds in above.items())),
                     tuple(sorted(store.versions(beats).items())))
            if state != seen:   # something landed: alive, however old its timestamps
                seen, quiet_since = state, time.monotonic()
            if lease is not None and _lease_rank(lease) == owner:   # not a late write from a replaced owner
                if lease.get("released") and lease.get("handover_to") in (None, instance):
                    return "released", None
                if lease.get("protocol", 1) < PROTOCOL:
                    return "legacy", None
            if time.monotonic() - quiet_since > stale_after:
                return "stale", (lease, owner, set(above))
            mine = request is not None and request.get("instance") == instance
            newer = request is not None and request.get("since", 0) > since
            if owner[1] < me and not mine and not newer:
                store.put([(json.dumps({"instance": instance, "since": since, "at": time.time()}).encode(),
                            HANDOVER)])
                quiet_since = time.monotonic()  # the owner can answer only now, however long the upload took
            if time.time() - last_log > log_every:
                log.info("waiting for instance %s to hand over the replica", owner[2])
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

    Returns why waiting ended: "free" (no owner), "released", "stale" (nothing from the owner, or from
    an instance claiming after it, has changed for `stale_after` s by this caller's clock: they crashed
    or cannot reach the store), "legacy" (an owner that predates these requests and hands over only
    when it stops; see wait_for_release) or "stopped". Replicator.take_over then claims the replica.
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
                 poll: float = 5.0, stale_after: float = 300.0, reclaim_after: float | None = None,
                 regrant_after: float | None = None, stop: threading.Event | None = None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.interval = interval
        self.keep = keep
        self.heartbeat = heartbeat
        self.poll = poll                      # how often to look for hand-over requests
        self.stale_after = stale_after        # an instance from which nothing lands for this long is dead
        # take the replica back if nothing from the instance it was released to lands for this long
        self.reclaim_after = stale_after if reclaim_after is None else reclaim_after
        # release it to a newer instance that asks if the one it was released to has not claimed it by then
        self.regrant_after = 3 * poll if regrant_after is None else regrant_after
        self._last_heartbeat = 0.0
        self.instance = uuid.uuid4().hex[:12]
        self.since = time.time()              # the newest instance wins the replica
        self.epoch: int | None = None         # this instance's owner number, once it has won the replica
        self.generation_fn = lambda: 0
        self._pushed_generation = None
        self._uploaded_blobs: set[str] | None = None
        self._stop = stop or threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watch = _Watch(store)
        self._refused: set[tuple] = set()     # (instance, at) of hand-over requests it gave up on
        self._uploading: float | None = None  # when the upload (or hand-over drain) in progress started
        self._last_beat = 0.0
        self._beats: threading.Thread | None = None
        self._beats_stop = threading.Event()
        self._acquiring = False
        self._tidied = False
        self._serving = False                 # started serving; until then its writes are nobody's
        self.fenced = False
        self.handed_over = False
        # Set by the Board: stop taking writes and return once accepted ones are committed; take them again.
        self.quiesce = lambda: None
        self.resume = lambda: None
        # Set by the Space app: another instance owns the replica now, so exit and let the platform restart.
        self.on_fenced = lambda: None
        self.events: list[tuple[float, str]] = []
        self._events_sent = 0
        self.status = {"instance": self.instance, "epoch": None, "last_push_at": None, "last_push_seconds": None,
                       "last_snapshot": None, "last_error": None, "pushes": 0, "fenced": False,
                       "handed_over": False}

    # Ownership (see "Ownership" above). An instance that finds a better owner than itself stops pushing
    # and taking writes instead of overwriting.
    def _rank(self) -> tuple[int, int, str]:
        return self.epoch, int(self.since * 1000), self.instance

    def _owns(self, lease: dict | None) -> bool:
        return lease is not None and lease.get("instance") == self.instance and lease.get("epoch") == self.epoch

    def _replaced_by(self, lease: dict | None) -> tuple | None:
        # Before every publish: the claims, not just the lease, which a late upload can overwrite.
        owner = _authority(_claims(self.store), lease)
        return owner if owner is not None and owner > self._rank() else None

    def _lease(self, released: bool = False, handover_to: str | None = None,
               snapshot: str | None = None) -> tuple[bytes, str]:
        return json.dumps({"protocol": PROTOCOL, "instance": self.instance, "since": self.since,
                           "epoch": self.epoch, "serving": self._serving, "heartbeat": time.time(),
                           "released": released, "handover_to": handover_to,
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

    def _put_lease(self, released: bool = False, handover_to: str | None = None, serving: bool = False) -> None:
        events, n = self._events()
        marker = [(b"{}", _claim_path(self._rank(), "serving"))] if serving else []
        self._upload([self._lease(released, handover_to)] + marker + events)
        self._last_heartbeat = time.time()
        self._events_sent = n

    def _start_beats(self) -> None:
        if self._beats is None:
            self._last_beat = time.monotonic()
            self._beats = threading.Thread(target=self._beat, name="msgboard-heartbeat", daemon=True)
            self._beats.start()

    def _beat(self) -> None:
        # Upload a small heartbeat now and then while an upload is slow (a rate-limited one takes minutes)
        # and while claiming or restoring the replica: instances waiting on this one count it dead only
        # when nothing from it lands for a while.
        while not self._beats_stop.wait(self.heartbeat / 4):
            now, started = time.monotonic(), self._uploading
            busy = started is not None and now - max(started, self._last_beat) >= self.heartbeat
            starting = self._acquiring or (self.epoch is not None and not self._serving and not self.handed_over)
            if self.fenced or not (busy or (starting and now - self._last_beat >= self.heartbeat)):
                continue
            self._last_beat = now
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
            self.on_fenced()

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

    def claim(self, expect: tuple | None = None, stale_after: float | None = None) -> bool | None:
        """Take the replica: win the next epoch, which this instance's snapshots then carry.

        Returns True once this instance owns it. Returns False if another instance owns it or is about
        to (it was released to another, a newer one claims it, or one won it meanwhile): this instance
        must not serve, and asks again. With `expect`, what request_handover judged stale, returns
        None if that changed meanwhile.
        """
        def look():
            lease, = self._watch.read(LEASE)
            claims = _claims(self.store)
            owner = _authority(claims, lease)
            if expect is not None:
                if (lease, owner) != expect[:2]:
                    return None
            elif lease is not None and owner is not None:
                if lease.get("released") and owner == _lease_rank(lease):
                    if lease.get("handover_to") not in (None, self.instance):
                        log.warning("the replica was released to instance %s", lease["handover_to"])
                        return False
                elif owner[1] > int(self.since * 1000):
                    log.warning("newer instance %s has the replica", owner[2])
                    return False
            return claims, owner, lease

        found = self._retrying("reading the replica's claims", look)
        if not isinstance(found, tuple):
            return found
        claims, owner, lease = found
        return self._acquire(owner[0] if owner else 0, claims, lease, stale_after or self.stale_after,
                             silent=expect[2] if expect is not None else ())

    def _acquire(self, parent: int, claims: dict, lease: dict | None, stale_after: float, silent=()) -> bool:
        """Win the epoch after `parent` against every other instance claiming it.

        A bucket has no compare-and-swap, but each instance reads only after its own upload has
        landed, so of two claims the later one to land sees the other. 1. Upload a claim. 2. Wait out
        the other claims after `parent`: give way to one that won or to a newer claimant, and wait for
        older ones to withdraw (or, if nothing from one lands for stale_after s, to count as dead).
        3. Upload a won marker, then list again: if another won marker shows, withdraw. Of two won
        markers, the later one to land sees the earlier, so at most one instance gets past 3.
        Also withdraw if an instance this one outranks started serving after `claims` was read: one
        that this one judged dead (silent) but was only slow. That instance uploads its .serving marker
        before it checks the claims (start()), so the same argument applies: of its marker and this
        won marker, the later to land is seen by the other's check, so they never both go on.
        `silent`: claims already seen silent for stale_after s (they count as dead unless they change).
        """
        epoch = max([rank[0] for rank in claims] + [(lease or {}).get("epoch") or 0, parent]) + 1
        mine = (epoch, int(self.since * 1000), self.instance)
        self._acquiring = True
        self._start_beats()
        try:
            self._retrying("uploading a claim", lambda: self._upload([(json.dumps(
                {"instance": self.instance, "since": self.since, "epoch": epoch, "parent": parent,
                 "at": time.time()}).encode(), _claim_path(mine))]))
            if self._retrying("claiming the replica", lambda: self._contend(mine, parent, stale_after, silent)):
                self._retrying("uploading the won marker", lambda: self._upload([(b"{}", _claim_path(mine, "won"))]))
                now = self._retrying("checking the replica's claims", lambda: _claims(self.store))
                rival = next((rank for rank, kinds in _contenders(now, parent).items()
                              if rank != mine and "won" in kinds), None)
                started = next((rank for rank, kinds in now.items() if rank != mine and "serving" in kinds
                                and "serving" not in claims.get(rank, ())), None)
                if rival is None and started is None:
                    self.epoch = self.status["epoch"] = epoch
                    log.info("took the replica: epoch %s", epoch)
                    return True
                if rival is not None:
                    log.warning("instance %s won the replica too; this one withdraws", rival[2])
                else:
                    log.warning("instance %s started serving meanwhile (it was slow, not dead); this one "
                                "withdraws", started[2])
            self._retrying("withdrawing a claim", lambda: self._upload([(b"{}", _claim_path(mine, "withdrawn"))]))
            return False
        except BaseException:
            # Stopping: stop(), or SIGTERM in the Space app (SystemExit), say when the platform replaces this
            # container in a double restart. Withdraw if it can, so the owner hands the replica on and the
            # others need not wait for this claim to go stale. Even if the claim upload was cut short: it may
            # still land, and a marker without a claim is harmless.
            with contextlib.suppress(Exception):
                self.store.put([(b"{}", _claim_path(mine, "withdrawn"))])
            raise
        finally:
            self._acquiring = False

    def _contend(self, mine: tuple, parent: int, stale_after: float, silent) -> bool:
        """Step 2 of _acquire. True: no other claim stands; False: give way."""
        seen: dict = {}  # rank -> (its markers and heartbeat, when they last changed)
        while True:
            rivals = {rank: kinds for rank, kinds in _contenders(_claims(self.store), parent).items() if rank != mine}
            won = next((rank for rank, kinds in rivals.items() if "won" in kinds), None)
            if won is not None:
                log.info("instance %s won the replica; this one gives way", won[2])
                return False
            beats = self.store.versions([f"{BEATS}{rank[2]}.json" for rank in rivals]) if rivals else {}
            now, alive = time.monotonic(), []
            for rank, kinds in rivals.items():
                state = (tuple(sorted(kinds)), beats.get(f"{BEATS}{rank[2]}.json"))
                if rank not in seen:
                    seen[rank] = (state, now - stale_after - 1 if rank in silent else now)
                elif seen[rank][0] != state:
                    seen[rank] = (state, now)
                if now - seen[rank][1] <= stale_after:
                    alive.append(rank)
            newer = next((rank for rank in alive if rank[1:] > mine[1:]), None)
            if newer is not None:
                log.info("newer instance %s claims the replica too; this one gives way", newer[2])
                return False
            if not alive:
                return True
            if self._stop.wait(self.poll):
                raise RuntimeError("stopped while claiming the replica")

    def take_over(self, stale_after: float = 300.0, poll: float = 2.0) -> str:
        """Before serving: have the replica handed over (request_handover), then claim it.

        Returns how: "free", "released", "stale", "legacy" (claim it in start(), once the old owner has
        stopped: see wait_for_release) or "stopped". While another instance owns or takes the replica,
        it keeps asking.
        """
        while True:
            how, stale = _await_owner(self.store, self.instance, self.since, stale_after, poll, 10.0, self._stop)
            if how in ("legacy", "stopped"):
                return how
            if self.claim(expect=stale, stale_after=stale_after):
                return how
            log.info("another instance has or takes the replica; asking again")
            if self._stop.wait(poll):
                return "stopped"

    def start(self, generation_fn) -> None:
        self.generation_fn = generation_fn
        self.record("started")
        owner = self.epoch is not None or self.claim()

        def mark_serving():
            # Tell the previous owner, which watches for it, that this instance serves now (the lease and a
            # .serving marker, in one upload), then check that nobody took the replica meanwhile (while this
            # one restored it, say). Marker first, check second: see _acquire.
            if self._replaced_by(self._watch.read(LEASE)[0]) is not None:
                return False
            self._serving = True
            self._put_lease(serving=True)
            return self._replaced_by(self._watch.read(LEASE)[0]) is None

        if owner:
            owner = self._retrying("checking the replica lease", mark_serving)
        if not owner:
            self._serving = False
            self.fenced = self.status["fenced"] = True
            log.error("this instance does not own the replica: it does not push or take writes")
            self.quiesce()
            self.on_fenced()
            return
        self._start_beats()
        self._thread = threading.Thread(target=self._run, name="msgboard-replicator", daemon=True)
        self._thread.start()

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
                        and (request.get("instance"), request.get("at")) not in self._refused):
                    if self._hand_over(request):
                        return
                    continue
                if time.monotonic() >= next_push:
                    next_push = time.monotonic() + self.interval
                    if self.push_if_changed():
                        continue
                    if self.fenced:
                        return
                if time.time() - self._last_heartbeat > self.heartbeat:
                    replaced = self._replaced_by(lease)
                    if replaced is not None:
                        self._fence(replaced)
                        return
                    self._put_lease()
            except Exception as exc:  # keep serving; report through metrics
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("replication push failed: %s", exc)

    def _hand_over(self, request: dict) -> bool:
        """A newer instance asked for the replica: stop taking writes, push a final snapshot and release
        the lease to it. Unlike after SIGTERM, there is no deadline while this instance's uploads (or its
        heartbeats, see _beat) keep landing. Before each attempt it checks that nobody took the replica
        meanwhile (the instance asking counts this one dead after stale_after s without either): then it
        must not publish. Returns False if nobody took the replica over and this instance took it back."""
        log.info("instance %s asked for the replica; handing over", request["instance"])
        self.record(f"handover-requested {request['instance']}")
        self._uploading = time.monotonic()  # writes in progress can take a while too: heartbeats meanwhile
        try:
            self.quiesce()
        finally:
            self._uploading = None
        while True:
            try:
                with self._lock:
                    lease, = self._watch.read(LEASE)
                    replaced = self._replaced_by(lease)
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
        seen = None
        while not self._stop.wait(self.poll):
            try:
                lease, newer = self._watch.read(LEASE, HANDOVER)
                claims = _claims(self.store)
                owner = _authority(claims, lease)
                taken = owner is not None and owner > self._rank()
                if taken and ("serving" in claims.get(owner, ()) or (
                        lease is not None and _lease_rank(lease) == owner and lease.get("serving"))):
                    log.info("instance %s took the replica over", owner[2])
                    return True
                # Until a new owner serves, watch for signs of life from instances that may take it over.
                after = _contenders(claims, self.epoch)
                beats = [f"{BEATS}{i}.json" for i in sorted({request["instance"]} | {rank[2] for rank in after})]
                state = (tuple(sorted((rank, tuple(sorted(kinds))) for rank, kinds in after.items())),
                         tuple(sorted(self.store.versions(beats).items())), self._watch.versions.get(LEASE))
                if state != seen:
                    seen, changed_at = state, time.monotonic()
                if not taken:
                    if not self._owns(lease) or not lease.get("released") or lease.get("handover_to") != request["instance"]:
                        self._put_lease(released=True, handover_to=request["instance"])  # a late write hid it
                    elif (newer is not None and newer.get("since", 0) > request.get("since", 0)
                            and (newer.get("instance"), newer.get("at")) not in self._refused
                            and time.monotonic() - released_at > self.regrant_after
                            and request["instance"] not in {rank[2] for rank in after}):  # it has not even claimed
                        log.info("instance %s asked too, and %s has not claimed the replica; releasing it to %s "
                                 "instead", newer["instance"], request["instance"], newer["instance"])
                        self._refused.add((request["instance"], request.get("at")))
                        request = newer
                        self._put_lease(released=True, handover_to=request["instance"])
                        released_at = changed_at = time.monotonic()
                        continue
                if time.monotonic() - changed_at > self.reclaim_after:
                    if self._reclaim(request, claims, lease, after):
                        return self.fenced  # False: it serves again
                    seen = None  # another instance is taking it: keep watching
            except Exception as exc:
                log.warning("waiting for the hand-over: %s", exc)
        return True

    def _reclaim(self, request: dict, claims: dict, lease: dict | None, silent) -> bool:
        # Nothing from any instance that could take the replica over has landed for reclaim_after s.
        # Returns True if this instance took the replica back (or, if one outranked it meanwhile, is fenced).
        log.warning("nobody took the replica over and served in %.0f s; taking it back", self.reclaim_after)
        owner = _authority(claims, lease)
        if not self._acquire(owner[0] if owner else self.epoch, claims, lease, self.reclaim_after, silent=silent):
            return False
        self._refused.add((request["instance"], request.get("at")))
        # serving again: the lease with a .serving marker, then the claims once more, as start() does
        self._retrying("writing the replica lease", lambda: self._put_lease(serving=True))
        replaced = self._retrying("checking the replica's claims", lambda: self._replaced_by(self._watch.read(LEASE)[0]))
        if replaced is not None:
            self._fence(replaced)
            return True
        with contextlib.suppress(Exception):  # instances still waiting ask again, this time this one
            self.store.delete([HANDOVER])
        self.handed_over = self.status["handed_over"] = False
        self.resume()
        return True

    def stop(self, final_push: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        if final_push and self.epoch is not None and not self.handed_over and not self.fenced:
            self.record("final-push")
            try:
                with self._lock:
                    lease, = self._watch.read(LEASE)
                    replaced = self._replaced_by(lease)
                    if replaced is not None:
                        self._fence(replaced)
                    elif self._serving and self.generation_fn() != self._pushed_generation:
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
            replaced = self._replaced_by(lease)
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
            old += [p for p in self.store.list(CLAIMS) if p[len(CLAIMS):len(CLAIMS) + 8].isdigit()
                    and int(p[len(CLAIMS):len(CLAIMS) + 8]) < self.epoch - KEEP_CLAIMS]
            old += [p for p in self.store.list(BEATS) if p != f"{BEATS}{self.instance}.json"]
        if old:
            self.store.delete(old)
