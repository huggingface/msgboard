"""Tokens, roles and short-lived browser download links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

from .errors import ApiError

TOKEN_PREFIX = "fbt_"

IDENTITY_TYPES = ("user", "agent", "system")  # a person, an AI agent, a service account
HUMAN_ROLES = ("admin", "member", "viewer", "guest")   # guest: only the channels they were added to
AGENT_ROLES = ("admin", "reader", "member", "guest")
ADMIN_ONLY_AGENT_ROLES = ("admin", "reader")  # only admins can create (or own) these


def owner_roles_for(agent_role: str) -> tuple[str, ...]:
    """Roles the owner of an agent with this role must have for the agent to keep working."""
    if agent_role in ADMIN_ONLY_AGENT_ROLES:
        return ("admin",)
    if agent_role == "guest":
        return ("admin", "member", "guest")
    return ("admin", "member")
ROLES = ("admin", "member", "viewer", "guest", "reader")


@dataclass(frozen=True)
class Principal:
    id: int
    issuer: str
    external_id: str
    identity_type: str
    role: str
    display_name: str | None
    owner_id: int | None
    can_create_channels: bool
    credential_id: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_human(self) -> bool:
        return self.identity_type == "user"

    @property
    def sees_everything(self) -> bool:
        return self.role in ("admin", "reader")

    @property
    def may_post(self) -> bool:
        return self.role not in ("viewer", "reader")

    @property
    def may_create_channels(self) -> bool:
        if self.role == "admin":
            return True
        if self.is_human:
            return self.role == "member"
        return self.role == "member" and self.can_create_channels

    @property
    def may_invite_agents(self) -> bool:
        # guests may invite agents too, but only guest agents limited to their own channels (store.create_invite)
        return self.role == "admin" or (self.is_human and self.role in ("member", "guest"))


def new_token() -> tuple[str, str, bytes]:
    """Return (credential_id, full token, sha256 of secret)."""
    cred_id = secrets.token_hex(8)
    secret = secrets.token_bytes(32)
    secret_text = base64.urlsafe_b64encode(secret).rstrip(b"=").decode()
    return cred_id, f"{TOKEN_PREFIX}{cred_id}.{secret_text}", hashlib.sha256(secret).digest()


def parse_token(token: str) -> tuple[str, bytes] | None:
    """Split a token into (credential_id, secret bytes); None if malformed."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    try:
        cred_id, secret_text = token[len(TOKEN_PREFIX):].split(".", 1)
        secret = base64.urlsafe_b64decode(secret_text + "=" * (-len(secret_text) % 4))
    except (ValueError, TypeError):
        return None
    if len(cred_id) != 16 or len(secret) != 32:
        return None
    return cred_id, secret


def secret_matches(secret: bytes, stored_hash: bytes) -> bool:
    return hmac.compare_digest(hashlib.sha256(secret).digest(), bytes(stored_hash))


DOWNLOAD_TTL_SECONDS = 120


@dataclass(frozen=True)
class DownloadLink:
    principal_id: int
    token: str = field(repr=False)
    file_id: str
    sha256: str
    expires: float


class DownloadLinks:
    def __init__(self, ttl_seconds: float = DOWNLOAD_TTL_SECONDS, max_links: int = 2048):
        self.ttl_seconds = ttl_seconds
        self.max_links = max_links
        self.links: dict[str, DownloadLink] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, link in self.links.items() if link.expires <= now]:
            del self.links[key]

    def issue(self, principal_id: int, token: str, file_id: str, sha256: str) -> str:
        self._prune()
        if len(self.links) >= self.max_links or sum(link.principal_id == principal_id for link in self.links.values()) >= 32:
            raise ApiError(429, "too_many_downloads", "too many recent downloads; try again shortly",
                           {"Retry-After": str(DOWNLOAD_TTL_SECONDS)})
        key = secrets.token_urlsafe(24)
        self.links[key] = DownloadLink(principal_id, token, file_id, sha256, time.monotonic() + self.ttl_seconds)
        return key

    def get(self, key: str) -> DownloadLink:
        self._prune()
        link = self.links.get(key)
        if link is None:
            raise ApiError(404, "download_expired", "download link expired; click Download again")
        return link
