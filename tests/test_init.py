from __future__ import annotations

import os
import sqlite3
import stat
from argparse import Namespace

import pytest

from msgboard import db as dbmod
from msgboard.cli import cmd_init, cmd_rotate_operator, main
from msgboard.client import Client
from msgboard.local import ThreadedServer
from msgboard.server import AlreadyRunning, Board, NotInitialized


def test_init_creates_restricted_token_file_and_never_prints_it(tmp_path, capsys):
    data = tmp_path / "board"
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    out = capsys.readouterr()
    token = (data / "operator.token").read_text().strip()
    assert token.startswith("fbt_")
    assert token not in out.out + out.err
    assert str(data / "operator.token") in out.out
    assert stat.S_IMODE(os.stat(data / "operator.token").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700
    conn = dbmod.connect(data / "board.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    raw = conn.execute("SELECT secret_hash FROM credentials").fetchone()[0]
    assert token.split(".")[1] not in str(raw)


def test_repeated_init_preserves_state_and_credentials(tmp_path, capsys):
    data = tmp_path / "board"
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    token = (data / "operator.token").read_text()
    with ThreadedServer(data) as s, Client(s.url, token.strip()) as c:
        ch = c.create_channel("keep")
        c.post(ch["id"], "still here")
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    assert "already initialized" in capsys.readouterr().out
    assert (data / "operator.token").read_text() == token

    # Losing the token file does not mint a new operator credential silently.
    (data / "operator.token").unlink()
    assert cmd_init(Namespace(data_dir=str(data))) == 0
    assert not (data / "operator.token").exists()
    with ThreadedServer(data) as s, Client(s.url, token.strip()) as c:
        assert [m["body"] for m in c.read(ch["id"])["messages"]] == ["still here"]


def test_init_refuses_stray_token_file(tmp_path):
    data = tmp_path / "board"
    data.mkdir()
    (data / "operator.token").write_text("something important\n")
    assert cmd_init(Namespace(data_dir=str(data))) == 1
    assert (data / "operator.token").read_text() == "something important\n"


def test_rotate_operator_token(tmp_path):
    data = tmp_path / "board"
    cmd_init(Namespace(data_dir=str(data)))
    old = (data / "operator.token").read_text().strip()
    assert cmd_rotate_operator(Namespace(data_dir=str(data))) == 0
    new = (data / "operator.token").read_text().strip()
    assert new != old
    with ThreadedServer(data) as s:
        with Client(s.url, new) as c:
            assert c.me()["role"] == "admin"
        with Client(s.url, old) as c, pytest.raises(Exception):
            c.me()


def test_serve_requires_init_and_refuses_second_instance(tmp_path):
    with pytest.raises(NotInitialized):
        Board(tmp_path / "empty")
    data = tmp_path / "board"
    cmd_init(Namespace(data_dir=str(data)))
    first = Board(data)
    try:
        with pytest.raises(AlreadyRunning):
            Board(data)
    finally:
        first.close()
    Board(data).close()  # lock released on close


def test_unknown_schema_version_is_refused(tmp_path):
    data = tmp_path / "board"
    cmd_init(Namespace(data_dir=str(data)))
    conn = sqlite3.connect(data / "board.db")
    conn.execute("PRAGMA user_version=99")
    conn.close()
    assert cmd_init(Namespace(data_dir=str(data))) == 1
    with pytest.raises(dbmod.SchemaError):
        Board(data)
    assert main(["serve", "--data-dir", str(data)]) == 1


def test_backup_snapshot(tmp_path):
    data = tmp_path / "board"
    cmd_init(Namespace(data_dir=str(data)))
    out = tmp_path / "snap.db"
    assert main(["backup", "--data-dir", str(data), "--out", str(out)]) == 0
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1
