"""Minimal HS256 JWT helpers plus dispatcher id generation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

_DISPATCHER_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_DISPATCHER_ID_LENGTH = 21
DISPATCHER_ID_PATTERN = "[" + re.escape(_DISPATCHER_ID_ALPHABET) + "]{" + str(_DISPATCHER_ID_LENGTH) + "}"

_b64url_encoder = base64.urlsafe_b64encode
_b64url_decoder = base64.urlsafe_b64decode


def new_dispatcher_id() -> str:
    return "".join(secrets.choice(_DISPATCHER_ID_ALPHABET) for _ in range(_DISPATCHER_ID_LENGTH))


def generate_secret() -> bytes:
    return secrets.token_bytes(32)


def issue_token(secret: bytes, dispatcher_id: str, ttl_seconds: int, *, now: float | None = None) -> str:
    issued_at = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": dispatcher_id, "iat": issued_at, "exp": issued_at + ttl_seconds}
    encoded_header = _b64url_encoder(json.dumps(header, separators=(",", ":")).encode()).rstrip(b"=").decode()
    encoded_payload = _b64url_encoder(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    encoded_signature = _b64url_encoder(signature).rstrip(b"=").decode()
    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"


def verify_token(secret: bytes, token: str, *, now: float | None = None) -> str | None:
    """Return the dispatcher id carried by a valid token, else ``None``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded_header, encoded_payload, encoded_signature = parts
    try:
        header = json.loads(_b64url_decoder(encoded_header + "=" * (-len(encoded_header) % 4)))
        payload = json.loads(_b64url_decoder(encoded_payload + "=" * (-len(encoded_payload) % 4)))
        signature = _b64url_decoder(encoded_signature + "=" * (-len(encoded_signature) % 4))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None
    if not isinstance(payload, dict):
        return None
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    dispatcher_id = payload.get("sub")
    expires_at = payload.get("exp")
    if not isinstance(dispatcher_id, str):
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    if expires_at < int(now if now is not None else time.time()):
        return None
    return dispatcher_id
