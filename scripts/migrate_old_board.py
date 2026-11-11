"""One-off: move a board made by the old hfspace/deploy.py onto msgboard.space.deploy(). Never merged.

Old boards keep their replica under a prefix (MSGBOARD_REPLICA=hf://buckets/<bucket>/demo) and have no
`msgboard` tag, so deploy() refuses them; deploy() wants the replica at the bucket root. For one board:

  0. checks (read-only): the operator token works on this board, the replica is <bucket>/<prefix>, the
     bucket root is empty, and the latest snapshot restores with every file it refers to, checksums included
  1. pauses the Space and waits for proof of a clean stop: the running instance released its lease naming
     the snapshot that LATEST now points to
  2. copies every object under <prefix>/ (not the lease) to the bucket root; <prefix>/ stays as it was
  3. restores the root copy, checks every referenced file's checksum, and compares it with the final snapshot
  4. tags the Space `msgboard`, points MSGBOARD_REPLICA at the root and runs deploy() with the board's own
     operator token, so logins do not change
  5. checks the live board through the API against what the old board showed before pausing

Without --go it stops after step 0. Any failure before the board runs on the root puts the Space's tag and
MSGBOARD_REPLICA back and restarts it on <prefix>/ unchanged. Once the new board has run, <prefix>/ no longer
has its latest writes: going back means copying the root back, not just flipping MSGBOARD_REPLICA.

    python scripts/migrate_old_board.py --space lvwerra/msgboard --bucket lvwerra/msgboard-data \\
        --secrets ~/.config/msgboard-lvwerra --title "personal board" [--go]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from huggingface_hub import HfApi, list_bucket_tree, metadata_update

from msgboard import replicate
from msgboard.space import deploy

TAG = "msgboard"


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def objects(bucket: str, prefix: str) -> dict[str, int]:
    """path (relative to prefix) -> size, for every file under prefix ('' = the whole bucket)."""
    out = {}
    for e in list_bucket_tree(bucket, prefix=prefix or None, recursive=True):
        if type(e).__name__ == "BucketFolder" or getattr(e, "type", "file") == "directory":
            continue
        out[e.path[len(prefix) + 1:] if prefix else e.path] = getattr(e, "size", None)
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect(store) -> dict:
    """Restore the store's latest snapshot into a temp dir, then fetch every file it refers to and check its
    checksum. Anything missing or different stops the migration. Returns what the snapshot holds."""
    with tempfile.TemporaryDirectory() as d:
        latest = replicate.restore(store, Path(d) / "board")
        if latest is None:
            raise SystemExit("no LATEST.json here")
        conn = sqlite3.connect(Path(d) / "board" / "board.db")
        try:
            one = lambda q: conn.execute(q).fetchone()[0]
            counts = {"snapshot": latest["snapshot"], "instance": latest.get("instance"),
                      "messages": one("SELECT COUNT(*) FROM messages"), "channels": one("SELECT COUNT(*) FROM channels"),
                      "principals": one("SELECT COUNT(*) FROM principals"),
                      "attachments": one("SELECT COUNT(*) FROM attachments WHERE state = 'ready'"),
                      "storage_files": one("SELECT COUNT(*) FROM storage_files"),
                      "max_message_id": one("SELECT COALESCE(MAX(id), 0) FROM messages"),
                      "schema": one("PRAGMA user_version")}
            refs = [(f"blobs/{k}", h) for k, h in conn.execute("SELECT storage_key, sha256 FROM attachments WHERE state = 'ready'")]
            refs += [(f"storage/{o}/{p}", h) for o, p, h in conn.execute("SELECT owner_id, path, sha256 FROM storage_files")]
        finally:
            conn.close()
        bad = []
        for remote, want in refs:   # restore() only warns about missing files and never checks their bytes
            local = Path(d) / "f"
            if not store.get(remote, local) or (want and sha256(local) != want):
                bad.append(remote)
            local.unlink(missing_ok=True)
    if bad:
        raise SystemExit(f"{len(bad)} referenced files are missing or differ, e.g. {bad[:5]}")
    counts["files_checked"] = len(refs)
    return counts


def api_view(host: str, token: str) -> dict:
    """What the board shows its operator: who it is, channel ids, principal ids, the newest message."""
    def get(path):
        req = urllib.request.Request(host + "/v1" + path, headers={"Authorization": f"Bearer {token}"})
        return json.load(urllib.request.urlopen(req, timeout=30))
    me = get("/me")
    chans = get("/channels")["channels"]
    people = get("/principals")["principals"]
    return {"me": me["id"], "role": me.get("role"), "channels": sorted(c["id"] for c in chans),
            "principals": sorted(p["id"] for p in people), "last_seq": {c["id"]: c["last_seq"] for c in chans}}


def wait_stage(api, space: str, want: set[str], timeout: float = 900) -> str:
    end = time.time() + timeout
    while time.time() < end:
        stage = api.get_space_runtime(space).stage
        if stage in want:
            return stage
        time.sleep(20)
    raise RuntimeError(f"{space} did not reach {want} in {timeout:.0f}s")


def wait_healthy(host: str, timeout: float = 1200) -> None:
    end = time.time() + timeout
    while time.time() < end:   # at most once a minute: hf.space rate-limits fast polling
        try:
            if json.load(urllib.request.urlopen(host + "/v1/health", timeout=10)).get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(60)
    raise RuntimeError(f"{host} was not healthy after {timeout:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="demo")
    ap.add_argument("--secrets", required=True, help="directory holding the board's operator.token")
    ap.add_argument("--title", default=None)
    ap.add_argument("--go", action="store_true", help="really migrate (default: checks only)")
    a = ap.parse_args()
    api, url = HfApi(), f"hf://buckets/{a.bucket}"
    host = f"https://{a.space.replace('/', '-').lower()}.hf.space"
    operator = (Path(a.secrets).expanduser() / "operator.token").read_text().strip()

    # 0. read-only checks
    info = api.space_info(a.space)
    card_tags = list(getattr(info.card_data, "tags", None) or [])
    replica = api.get_space_variables(a.space).get("MSGBOARD_REPLICA")
    replica = replica.value if replica else None
    if TAG in (info.tags or []):
        raise SystemExit(f"{a.space} already has the {TAG!r} tag; nothing to migrate")
    if replica != f"{url}/{a.prefix}":
        raise SystemExit(f"{a.space} has MSGBOARD_REPLICA={replica!r}, expected {url}/{a.prefix}")
    try:   # the token must be this board's operator, or deploy() would install a stranger's credential
        before_api = api_view(host, operator)
    except Exception as exc:
        raise SystemExit(f"the operator token does not work on {host}: {exc}")
    if before_api["role"] != "admin":
        raise SystemExit(f"the token in {a.secrets} is not an admin on {host} (role {before_api['role']!r})")
    root = {k: v for k, v in objects(a.bucket, "").items() if not k.startswith(a.prefix + "/")}
    if root:
        raise SystemExit(f"the bucket root is not empty ({len(root)} objects, e.g. {sorted(root)[:3]}); refusing")
    old = replicate.store_from_url(f"{url}/{a.prefix}")
    before = inspect(old)
    lease0 = replicate._read_json(old, replicate.LEASE) or {}
    say(f"checks pass: operator #{before_api['me']}, {len(before_api['channels'])} channels via the API, "
        f"lease held by {lease0.get('instance')}; snapshot {json.dumps(before)}")
    if not a.go:
        say("dry run: nothing changed (add --go to migrate)")
        return

    switched = False

    def put_back(why: str) -> None:
        """Undo the switch (if it started) and run the board on <prefix>/ again, as before."""
        say(f"FAILED ({why}); putting {a.space} back on {url}/{a.prefix}")
        if switched:
            metadata_update(a.space, {"tags": card_tags}, repo_type="space", overwrite=True)
            api.add_space_variable(a.space, "MSGBOARD_REPLICA", f"{url}/{a.prefix}")
        api.restart_space(a.space)
        say("restarted on the old prefix; the root copy (if any) can be deleted")

    try:
        # 1. stop writes; proof of a clean stop: the running instance released its lease naming LATEST
        final_api = api_view(host, operator)   # the last thing people saw, to compare the new board with
        say(f"pausing {a.space}")
        api.pause_space(a.space)
        wait_stage(api, a.space, {"PAUSED"})
        for _ in range(30):
            lease = replicate._read_json(old, replicate.LEASE) or {}
            latest = replicate._read_json(old, replicate.LATEST) or {}
            if (lease.get("released") and lease.get("instance") == lease0.get("instance")
                    and lease.get("snapshot") and lease.get("snapshot") == latest.get("snapshot")
                    and latest.get("instance") == lease0.get("instance")):
                break
            time.sleep(10)
        else:
            raise RuntimeError(f"no proof of a final snapshot: lease {lease}, LATEST {latest}")
        final = inspect(old)
        say(f"paused cleanly; final snapshot {json.dumps(final)}")

        # 2. copy everything but the lease
        files = objects(a.bucket, a.prefix)
        todo = sorted(k for k in files if k != replicate.LEASE)
        with tempfile.TemporaryDirectory() as d:
            for i in range(0, len(todo), 50):
                batch = []
                for k in todo[i:i + 50]:
                    local = Path(d) / f"{i}-{len(batch)}"
                    if not old.get(k, local):
                        raise RuntimeError(f"could not read {a.prefix}/{k}")
                    batch.append((str(local), k))
                api.batch_bucket_files(a.bucket, add=batch)
                for p, _ in batch:
                    Path(p).unlink()
                say(f"copied {min(i + 50, len(todo))}/{len(todo)}")
        copied = {k: v for k, v in objects(a.bucket, "").items() if not k.startswith(a.prefix + "/")}
        if [k for k in todo if copied.get(k) != files[k]]:
            raise RuntimeError("some copies are missing or differ in size")

        # 3. the root copy must be the same board, every referenced file checked by checksum
        after = inspect(replicate.store_from_url(url))
        if {k: v for k, v in after.items() if k != "files_checked"} != {k: v for k, v in final.items() if k != "files_checked"}:
            raise RuntimeError(f"the root copy differs: {after} vs {final}")
        say(f"root copy restores to the same board ({after['files_checked']} files checked)")

        # 4. switch: tag, point at the root, deploy with the board's own operator token
        say("switching the Space to the bucket root")
        switched = True
        metadata_update(a.space, {"tags": card_tags + [TAG]}, repo_type="space", overwrite=True)
        api.add_space_variable(a.space, "MSGBOARD_REPLICA", url)
        result = deploy(a.space, operator_token=operator, title=a.title)
        say(f"deployed {result.commit[:8]}; starting")
        api.restart_space(a.space)
        wait_stage(api, a.space, {"RUNNING"})
        wait_healthy(host)
    except BaseException as exc:
        put_back(repr(exc))
        raise

    # 5. the live board must show what it showed before the pause; from here on the root has new writes
    say(f"the board runs on {url} since {time.strftime('%H:%M:%S')}; {a.prefix}/ is now behind it")
    live = api_view(host, operator)
    problems = ["operator"] if live["me"] != final_api["me"] else []
    problems += [f"{k} missing: {sorted(set(final_api[k]) - set(live[k]))[:5]}" for k in ("channels", "principals")
                 if not set(final_api[k]) <= set(live[k])]   # new ones may have arrived just before the pause
    problems += [f"channel {c} seq {s} < {final_api['last_seq'][c]}" for c, s in live["last_seq"].items()
                 if c in final_api["last_seq"] and s < final_api["last_seq"][c]]
    if problems:
        say(f"LIVE CHECK FAILED: {problems}. Do not put it back by flipping MSGBOARD_REPLICA if people posted "
            f"since the switch; copy the root back to {a.prefix}/ first.")
        sys.exit(1)
    say(f"live check passes: operator #{live['me']}, {len(live['channels'])} channels, {len(live['principals'])} "
        "principals, no channel lost messages. Done.")


if __name__ == "__main__":
    main()
