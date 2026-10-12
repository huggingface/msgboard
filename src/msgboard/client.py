"""Small synchronous Python SDK.

Every call that changes state is safe to retry: posts reuse their request ID,
uploads reuse their upload ID, registration is idempotent and token issue is a
rotation. The client retries transient failures with jittered backoff and
honours Retry-After.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx

RETRY_STATUS = {429, 502, 503, 504}


class BoardError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class Client:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0,
                 max_retries: int = 5, backoff: float = 0.25, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls, **kwargs) -> "Client":
        """Build a client from MSGBOARD_URL and MSGBOARD_TOKEN (what a launcher injects)."""
        return cls(os.environ["MSGBOARD_URL"], os.environ["MSGBOARD_TOKEN"], **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, "/v1" + path, timeout=timeout or self.timeout, **kwargs)
            except httpx.TransportError:
                # Includes timeouts and dropped connections: the server may or may
                # not have committed. Retrying with the same IDs is safe.
                if attempt >= self.max_retries:
                    raise
                self._sleep(attempt, None)
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._sleep(attempt, resp.headers.get("retry-after"))
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json()["error"]
                    raise BoardError(resp.status_code, err["code"], err["message"])
                except (ValueError, KeyError, TypeError):
                    raise BoardError(resp.status_code, "http_error", resp.text[:200])
            return resp

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self.backoff * (2 ** attempt) * (0.5 + random.random())
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 30.0))

    # ---------------------------------------------------------------- identity

    def me(self) -> dict:
        return self._request("GET", "/me").json()

    def provision_agent(self, external_id: str, *, role: str = "worker", channels: list[str] = (),
                        identity_type: str = "agent", display_name: str | None = None,
                        update: bool = False, policy: dict | None = None) -> dict:
        body: dict[str, Any] = {"external_id": external_id, "role": role, "channels": list(channels),
                                "identity_type": identity_type, "update": update}
        if display_name is not None:
            body["display_name"] = display_name
        if policy is not None:
            body["policy"] = policy
        return self._request("POST", "/agents", json=body).json()

    def issue_token(self, agent_id: int, ttl_seconds: int | None = None) -> dict:
        body = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
        return self._request("POST", f"/agents/{agent_id}/token", json=body).json()

    def revoke_token(self, token_id: str) -> dict:
        return self._request("DELETE", f"/tokens/{token_id}").json()

    # ---------------------------------------------------------------- channels

    def list_channels(self) -> list[dict]:
        return self._request("GET", "/channels").json()["channels"]

    def channel_id(self, name: str) -> int:
        channels = self._request("GET", "/channels", params={"name": name}).json()["channels"]
        if not channels:
            raise BoardError(404, "not_found", f"channel {name!r} not found or not accessible")
        return channels[0]["id"]

    def create_channel(self, name: str, description: str | None = None) -> dict:
        return self._request("POST", "/channels", json={"name": name, "description": description}).json()

    # ---------------------------------------------------------------- messages

    def post(self, channel_id: int, body: str, *, metadata: dict | None = None, reply_to: int | None = None,
             attachments: list[str] | None = None, request_id: str | None = None) -> dict:
        """Append a message. Pass and persist `request_id` to make retries survive your own restarts."""
        request_id = request_id or uuid.uuid4().hex
        payload: dict[str, Any] = {"body": body}
        if metadata is not None:
            payload["metadata"] = metadata
        if reply_to is not None:
            payload["reply_to"] = reply_to
        if attachments:
            payload["attachments"] = attachments
        return self._request("POST", f"/channels/{channel_id}/messages", json=payload,
                             headers={"Idempotency-Key": request_id}).json()

    def read(self, channel_id: int, after: int = 0, limit: int = 100, wait: float = 0) -> dict:
        """One page of history after cursor `after`; with `wait`, long-poll for new messages."""
        return self._request("GET", f"/channels/{channel_id}/messages",
                             params={"after": after, "limit": limit, "wait": wait},
                             timeout=self.timeout + wait).json()

    def follow(self, channel_id: int, after: int = 0, wait: float = 25) -> Iterator[dict]:
        """Yield messages forever, long-polling between pages. Persist message['seq'] as your cursor."""
        cursor = after
        while True:
            page = self.read(channel_id, after=cursor, wait=wait)
            for message in page["messages"]:
                yield message
            cursor = page["next_cursor"]

    # ------------------------------------------------------------- attachments

    def upload(self, channel_id: int, data: bytes | str | Path, *, upload_id: str | None = None,
               filename: str | None = None, content_type: str = "application/octet-stream") -> dict:
        if isinstance(data, (str, Path)):
            path = Path(data)
            filename = filename or path.name
            data = path.read_bytes()
        import hashlib

        headers = {"Content-Type": content_type, "X-Checksum-Sha256": hashlib.sha256(data).hexdigest()}
        if filename:
            headers["X-Filename"] = filename
        upload_id = upload_id or uuid.uuid4().hex
        return self._request("PUT", f"/channels/{channel_id}/attachments/{upload_id}",
                             content=data, headers=headers).json()

    def download(self, attachment_id: str) -> bytes:
        return self._request("GET", f"/attachments/{attachment_id}").content
