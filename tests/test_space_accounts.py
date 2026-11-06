from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hfspace"))
import accounts  # noqa: E402

from msgboard.cli import cmd_init  # noqa: E402
from msgboard.config import OPERATOR_TOKEN_FILE  # noqa: E402
from msgboard.server import Board, create_app  # noqa: E402

USERS = {"code-alice": {"preferred_username": "alice-hf", "name": "Alice A"},
         "code-bob": {"preferred_username": "bob-hf", "name": "Bob B"}}


def fake_provider(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/oauth/token":
        code = parse_qs(request.content.decode())["code"][0]
        return httpx.Response(200, json={"access_token": "at-" + code}) if code in USERS else httpx.Response(400)
    if request.url.path == "/oauth/userinfo":
        return httpx.Response(200, json=USERS[request.headers["authorization"].split("at-")[1]])
    if request.url.path == "/api/users/alice-hf/overview":
        return httpx.Response(200, json={"avatarUrl": "/avatars/alice.png"})
    if request.url.path == "/avatars/alice.png":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    if request.url.path == "/api/users/bob-hf/overview":           # default avatars are SVG: not adopted
        return httpx.Response(200, json={"avatarUrl": "/avatars/bob.svg"})
    if request.url.path == "/avatars/bob.svg":
        return httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    return httpx.Response(404)


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002"
                    "0000050001e226059b0000000049454e44ae426082")


