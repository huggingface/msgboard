"""python -m msgboard {init,serve,backup,rotate-operator-token,demo}"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import time
from pathlib import Path

from . import db as dbmod
from . import store
from .config import DB_NAME, DEFAULT_PORT, OPERATOR_TOKEN_FILE

NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse", "fuseblk", "9p", "sshfs"}


def _fs_type(path: Path) -> str | None:
    """Best-effort filesystem type of `path` from /proc/mounts (Linux only)."""
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return None
    best, fstype = "", None
    target = str(path.resolve())
    for line in mounts:
        parts = line.split()
        if len(parts) >= 3 and (target == parts[1] or target.startswith(parts[1].rstrip("/") + "/")):
            if len(parts[1]) > len(best):
                best, fstype = parts[1], parts[2]
    return fstype


def _warn_fs(data_dir: Path) -> None:
    fstype = _fs_type(data_dir)
    if fstype and (fstype in NETWORK_FS or fstype.startswith("fuse")):
        print(f"warning: {data_dir} is on a {fstype} filesystem. SQLite WAL needs a local disk; "
              "do not keep a live board on network or bucket mounts.", file=sys.stderr)


def _write_secret_file(path: Path, secret: str) -> None:
    """Create a 0600 file that must not already exist, and fsync it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (secret + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_init(args) -> int:
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    _warn_fs(data_dir)
    db_path = data_dir / DB_NAME
    token_path = data_dir / OPERATOR_TOKEN_FILE
    existed = db_path.exists()
    conn = dbmod.connect(db_path)
    try:
        before = dbmod.schema_version(conn)
        try:
            version = dbmod.migrate(conn, db_path)
        except dbmod.SchemaError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if store.admin_exists(conn):
            note = "migrated" if before != version else "unchanged"
            print(f"Board already initialized in {data_dir} (schema v{version}, {note}).")
            tmp_path = data_dir / (OPERATOR_TOKEN_FILE + ".tmp")
            if not token_path.exists() and tmp_path.exists():
                # An earlier init committed but stopped before moving the file into place.
                try:
                    store.authenticate(conn, tmp_path.read_text().strip(), time.time())
                    os.link(tmp_path, token_path)
                    tmp_path.unlink()
                except Exception:
                    pass
            if token_path.exists():
                print(f"Operator token file: {token_path}")
            else:
                print("Operator token file is missing; existing credentials were left untouched. "
                      "Run `python -m msgboard rotate-operator-token --data-dir ...` to issue a new one.")
            return 0
        if token_path.exists():
            print(f"error: {token_path} exists but the database has no operator; refusing to overwrite it. "
                  "Move the file away if it is stale.", file=sys.stderr)
            return 1
        # Write the secret to a temp file first, then commit, then move it into place.
        tmp_path = data_dir / (OPERATOR_TOKEN_FILE + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _, token = store.create_operator(conn, time.time())
            _write_secret_file(tmp_path, token)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        os.link(tmp_path, token_path)  # fails rather than overwrite
        tmp_path.unlink()
    finally:
        conn.close()
    print(f"{'Initialized schema in existing' if existed else 'Created'} board in {data_dir} (schema v{version}).")
    print(f"Operator token saved to {token_path} (mode 0600). Keep it secret; give launchers a provisioner token instead.")
    return 0


def cmd_rotate_operator(args) -> int:
    data_dir = Path(args.data_dir).expanduser()
    db_path = data_dir / DB_NAME
    token_path = data_dir / OPERATOR_TOKEN_FILE
    if not db_path.exists():
        print(f"error: no board in {data_dir}", file=sys.stderr)
        return 1
    conn = dbmod.connect(db_path)
    try:
        if dbmod.schema_version(conn) != dbmod.SCHEMA_VERSION:
            print("error: run init first to migrate the schema", file=sys.stderr)
            return 1
        new_path = data_dir / (OPERATOR_TOKEN_FILE + ".new")
        if new_path.exists():
            new_path.unlink()
        conn.execute("BEGIN IMMEDIATE")
        try:
            token = store.rotate_operator(conn, time.time())
            _write_secret_file(new_path, token)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            if new_path.exists():
                new_path.unlink()
            raise
        os.replace(new_path, token_path)
    finally:
        conn.close()
    print(f"Operator token rotated; the old one is revoked. New token saved to {token_path}.")
    return 0


def cmd_backup(args) -> int:
    db_path = Path(args.data_dir).expanduser() / DB_NAME
    if not db_path.exists():
        print(f"error: no board at {db_path}", file=sys.stderr)
        return 1
    conn = dbmod.connect(db_path)
    try:
        dbmod.backup(conn, args.out)
    finally:
        conn.close()
    print(f"Backup written to {args.out}. Attachments live in {db_path.parent / 'blobs'}; copy them too.")
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_serve(args) -> int:
    import uvicorn

    from .server import AlreadyRunning, Board, NotInitialized, create_app

    data_dir = Path(args.data_dir).expanduser()
    _warn_fs(data_dir)
    try:
        board = Board(data_dir)
    except (NotInitialized, AlreadyRunning, dbmod.SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not _is_loopback(args.host):
        print(f"warning: listening on {args.host}. Tokens travel in plain HTTP; "
              "put HTTPS in front of this outside isolated local development.", file=sys.stderr)

    class BoardServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            board.begin_shutdown()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(
        create_app(board), host=args.host, port=args.port, log_level=args.log_level,
        timeout_graceful_shutdown=5, server_header=False,
    )
    try:
        BoardServer(config).run()
    finally:
        board.close()
        print("Board stopped cleanly.", file=sys.stderr)
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo

    return run_demo(args.data_dir, args.port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m msgboard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create or migrate a board; never overwrites existing credentials")
    p.add_argument("--data-dir", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the HTTP server in the foreground (Ctrl-C to stop)")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("backup", help="write a consistent SQLite snapshot")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("rotate-operator-token", help="replace the operator token (local file access required)")
    p.add_argument("--data-dir", required=True)
    p.set_defaults(func=cmd_rotate_operator)

    p = sub.add_parser("demo", help="start a throwaway board and run two agents against it")
    p.add_argument("--data-dir", default=None, help="defaults to a new temporary directory")
    p.add_argument("--port", type=int, default=0, help="0 picks a free port")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)
