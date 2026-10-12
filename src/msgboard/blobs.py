"""Local attachment storage: synced temp file, atomic rename, directory sync."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.tmp = self.root / "tmp"
        self.objects = self.root / "objects"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)

    def path_for(self, storage_key: str) -> Path:
        # storage_key is always server generated ("ab/att_ab..."); never a client filename.
        return self.objects / storage_key

    def open_temp(self):
        path = self.tmp / f"upload-{secrets.token_hex(8)}"
        return path, open(path, "xb")

    @staticmethod
    def finish_temp(fh) -> None:
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()

    def commit(self, tmp_path: Path, storage_key: str) -> None:
        """Move a synced temp file into place. Only called for not-yet-ready uploads."""
        dest = self.path_for(storage_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, dest)
        _fsync_dir(dest.parent)

    @staticmethod
    def discard(tmp_path: Path) -> None:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def new_hasher():
    return hashlib.sha256()
