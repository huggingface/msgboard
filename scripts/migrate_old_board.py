"""One-off: move a board made by the old hfspace/deploy.py onto msgboard.space.deploy(). Never merged.

Old boards keep their replica under a prefix (MSGBOARD_REPLICA=hf://buckets/<bucket>/demo) and have no
`msgboard` tag, so deploy() refuses them; deploy() wants the replica at the bucket root. This script, for one
board at a time:

  0. checks (read-only): the Space's replica is <bucket>/<prefix>, the bucket root is empty, and the latest
     snapshot restores with every file it refers to
  1. pauses the Space, so the board writes a final snapshot and releases its lease
  2. copies every object under <prefix>/ (not the lease) to the bucket root, checking sizes; <prefix>/ stays
  3. restores the root copy into a temporary directory and compares it with the paused board's snapshot
  4. tags the Space `msgboard`, points MSGBOARD_REPLICA at the root, and runs deploy() with the board's
     own operator token, so logins do not change
  5. waits for the board and compares its counts again

Without --go it stops after step 0. If step 1-3 fails the Space is restarted unchanged. After step 4, rolling
back is: set MSGBOARD_REPLICA back to hf://buckets/<bucket>/<prefix> and restart the Space.

    python scripts/migrate_old_board.py --space lvwerra/msgboard --bucket lvwerra/msgboard-data \\
        --secrets ~/.config/msgboard-lvwerra --title "personal board" [--go]
"""

from __future__ import annotations

import argparse
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
        rel = e.path[len(prefix) + 1:] if prefix else e.path
        out[rel] = getattr(e, "size", None)
    return out


def inspect(store, files: dict[str, int]) -> dict:
    """Restore the store's latest snapshot into a temp dir; refuse anything missing; return counts."""
    with tempfile.TemporaryDirectory() as d:
        latest = replicate.restore(store, Path(d))
        if latest is None:
            raise SystemExit("no LATEST.json: nothing to migrate from here")
        conn = sqlite3.connect(Path(d) / "board.db")
        try:
            one = lambda q: conn.execute(q).fetchone()[0]
            counts = {"snapshot": latest["snapshot"], "generation": latest.get("generation"),
                      "messages": one("SELECT COUNT(*) FROM messages"), "channels": one("SELECT COUNT(*) FROM channels"),
                      "principals": one("SELECT COUNT(*) FROM principals"),
                      "attachments": one("SELECT COUNT(*) FROM attachments WHERE state = 'ready'"),
                      "storage_files": one("SELECT COUNT(*) FROM storage_files"),
                      "max_message_id": one("SELECT COALESCE(MAX(id), 0) FROM messages"),
                      "schema": one("PRAGMA user_version")}
            blobs = [f"blobs/{r[0]}" for r in conn.execute("SELECT storage_key FROM attachments WHERE state = 'ready'")]
            stored = [f"storage/{o}/{p}" for o, p in conn.execute("SELECT owner_id, path FROM storage_files")]
        finally:
            conn.close()
    missing = [k for k in blobs + stored if k not in files]
    if missing:   # restore() only warns about these; a migration must not lose them
        raise SystemExit(f"{len(missing)} referenced files are missing, e.g. {missing[:5]}")
    return counts


def lease(store) -> dict | None:
    return replicate._read_json(store, replicate.LEASE)


def wait_stage(api, space: str, want: set[str], timeout: float = 900) -> str:
    end = time.time() + timeout
    while time.time() < end:
        stage = api.get_space_runtime(space).stage
        if stage in want:
            return stage
        time.sleep(20)
    raise SystemExit(f"{space} did not reach {want} in {timeout:.0f}s")


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
    operator = (Path(a.secrets).expanduser() / "operator.token").read_text().strip()

    # 0. read-only checks
    info = api.space_info(a.space)
    replica = api.get_space_variables(a.space).get("MSGBOARD_REPLICA")
    replica = replica.value if replica else None
    if TAG in (info.tags or []):
        raise SystemExit(f"{a.space} already has the {TAG!r} tag; nothing to migrate")
    if replica != f"{url}/{a.prefix}":
        raise SystemExit(f"{a.space} has MSGBOARD_REPLICA={replica!r}, expected {url}/{a.prefix}")
    root = {k: v for k, v in objects(a.bucket, "").items() if not k.startswith(a.prefix + "/")}
    if root:
        raise SystemExit(f"the bucket root is not empty ({len(root)} objects, e.g. {sorted(root)[:3]}); refusing")
    old = replicate.store_from_url(f"{url}/{a.prefix}")
    files = objects(a.bucket, a.prefix)
    before = inspect(old, files)
    say(f"checks pass: {len(files)} objects under {a.prefix}/, latest snapshot {json.dumps(before)}")
    if not a.go:
        say("dry run: nothing changed (add --go to migrate)")
        return

    # 1. stop writes: pausing sends SIGTERM, the board pushes a final snapshot and releases its lease
    say(f"pausing {a.space}")
    api.pause_space(a.space)
    wait_stage(api, a.space, {"PAUSED"})
    for _ in range(30):
        l = lease(old)
        if l is None or l.get("released"):
            break
        time.sleep(10)
    else:
        api.restart_space(a.space)
        raise SystemExit(f"the lease was not released after pausing ({l}); restarted the Space unchanged")
    try:
        files = objects(a.bucket, a.prefix)
        final = inspect(old, files)
        say(f"paused; final snapshot {json.dumps(final)}")

        # 2. copy everything but the lease, then compare sizes
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
        bad = [k for k in todo if copied.get(k) != files[k]]
        if bad:
            raise RuntimeError(f"{len(bad)} copies differ in size, e.g. {bad[:5]}")

        # 3. the root copy must restore to the same board
        after = inspect(replicate.store_from_url(url), copied)
        if after != final:
            raise RuntimeError(f"the root copy differs: {after} vs {final}")
        say("root copy restores to the same board")
    except BaseException:
        say("something failed before the switch: restarting the Space unchanged (the root copy can be deleted)")
        api.restart_space(a.space)
        raise

    # 4. switch: tag, point at the root, deploy with the board's own operator token
    say("switching the Space to the bucket root")
    card_tags = list(getattr(info.card_data, "tags", None) or [])   # the card's own tags, not the Hub's automatic ones
    metadata_update(a.space, {"tags": card_tags + [TAG]}, repo_type="space", overwrite=True)
    api.add_space_variable(a.space, "MSGBOARD_REPLICA", url)
    result = deploy(a.space, operator_token=operator, title=a.title)
    say(f"deployed {result.commit[:8]}; restarting")
    api.restart_space(a.space)

    # 5. the live board must show what the snapshot holds (checked at most once a minute)
    wait_stage(api, a.space, {"RUNNING"})
    host = f"https://{a.space.replace('/', '-').lower()}.hf.space"
    for _ in range(20):
        try:
            if json.load(urllib.request.urlopen(f"{host}/v1/health", timeout=10)).get("status") == "ok":
                break
        except Exception:
            pass
        time.sleep(60)
    req = urllib.request.Request(f"{host}/v1/channels", headers={"Authorization": f"Bearer {operator}"})
    live = len(json.load(urllib.request.urlopen(req, timeout=20))["channels"])
    say(f"live: {live} channels visible to the operator (snapshot has {final['channels']})")
    say(f"done. Rollback if needed: set MSGBOARD_REPLICA={url}/{a.prefix} and restart {a.space}")


if __name__ == "__main__":
    sys.exit(main())
