from __future__ import annotations

import threading
import time

import pytest

from msgboard.client import BoardError


def status(fn, *args, **kwargs) -> int:
    try:
        fn(*args, **kwargs)
    except BoardError as exc:
        return exc.status
    return 200


def test_bad_tokens_are_rejected(env):
    for token in (None, "nonsense", "fbt_0123456789abcdef.AAAA", env.operator_token[:-2] + "xx"):
        with env.http(token) as h:
            assert h.get("/me").status_code == 401
    with env.http(None) as h:
        assert h.get("/health").status_code == 200


def test_sender_identity_comes_from_token(env):
    ch = env.op.create_channel("c")
    ident, token, _ = env.agent("honest", channels=["c"])
    with env.http(token) as h:
        r = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "k"},
                   json={"body": "hi", "sender": {"id": 1, "identity_type": "user"}})
        assert r.status_code == 400  # unknown fields rejected; no way to claim a sender
        r = h.post(f"/channels/{ch['id']}/messages", headers={"Idempotency-Key": "k"}, json={"body": "hi"})
    assert r.json()["sender"] == {"id": ident["id"], "external_id": "honest", "identity_type": "agent",
                                  "display_name": None}


def test_channel_boundaries(env):
    mine = env.op.create_channel("mine")
    other = env.op.create_channel("other")
    secret = env.op.post(other["id"], "secret")
    _, _, worker = env.agent("w", channels=["mine"])
    _, _, observer = env.agent("o", role="observer", channels=["mine"])

    assert [c["name"] for c in worker.list_channels()] == ["mine"]
    assert status(worker.read, other["id"]) == 404           # invisible, not just forbidden
    assert status(worker.post, other["id"], "x") == 404
    assert status(worker.channel_id, "other") == 404
    assert status(worker.post, mine["id"], "reply", reply_to=secret["id"]) == 400
    assert status(observer.read, mine["id"]) == 200
    assert status(observer.post, mine["id"], "x") == 403
    assert status(observer.upload, mine["id"], b"x") == 403
    assert status(worker.read, 9999) == 404


def test_channel_creation_is_a_separate_right(env):
    _, _, worker = env.agent("w")
    _, _, coord = env.agent("c", role="coordinator")
    hidden = env.op.create_channel("hidden")
    assert status(worker.create_channel, "nope") == 403
    created = coord.create_channel("team")
    assert [c["name"] for c in coord.list_channels()] == ["team"]  # creator gets its channel, nothing else
    assert status(coord.read, hidden["id"]) == 404
    coord.post(created["id"], "hello team")
    assert status(coord.create_channel, "hidden") == 409          # name taken, still no access
    assert coord.create_channel("team")["id"] == created["id"]    # creator retry is safe
    assert status(coord.provision_agent, "x") == 403
    with env.http(coord._http.headers["authorization"].split()[1]) as h:
        assert h.get("/metrics").status_code == 403
        assert h.post(f"/channels/{created['id']}/archive").status_code == 403


def test_workers_cannot_escalate(env):
    env.op.create_channel("c")
    ident, token, worker = env.agent("w", channels=["c"])
    other, _, _ = env.agent("v", channels=["c"])
    assert status(worker.provision_agent, "evil", role="admin") == 403
    assert status(worker.issue_token, ident["id"]) == 404    # cannot mint its own tokens
    assert status(worker.issue_token, other["id"]) == 404
    me = worker.me()
    assert me["role"] == "worker" and me["board_actions"] == []
    other_tok = env.op.issue_token(other["id"])
    assert status(worker.revoke_token, other_tok["token_id"]) == 404
    assert status(worker.revoke_token, me["token"]["id"]) == 200  # may revoke itself
    assert status(worker.me) == 401


