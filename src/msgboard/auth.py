"""Tokens, roles and the actions they expand to."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

TOKEN_PREFIX = "fbt_"

# Channel-scoped actions per role, and board-scoped actions per role.
CHANNEL_ACTIONS = {
    "observer": ("messages:read",),
    "worker": ("messages:read", "messages:write", "attachments:write"),
    "coordinator": ("messages:read", "messages:write", "attachments:write"),
    "provisioner": (),
    "admin": (),
}
BOARD_ACTIONS = {
    "observer": (),
    "worker": (),
    "coordinator": ("channels:create",),
    "provisioner": ("agents:provision",),
    "admin": ("admin",),
}
# What a channel creator receives on the channel it created.
CREATOR_ACTIONS = ("messages:read", "messages:write", "attachments:write")

ROLES = ("observer", "worker", "coordinator", "provisioner", "admin")
IDENTITY_TYPES = ("agent", "system", "user")
# Roles a provisioner may ever be allowed to hand out. Provisioners can never
# mint other provisioners or admins, whatever their policy says.
PROVISIONABLE_ROLES = ("observer", "worker", "coordinator")
DEFAULT_PROVISIONER_ROLES = ("observer", "worker")


@dataclass(frozen=True)
class Principal:
    id: int
    issuer: str
    external_id: str
    identity_type: str
    role: str
    display_name: str | None
    credential_id: str


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
