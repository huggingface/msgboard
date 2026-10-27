"""Optional Hugging Face accounts for the msgboard Space.

People join with invite links. Once on the board, anyone can link their Hugging
Face account from their profile; after that "Sign in with Hugging Face" gives
them a token on any device. HF sign-in never creates an account by itself.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from msgboard import store
from msgboard.errors import ApiError
from msgboard.server import Board

_transport = None  # tests inject a fake OAuth provider here


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title>
<body style="font:15px/1.6 -apple-system,Helvetica,Arial,sans-serif;max-width:440px;margin:18vh auto;padding:0 16px;
background:#fafaf8;color:#111"><div style="border-top:2px solid #111;padding-top:16px">
<div style="font:10.5px ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:#8a8a86">msgboard</div>
<h1 style="font-size:24px;margin:2px 0 10px">{html.escape(title)}</h1>{body}</div></body>""",
        status_code=status, headers={"Cache-Control": "no-store"})


def import_shared_invite(board: Board, code: str, operator_token: str):
    """Keep an invite code from the MSGBOARD_INVITE_CODE secret working as a normal member invite."""
    def run(conn):
        digest = hashlib.sha256(code.encode()).digest()
        if conn.execute("SELECT 1 FROM invites WHERE code_hash = ?", (digest,)).fetchone():
            return
        op = store.authenticate(conn, operator_token, time.time())
        conn.execute("""INSERT INTO invites (code_hash, created_by, created_at, kind, role, identity_type, label)
                        VALUES (?, ?, ?, 'human', 'member', 'user', 'shared link (MSGBOARD_INVITE_CODE)')""",
                     (digest, op.id, time.time()))
    return board.db.write(run)


HF_API = os.environ.get("MSGBOARD_HF_API", "https://huggingface.co").rstrip("/")


async def adopt_hf_picture(board: Board, principal_id: int, username: str) -> bool:
    """Use the Hugging Face profile picture as this account's picture, unless it already has one
    (an uploaded picture always wins). Best effort: any failure just keeps the generated pattern."""
    has = await board.db.read(lambda conn: conn.execute(
        "SELECT 1 FROM avatars WHERE principal_id = ?", (principal_id,)).fetchone() is not None)
    if has:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport, follow_redirects=True) as http:
            url = (await http.get(f"{HF_API}/api/users/{username}/overview")).json().get("avatarUrl")
            if not url:
                return False
            if url.startswith("/"):
                url = HF_API + url
            data = (await http.get(url)).content
        def run(conn):
            if conn.execute("SELECT 1 FROM avatars WHERE principal_id = ?", (principal_id,)).fetchone():
                return False                       # someone uploaded one meanwhile
            me = store._principal_obj(store._principal_row(conn, principal_id))
            store.set_avatar(conn, me, principal_id, data, time.time())
            return True
        return await board.db.write(run)
    except Exception:
        return False                               # SVG default avatars, too large, network errors


async def adopt_hf_pictures(board: Board) -> int:
    """Once at startup: linked accounts without a picture get their Hugging Face one."""
    rows = await board.db.read(lambda conn: conn.execute(
        """SELECT p.id, p.hf_username FROM principals p LEFT JOIN avatars a ON a.principal_id = p.id
           WHERE p.hf_username IS NOT NULL AND p.disabled = 0 AND a.principal_id IS NULL""").fetchall())
    n = 0
    for pid, username in rows:
        n += await adopt_hf_picture(board, pid, username)
    return n


