from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import resource
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from msgboard import auth, db, store
from msgboard.config import LIMITS
from msgboard.errors import forbidden
from msgboard.server import Board, create_app


def bootstrap(data_dir: Path, token: str) -> None:
    parsed = auth.parse_token(token)
    if parsed is None:
        raise RuntimeError("FLOCK_SPACE_OPERATOR_TOKEN must be a valid board token")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = db.connect(data_dir / "board.db")
    try:
        db.migrate(connection, data_dir / "board.db")
        connection.execute("BEGIN IMMEDIATE")
        if not store.admin_exists(connection):
            store.create_operator(connection, time.time())
        principal_id = connection.execute(
            "SELECT id FROM principals WHERE issuer='local' AND external_id='operator'"
        ).fetchone()[0]
        credential_id, secret = parsed
        connection.execute(
            "UPDATE credentials SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
            (time.time(), principal_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO credentials "
            "(id, principal_id, secret_hash, issued_by, created_at, expires_at, revoked_at) "
            "VALUES (?, ?, ?, NULL, ?, NULL, NULL)",
            (credential_id, principal_id, hashlib.sha256(secret).digest(), time.time()),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    secret_path = data_dir / "operator.token"
    descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as secret_file:
        secret_file.write(token + "\n")


def system_snapshot() -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:", "Threads:")):
            key, value = line.split(":", 1)
            status[key] = value.strip()
    cgroup = {}
    for filename in ("cpu.max", "cpu.stat", "memory.current", "memory.max", "memory.events"):
        candidate = Path("/sys/fs/cgroup") / filename
        if candidate.exists():
            cgroup[filename] = candidate.read_text().strip()
    return {
        "time": time.time(), "pid": os.getpid(), "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count(),
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "max_rss_kib": usage.ru_maxrss, "process": status, "cgroup": cgroup,
        "dependencies": {name: importlib.metadata.version(name) for name in ("fastapi", "uvicorn", "httpx")},
    }


class BoardCredentialHeader:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            board_token = next((value for key, value in headers if key == b"x-flock-token"), None)
            if board_token is not None:
                scope = dict(scope)
                scope["headers"] = [(key, value) for key, value in headers
                                    if key not in (b"authorization", b"x-flock-token")]
                scope["headers"].append((b"authorization", b"Bearer " + board_token))
        await self.app(scope, receive, send)


def build_app(data_dir: Path):
    board = Board(data_dir)
    app = create_app(board)
    app.add_middleware(BoardCredentialHeader)
    original_lifespan = app.router.lifespan_context
    jobs = {}
    active_job = None

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            yield
        board.close()

    app.router.lifespan_context = lifespan

    async def operator(request: Request):
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ")
        principal = await board.db.read(store.authenticate, token, time.time())
        if not principal.is_admin:
            raise forbidden("operator only")
        return principal

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """<!doctype html><html><meta name="viewport" content="width=device-width">
        <title>Flock board prototype</title><body style="font:17px/1.6 system-ui;max-width:760px;margin:60px auto;padding:20px">
        <h1>Flock board prototype</h1><p>One Python server. SQLite. Authenticated channel history.</p>
        <p><strong>Test deployment: storage is ephemeral.</strong> Do not store production data here.</p>
        <p>The API is under <code>/v1</code>. Private Space clients send their Hugging Face credential
        in <code>Authorization</code> and their board credential in <code>X-Flock-Token</code>.</p>
        <p><a href="/v1/health">Health</a></p></body></html>"""

    @app.get("/_bench/system")
    async def system(request: Request):
        await operator(request)
        snapshot = system_snapshot()
        snapshot["limits"] = vars(LIMITS)
        snapshot["bench_enabled"] = os.environ.get("FLOCK_BENCH_ENABLE") == "1"
        return snapshot

    async def run_job(job_id: str, configuration: dict):
        nonlocal active_job
        result_path = data_dir / f"bench-{job_id}.json"
        configuration_path = data_dir / f"bench-{job_id}-config.json"
        configuration_path.write_text(json.dumps(configuration))
        try:
            script = "aio_loadtest.py" if configuration.get("driver") == "aiohttp" else "loadtest.py"
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name(script)), "--url",
                "http://127.0.0.1:" + os.environ.get("PORT", "7860"),
                "--token-file", str(data_dir / "operator.token"),
                "--config", str(configuration_path), "--output", str(result_path),
                "--data-dir", str(data_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            jobs[job_id]["pid"] = process.pid
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1500)
            jobs[job_id].update(status="complete" if process.returncode == 0 else "failed",
                                returncode=process.returncode,
                                stdout=stdout.decode()[-1000:], stderr=stderr.decode()[-2000:])
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            jobs[job_id].update(status="failed", error="test exceeded 25 minute limit")
        except Exception as error:
            jobs[job_id].update(status="failed", error=type(error).__name__)
        finally:
            if result_path.exists():
                jobs[job_id]["result"] = json.loads(result_path.read_text())
            jobs[job_id]["finished_at"] = time.time()
            active_job = None

    @app.post("/_bench/jobs")
    async def start_job(request: Request):
        nonlocal active_job
        await operator(request)
        if os.environ.get("FLOCK_BENCH_ENABLE") != "1":
            raise forbidden("benchmark controls disabled")
        if active_job is not None:
            return JSONResponse({"error": "benchmark already running", "job_id": active_job}, status_code=409)
        configuration = await request.json()
        if configuration.get("suite") not in {"smoke", "matrix", "volume", "fanout", "limits", "rate", "scenario", "attachments", "unicode"}:
            return JSONResponse({"error": "unknown benchmark suite"}, status_code=400)
        if configuration.get("suite") in {"attachments", "unicode"}:
            configuration["driver"] = "aiohttp"
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"id": job_id, "status": "running", "started_at": time.time(), "configuration": configuration}
        active_job = job_id
        asyncio.create_task(run_job(job_id, configuration))
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.get("/_bench/jobs/{job_id}")
    async def job_status(job_id: str, request: Request):
        await operator(request)
        if job_id not in jobs:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        snapshot = dict(jobs[job_id])
        result_path = data_dir / f"bench-{job_id}.json"
        if result_path.exists():
            snapshot["result"] = json.loads(result_path.read_text())
        return snapshot

    return app


if __name__ == "__main__":
    directory = Path(os.environ.get("FLOCK_DATA_DIR", "/home/user/board-data"))
    bootstrap(directory, os.environ["FLOCK_SPACE_OPERATOR_TOKEN"])
    uvicorn.run(build_app(directory), host="0.0.0.0", port=int(os.environ.get("PORT", "7860")),
                access_log=False, log_level="warning")
