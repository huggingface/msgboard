"""Optional Hugging Face accounts for the msgboard Space.

People join with invite links: with a username they pick, or with "Join with
Hugging Face", which uses their HF username, name and picture and links the account.
Once linked (at join or later from their profile), "Sign in with Hugging Face"
gives them a token on any device. HF sign-in never creates an account without an invite.
"""

import hashlib
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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


def join_with_hf(conn, code, name, display_name, username: str, now: float) -> dict:
    """Redeem a people invite and link the HF account in one go; nothing is kept if either fails."""
    row = store._find_invite(conn, code)
    if row is not None and row["kind"] != "human":
        raise ApiError(400, "invalid_request", "only invites for people can be used with Hugging Face")
    result = store.join(conn, code, name, display_name, now)
    store.set_hf_username(conn, result["principal"]["id"], username, now)
    result["principal"]["hf_username"] = username
    return result


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
    pending: dict[str, tuple] = {}  # state -> (created, mode, principal to link or invite code)
    # ticket -> (created, invite code, HF username, HF name): an HF join whose username was taken here
    tickets: dict[str, tuple[float, str, str, str | None]] = {}

    def prune(entries: dict) -> None:
        now = time.time()
        for s, entry in list(entries.items()):
            if now - entry[0] > 600:
                del entries[s]
        if len(entries) > 5000:
            raise ApiError(429, "busy", "too many sign-ins in progress; try again shortly")

    def start(mode: str, target: int | str | None) -> str:
        prune(pending)
        state = secrets.token_urlsafe(24)
        pending[state] = (time.time(), mode, target)
        return f"{provider}/oauth/authorize?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "scope": "openid profile",
            "state": state, "response_type": "code"})

    @app.get("/auth/config")
    async def auth_config():
        return {"hf_login": enabled}

    @app.get("/login")
    async def login(invite: str | None = None):
        if not enabled:
            return _page("Hugging Face sign-in is not set up", "<p>Use an invite link or a token.</p>", 404)
        return RedirectResponse(start("join", invite) if invite else start("signin", None))

    @app.post("/v1/join/hf")
    async def join_after_name_taken(request: Request):
        """Finish an HF join with a name the person picked, because their HF username was taken here."""
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_request", "expected a JSON object")
        ticket = body.get("ticket") if isinstance(body.get("ticket"), str) else ""
        entry = tickets.pop(ticket, None)
        if entry is None or time.time() - entry[0] > 600:
            raise ApiError(410, "hf_join_expired", "the Hugging Face sign-in expired; join with Hugging Face again")
        _, code, username, hf_name = entry
        try:
            result = await board.db.write(join_with_hf, code, body.get("name"), body.get("display_name") or hf_name,
                                          username, time.time())
        except ApiError as exc:
            if exc.status in (400, 409) and exc.code != "hf_linked_elsewhere":
                tickets[ticket] = entry  # let them try another name
            raise
        await adopt_hf_picture(board, result["principal"]["id"], username)
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

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

    async def hf_join(code: str, username: str, hf_name: str | None) -> dict | str:
        """Sign in if this HF account is linked already (the invite stays unused), else join with it.
        Returns the join result, or where to send the person to pick another name."""
        try:
            return await board.db.write(store.sign_in_with_hf, username, time.time())
        except ApiError as exc:
            if exc.status != 404:
                raise
        display = hf_name.strip()[:128] if isinstance(hf_name, str) and hf_name.strip() else None
        try:
            return await board.db.write(join_with_hf, code, username, display, username, time.time())
        except ApiError as exc:
            if exc.code != "name_taken":
                raise
        prune(tickets)
        ticket = secrets.token_urlsafe(24)
        tickets[ticket] = (time.time(), code, username, display)
        return "/?" + urlencode({"invite": code, "hf": ticket, "hf_user": username})

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
        _, mode, target = entry
        try:
            if mode == "link":
                await board.db.write(store.set_hf_username, target, username, time.time())
                await adopt_hf_picture(board, target, username)
                return _page("Hugging Face account linked",
                             f"<p>You can now sign in as <b>@{html.escape(username)}</b> on any device.</p>"
                             '<p><a href="/">Back to the board</a></p>')
            if mode == "join":
                result = await hf_join(target, username, info.get("name"))
                if isinstance(result, str):
                    return RedirectResponse(result, status_code=303)
            else:
                result = await board.db.write(store.sign_in_with_hf, username, time.time())
            if result.get("principal", {}).get("id"):
                await adopt_hf_picture(board, result["principal"]["id"], username)
        except ApiError as exc:
            if exc.status == 404 and mode != "join":
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