def test_provisioner_ceiling(env):
    allowed = env.op.create_channel("allowed")
    env.op.create_channel("forbidden")
    prov_ident, _, prov = env.provisioner(policy={"channels": ["allowed"], "max_ttl_seconds": 600, "max_agents": 3})
    assert prov_ident["policy"]["roles"] == ["observer", "worker"]  # default ceiling

    worker = prov.provision_agent("w1", channels=["allowed"])
    assert prov.provision_agent("w1", channels=["allowed"])["id"] == worker["id"]  # idempotent
    assert status(prov.provision_agent, "p2", role="provisioner") == 403
    assert status(prov.provision_agent, "a2", role="admin") == 403
    assert status(prov.provision_agent, "c2", role="coordinator") == 403
    assert status(prov.provision_agent, "w2", channels=["forbidden"]) == 403
    assert status(prov.issue_token, worker["id"], ttl_seconds=601) == 403
    tok = prov.issue_token(worker["id"])
    assert tok["expires_at"] - time.time() <= 601

    # A provisioner has no message access of its own and cannot reach others' agents.
    assert status(prov.read, allowed["id"]) == 404
    admin_made, _, _ = env.agent("admin-made", channels=["allowed"])
    assert status(prov.issue_token, admin_made["id"]) == 404
    assert status(prov.issue_token, prov_ident["id"]) == 404
    _, _, prov2 = env.provisioner("launcher2", policy={"channels": ["*"]})
    assert status(prov2.issue_token, worker["id"]) == 404
    assert status(prov2.revoke_token, tok["token_id"]) == 404
    # Same external ID under a different issuer is a different identity.
    assert prov2.provision_agent("w1", channels=["allowed"])["id"] != worker["id"]

    prov.provision_agent("w2", channels=["allowed"])
    prov.provision_agent("w3", channels=["allowed"])
    assert status(prov.provision_agent, "w4", channels=["allowed"]) == 403  # max_agents


def test_registration_conflicts_need_explicit_update(env):
    a = env.op.create_channel("a")
    env.op.create_channel("b")
    _, _, prov = env.provisioner(policy={"channels": ["*"]})
    w = prov.provision_agent("w", channels=["a"])
    token = prov.issue_token(w["id"])["token"]
    worker = env.client(token)
    assert status(prov.provision_agent, "w", channels=["b"]) == 409
    assert status(prov.provision_agent, "w", channels=["a"], identity_type="user") == 409
    updated = prov.provision_agent("w", channels=["b"], update=True)
    assert updated["id"] == w["id"] and updated["channels"] == ["b"]
    assert status(worker.read, a["id"]) == 404  # grant removal applies to existing tokens immediately


def test_rotation_expiry_and_revocation(env):
    ch = env.op.create_channel("c")
    ident, token1, c1 = env.agent("w", channels=["c"])
    c1.post(ch["id"], "first")
    token2 = env.op.issue_token(ident["id"], ttl_seconds=3600)
    assert status(c1.me) == 401  # rotation replaced the old token
    c2 = env.client(token2["token"])
    assert c2.me()["id"] == ident["id"]
    env.op.revoke_token(token2["token_id"])
    assert status(c2.me) == 401

    short = env.op.issue_token(ident["id"], ttl_seconds=1)
    c3 = env.client(short["token"])
    assert c3.me()["id"] == ident["id"]
    time.sleep(1.2)
    with env.http(short["token"]) as h:
        r = h.get("/me")
    assert r.status_code == 401 and "expired" in r.json()["error"]["message"]


def test_revocation_applies_to_held_long_poll(env):
    ch = env.op.create_channel("c")
    _, token, reader = env.agent("r", role="observer", channels=["c"])
    token_id = reader.me()["token"]["id"]
    result = {}

    def poll():
        with env.http(token) as h:
            result["resp"] = h.get(f"/channels/{ch['id']}/messages", params={"wait": 10})

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.3)
    env.op.revoke_token(token_id)
    env.op.post(ch["id"], "you should not see this")
    t.join(timeout=10)
    assert result["resp"].status_code == 401


def test_admin_only_operations(env):
    _, token, _ = env.agent("w")
    with env.http(token) as h:
        assert h.get("/metrics").status_code == 403
    with env.http(env.operator_token) as h:
        m = h.get("/metrics").json()
    assert {"open_polls", "pending_writes", "db_bytes", "wal_bytes", "disk_free_bytes"} <= set(m)


@pytest.mark.parametrize("bad", [{"external_id": "../x"}, {"external_id": "ok", "role": "root"},
                                 {"external_id": "ok", "identity_type": "robot"},
                                 {"external_id": "ok", "role": "worker", "policy": {}}])
def test_provisioning_validation(env, bad):
    with env.http(env.operator_token) as h:
        assert h.post("/agents", json=bad).status_code == 400