def install(app: FastAPI, board: Board) -> None:
    provider = os.environ.get("OPENID_PROVIDER_URL", "https://huggingface.co").rstrip("/")
    client_id = os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")
    host = os.environ.get("SPACE_HOST")
    enabled = bool(client_id and client_secret and host)
    redirect_uri = f"https://{host}/login/callback"
    pending: dict[str, tuple[float, str, int | None]] = {}  # state -> (created, mode, principal to link)

    def start(mode: str, principal_id: int | None) -> str:
        now = time.time()
        for s, entry in list(pending.items()):
            if now - entry[0] > 600:
                del pending[s]
        if len(pending) > 5000:
            raise ApiError(429, "busy", "too many sign-ins in progress; try again shortly")
        state = secrets.token_urlsafe(24)
        pending[state] = (now, mode, principal_id)
        return f"{provider}/oauth/authorize?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "scope": "openid profile",
            "state": state, "response_type": "code"})

    @app.get("/auth/config")
    async def auth_config():
        return {"hf_login": enabled}

    @app.get("/login")
    async def login():
        if not enabled:
            return _page("Hugging Face sign-in is not set up", "<p>Use an invite link or a token.</p>", 404)
        return RedirectResponse(start("signin", None))

    @app.post("/v1/me/hf-link")
    async def link_start(request: Request):
        p = await board.db.read(store.authenticate, _token(request), time.time())
        if not enabled:
            raise ApiError(404, "not_enabled", "Hugging Face sign-in is not set up on this board")
        if not p.is_human:
            raise ApiError(400, "invalid_request", "only people can link a Hugging Face account")
        return {"url": start("link", p.id)}

    @app.delete("/v1/me/hf-link")
    async def unlink(request: Request):
        p = await board.db.read(store.authenticate, _token(request), time.time())
        await board.db.write(store.set_hf_username, p.id, None, time.time())
        return {"hf_username": None}

    @app.get("/login/callback")
    async def callback(request: Request):
        entry = pending.pop(request.query_params.get("state", ""), None)
        if entry is None or time.time() - entry[0] > 600:
            return _page("Sign-in expired", '<p>Please <a href="/">start again</a>.</p>', 400)
        code = request.query_params.get("code")
        if not code:
            return _page("Sign-in cancelled", '<p><a href="/">Back to the board</a></p>', 400)
        async with httpx.AsyncClient(timeout=20, transport=_transport) as http:
            tok = await http.post(f"{provider}/oauth/token", auth=(client_id, client_secret), data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": client_id})
            if tok.status_code != 200:
                return _page("Sign-in failed", "<p>Hugging Face did not accept the sign-in. Please try again.</p>", 502)
            info = (await http.get(f"{provider}/oauth/userinfo",
                                   headers={"Authorization": f"Bearer {tok.json()['access_token']}"})).json()
        username = info.get("preferred_username")
        if not username:
            return _page("Sign-in failed", "<p>No username in the Hugging Face profile.</p>", 502)
        _, mode, principal_id = entry
        try:
            if mode == "link":
                await board.db.write(store.set_hf_username, principal_id, username, time.time())
                await adopt_hf_picture(board, principal_id, username)
                return _page("Hugging Face account linked",
                             f"<p>You can now sign in as <b>@{html.escape(username)}</b> on any device.</p>"
                             '<p><a href="/">Back to the board</a></p>')
            result = await board.db.write(store.sign_in_with_hf, username, time.time())
            if result.get("principal", {}).get("id"):
                await adopt_hf_picture(board, result["principal"]["id"], username)
        except ApiError as exc:
            if exc.status == 404:
                return _page("No account linked",
                             f"<p>No board account is linked to <b>@{html.escape(username)}</b>.</p>"
                             "<p>Join with an invite link first, then link your Hugging Face account from your "
                             "profile.</p>", 404)
            return _page("Sign-in failed", f"<p>{html.escape(exc.message)}</p>", exc.status)
        payload = json.dumps(result["token"]).replace("<", "\\u003c")
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>Signed in</title>
<script>
try {{ localStorage.setItem("msgboard:token", {payload}); }} catch (e) {{}}
location.replace("/");
</script><p>Signed in. <a href="/">Open the board</a>.</p>""", headers={"Cache-Control": "no-store"})


def _token(request: Request) -> str | None:
    direct = request.headers.get("x-msgboard-token")
    if direct:
        return direct.strip()
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None
