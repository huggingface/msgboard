from __future__ import annotations

import stat

from fastapi.testclient import TestClient

from msgboard.auth import new_token
from space.space_app import bootstrap, build_app


def test_space_header_auth_and_benchmark_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOCK_BENCH_ENABLE", raising=False)
    token = new_token()[1]
    bootstrap(tmp_path, token)
    with TestClient(build_app(tmp_path)) as client:
        assert client.get("/_bench/system").status_code == 401
        headers = {"Authorization": "Bearer unrelated-hf-token", "X-Flock-Token": token}
        response = client.get("/_bench/system", headers=headers)
        assert response.status_code == 200
        assert response.json()["bench_enabled"] is False
        assert client.post("/_bench/jobs", headers=headers, json={"suite": "smoke"}).status_code == 403
        invite = client.post("/v1/invites", headers=headers, json={"kind": "agent"}).json()
        issued = client.post("/v1/join", json={"code": invite["code"], "name": "worker"}).json()
        assert client.get("/_bench/system", headers={"X-Flock-Token": issued["token"]}).status_code == 403
        assert client.get("/_bench/system", headers={"X-Flock-Token": "invalid"}).status_code == 401
        assert client.get("/_bench/system", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_space_bootstrap_keeps_history_across_process_restarts(tmp_path):
    token = new_token()[1]
    bootstrap(tmp_path, token)
    assert stat.S_IMODE((tmp_path / "operator.token").stat().st_mode) == 0o600
    headers = {"X-Flock-Token": token}
    with TestClient(build_app(tmp_path)) as client:
        channel = client.post("/v1/channels", headers=headers, json={"name": "keep"}).json()
        posted = client.post(f"/v1/channels/{channel['id']}/messages",
            headers={**headers, "Idempotency-Key": "stable"}, json={"body": "survives"})
        assert posted.status_code == 201
    bootstrap(tmp_path, token)
    with TestClient(build_app(tmp_path)) as client:
        history = client.get(f"/v1/channels/{channel['id']}/messages", headers=headers).json()
        assert [message["body"] for message in history["messages"]] == ["survives"]
        replay = client.post(f"/v1/channels/{channel['id']}/messages",
            headers={**headers, "Idempotency-Key": "stable"}, json={"body": "survives"})
        assert replay.status_code == 200
        assert replay.json()["seq"] == posted.json()["seq"]