@pytest.fixture
def space(tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPACE_HOST", "example.hf.space")
    monkeypatch.setattr(accounts, "_transport", httpx.MockTransport(fake_provider))
    assert cmd_init(Namespace(data_dir=str(tmp_path))) == 0
    operator = (tmp_path / OPERATOR_TOKEN_FILE).read_text().strip()
    board = Board(tmp_path)
    app = create_app(board)
    accounts.install(app, board)
    with TestClient(app, follow_redirects=False) as client:
        client.operator = operator
        yield client
    board.close()


def oauth(client, start_url, code):
    q = parse_qs(urlparse(start_url).query)
    assert q["redirect_uri"] == ["https://example.hf.space/login/callback"]
    return client.get("/login/callback", params={"code": code, "state": q["state"][0]})


def token_from(page) -> str:
    return page.text.split('setItem("msgboard:token", "')[1].split('"')[0]


def join_person(client, name):
    h = {"X-Msgboard-Token": client.operator}
    code = client.post("/v1/invites", headers=h, json={"kind": "human"}).json()["code"]
    return client.post("/v1/join", json={"code": code, "name": name}).json()


def test_hf_sign_in_needs_a_linked_account(space):
    assert space.get("/auth/config").json() == {"hf_login": True}
    page = oauth(space, space.get("/login").headers["location"], "code-alice")
    assert page.status_code == 404 and "No account linked" in page.text     # HF alone creates nothing

    alice = join_person(space, "alice")
    h = {"X-Msgboard-Token": alice["token"]}
    linked = oauth(space, space.post("/v1/me/hf-link", headers=h).json()["url"], "code-alice")
    assert linked.status_code == 200 and "@alice-hf" in linked.text
    assert space.get("/v1/me", headers=h).json()["hf_username"] == "alice-hf"

    signed_in = oauth(space, space.get("/login").headers["location"], "code-alice")
    assert signed_in.headers["cache-control"] == "no-store"
    second = {"X-Msgboard-Token": token_from(signed_in)}
    assert space.get("/v1/me", headers=second).json()["external_id"] == "alice"
    assert space.get("/v1/me", headers=h).status_code == 200                 # the first device keeps working


def test_one_hf_account_per_person_and_unlink(space):
    alice, bob = join_person(space, "alice"), join_person(space, "bob")
    ha, hb = {"X-Msgboard-Token": alice["token"]}, {"X-Msgboard-Token": bob["token"]}
    oauth(space, space.post("/v1/me/hf-link", headers=ha).json()["url"], "code-alice")
    taken = oauth(space, space.post("/v1/me/hf-link", headers=hb).json()["url"], "code-alice")
    assert taken.status_code == 409
    space.delete("/v1/me/hf-link", headers=ha)
    assert oauth(space, space.get("/login").headers["location"], "code-alice").status_code == 404


def test_state_is_single_use_and_agents_cannot_link(space):
    alice = join_person(space, "alice")
    h = {"X-Msgboard-Token": alice["token"]}
    url = space.post("/v1/me/hf-link", headers=h).json()["url"]
    assert oauth(space, url, "code-alice").status_code == 200
    assert oauth(space, url, "code-alice").status_code == 400
    assert space.get("/login/callback", params={"code": "code-bob", "state": "forged"}).status_code == 400
    invite = space.post("/v1/invites", headers=h, json={"kind": "agent"}).json()
    agent = space.post("/v1/join", json={"code": invite["code"], "name": "bot"}).json()
    assert space.post("/v1/me/hf-link", headers={"X-Msgboard-Token": agent["token"]}).status_code == 400


def test_shared_invite_code_is_imported(space, tmp_path):
    import asyncio
    board = space.app.state.board
    asyncio.run(accounts.import_shared_invite(board, "old-shared-code-123", space.operator))
    joined = space.post("/v1/join", json={"code": "old-shared-code-123", "name": "newcomer"})
    assert joined.status_code == 201 and joined.json()["principal"]["role"] == "member"


def test_linking_hf_adopts_the_hf_picture_but_never_replaces_an_upload(space):
    alice = join_person(space, "alice")
    h = {"X-Msgboard-Token": alice["token"]}
    assert space.get("/v1/me", headers=h).json()["avatar"] is None
    oauth(space, space.post("/v1/me/hf-link", headers=h).json()["url"], "code-alice")
    me = space.get("/v1/me", headers=h).json()
    assert me["avatar"] is not None                                             # the HF picture
    assert space.get(f"/v1/principals/{me['id']}/avatar", headers=h).content == PNG

    bob = join_person(space, "bob")
    hb = {"X-Msgboard-Token": bob["token"]}
    oauth(space, space.post("/v1/me/hf-link", headers=hb).json()["url"], "code-bob")
    assert space.get("/v1/me", headers=hb).json()["avatar"] is None             # SVG default: pattern kept

    own = PNG + b"own"                                                          # an upload always wins
    space.delete(f"/v1/principals/{me['id']}/avatar", headers=h)
    assert space.put(f"/v1/principals/{me['id']}/avatar", headers=h, content=own).status_code == 200
    oauth(space, space.get("/login").headers["location"], "code-alice")         # signing in again
    assert space.get(f"/v1/principals/{me['id']}/avatar", headers=h).content == own


def test_hf_signin_does_not_rejoin_general_after_leaving(space):
    operator = {"X-Msgboard-Token": space.operator}
    general = space.post("/v1/channels", headers=operator, json={"name": "general"}).json()["id"]
    alice = join_person(space, "alice")
    pid = alice["principal"]["id"]
    h = {"X-Msgboard-Token": alice["token"]}
    assert pid in {p["id"] for p in space.get(f"/v1/channels/{general}/members", headers=operator).json()["members"]}
    oauth(space, space.post("/v1/me/hf-link", headers=h).json()["url"], "code-alice")
    assert space.delete(f"/v1/channels/{general}/members/{pid}", headers=h).status_code == 200
    signed_in = oauth(space, space.get("/login").headers["location"], "code-alice")
    assert signed_in.status_code == 200
    assert pid not in {p["id"] for p in space.get(f"/v1/channels/{general}/members", headers=operator).json()["members"]}


def person_invite(client, **extra):
    return client.post("/v1/invites", headers={"X-Msgboard-Token": client.operator},
                       json={"kind": "human", **extra}).json()["code"]


def test_join_with_hf_from_the_invite_link(space):
    code = person_invite(space)
    page = oauth(space, space.get("/login", params={"invite": code}).headers["location"], "code-alice")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    h = {"X-Msgboard-Token": token_from(page)}
    me = space.get("/v1/me", headers=h).json()
    assert (me["external_id"], me["display_name"], me["hf_username"]) == ("alice-hf", "Alice A", "alice-hf")
    assert space.get(f"/v1/principals/{me['id']}/avatar", headers=h).content == PNG        # the HF picture
    again = oauth(space, space.get("/login").headers["location"], "code-alice")            # sign in elsewhere
    assert space.get("/v1/me", headers={"X-Msgboard-Token": token_from(again)}).json()["id"] == me["id"]


def test_join_with_hf_when_already_linked_signs_in_and_keeps_the_invite(space):
    alice = join_person(space, "alice")
    oauth(space, space.post("/v1/me/hf-link", headers={"X-Msgboard-Token": alice["token"]}).json()["url"], "code-alice")
    code = person_invite(space, max_uses=1)
    page = oauth(space, space.get("/login", params={"invite": code}).headers["location"], "code-alice")
    assert space.get("/v1/me", headers={"X-Msgboard-Token": token_from(page)}).json()["external_id"] == "alice"
    assert space.get(f"/v1/join/{code}").json()["status"] == "active"                       # not used up


def test_join_with_hf_when_the_name_is_taken_asks_for_another(space):
    join_person(space, "alice-hf")
    code = person_invite(space)
    page = oauth(space, space.get("/login", params={"invite": code}).headers["location"], "code-alice")
    assert page.status_code == 303
    q = parse_qs(urlparse(page.headers["location"]).query)
    assert q["invite"] == [code] and q["hf_user"] == ["alice-hf"]
    ticket = q["hf"][0]
    taken = space.post("/v1/join/hf", json={"ticket": ticket, "code": code, "name": "alice-hf"})
    assert taken.status_code == 409                                                        # the ticket survives
    joined = space.post("/v1/join/hf", json={"ticket": ticket, "name": "alice2"})
    assert joined.status_code == 201
    me = space.get("/v1/me", headers={"X-Msgboard-Token": joined.json()["token"]}).json()
    assert (me["external_id"], me["display_name"], me["hf_username"]) == ("alice2", "Alice A", "alice-hf")
    assert space.post("/v1/join/hf", json={"ticket": ticket, "name": "alice3"}).status_code == 410   # single use
    assert space.post("/v1/join/hf", json={"ticket": "forged", "name": "alice3"}).status_code == 410


def test_join_with_hf_needs_a_valid_people_invite(space):
    page = oauth(space, space.get("/login", params={"invite": "not-a-real-code"}).headers["location"], "code-alice")
    assert page.status_code == 404 and "No account linked" not in page.text
    agent_code = space.post("/v1/invites", headers={"X-Msgboard-Token": space.operator}, json={"kind": "agent"}).json()["code"]
    page = oauth(space, space.get("/login", params={"invite": agent_code}).headers["location"], "code-bob")
    assert page.status_code == 400
    assert oauth(space, space.get("/login").headers["location"], "code-bob").status_code == 404   # nothing was created
