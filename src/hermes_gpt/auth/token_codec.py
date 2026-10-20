"""Token wire encoding and cryptographic validation for Hermes GPT OAuth.

This module owns the self-contained format and MAC layers shared by
authorization codes and access tokens:

- URL-safe base64 helpers with canonical-spelling enforcement;
- canonical JSON payload encoding;
- HMAC-SHA256 signing and constant-time signature verification;
- PKCE S256 derivation and verifier/challenge shape checks;
- the access-token HMAC key derivation from the confidential client secret;
- payload shape, version, type, nonce, and expiry validation that does not
  depend on client configuration or durable state.

It deliberately does **not** import ``oauth_auth``: grant lifecycle, replay
prevention, revocation epochs, scope policy, and durable persistence stay in
``oauth_auth.OAuthState`` (which imports this module). Codec functions take
explicit keys and payloads and hold no grant or durable state. Expiry uses
the clock and issuance uses secure randomness, as before the extraction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

ACCESS_TOKEN_PREFIX = "hg.at.v1."
ACCESS_TOKEN_MAC_CONTEXT = b"hermes-gpt.oauth.access.v1\0"
MAX_TOKEN_VALUE_LENGTH = 4096

_PKCE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

_AUTHORIZATION_CODE_FIELDS = {
    "v": int,
    "nonce": str,
    "client_id": str,
    "redirect_uri": str,
    "scope": str,
    "resource": str,
    "code_challenge": str,
    "expires_at": int,
}
_ACCESS_TOKEN_FIELDS = {
    "v": int,
    "typ": str,
    "nonce": str,
    "client_id": str,
    "scope": str,
    "resource": str,
    "expires_at": int,
}

_INVALID_AUTHORIZATION_CODE_DESCRIPTION = (
    "Invalid, expired, or already used authorization code."
)


class OAuthError(RuntimeError):
    """An OAuth protocol error carrying the wire error code and HTTP status.

    Defined here because the codec raises it directly for malformed signed
    authorization codes; ``oauth_auth`` re-exports this exact class so callers
    and ``except`` clauses keep their identity.
    """

    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


def invalid_authorization_code_error() -> OAuthError:
    """The single fail-closed error used for every bad authorization code."""
    return OAuthError("invalid_grant", _INVALID_AUTHORIZATION_CODE_DESCRIPTION)


def base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def base64url_decode(value: str) -> bytes:
    if not value or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64url_encode(digest)


def valid_pkce_verifier(verifier: str) -> bool:
    return bool(_PKCE_VALUE.fullmatch(verifier))


def valid_pkce_challenge(challenge: str) -> bool:
    return bool(_PKCE_VALUE.fullmatch(challenge))


def access_token_key(client_secret: str) -> bytes:
    """Derive the clustered access-token HMAC key from the shared client secret.

    Clustered origins (the same public MCP hostname served by more than one
    process) share ``HERMES_GPT_OAUTH_CLIENT_SECRET`` but not process memory.
    Opaque ``token_urlsafe`` access tokens therefore 401 on the origin that
    did not issue them. HMAC-SHA256 over a versioned payload lets any origin
    with the same confidential client secret validate the bearer without a
    shared token table. Rotating the client secret invalidates every signed
    access token.
    """
    return hashlib.sha256(
        ACCESS_TOKEN_MAC_CONTEXT + client_secret.encode("utf-8")
    ).digest()


def encode_signed_payload(payload: dict[str, Any], *, key: bytes) -> str:
    """Return ``<base64url(json)>.<base64url(hmac-sha256)>`` for ``payload``."""
    encoded = base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{base64url_encode(signature)}"


def _decode_signed_json(value: str, *, key: bytes, prefix: str) -> Any:
    """Verify the MAC over the signed body and parse its JSON payload.

    Raises ``ValueError`` (or a JSON/Unicode decode error) for a missing
    separator, non-canonical base64url, a MAC mismatch, or unparseable JSON.
    """
    body = value[len(prefix):] if prefix else value
    encoded, separator, encoded_signature = body.partition(".")
    if not separator:
        raise ValueError("missing signature separator")
    supplied_signature = base64url_decode(encoded_signature)
    expected_signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("signature mismatch")
    return json.loads(base64url_decode(encoded))


def _matches_required_fields(payload: Any, required: dict[str, type]) -> bool:
    return isinstance(payload, dict) and all(
        isinstance(payload.get(field), kind) for field, kind in required.items()
    )


def decode_authorization_code(code: str, *, key: bytes) -> dict[str, Any]:
    """Verify and decode a signed authorization code into its payload.

    Raises ``OAuthError('invalid_grant', ...)`` for any length, structure, MAC,
    field-type, version, or nonce failure. Replay and revocation-epoch checks
    require caller state and stay in ``oauth_auth.OAuthState``.
    """
    if len(code) > MAX_TOKEN_VALUE_LENGTH:
        raise invalid_authorization_code_error()
    try:
        payload = _decode_signed_json(code, key=key, prefix="")
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise invalid_authorization_code_error() from exc
    if not _matches_required_fields(payload, _AUTHORIZATION_CODE_FIELDS):
        raise invalid_authorization_code_error()
    if payload["v"] not in (1, 2) or not _NONCE.fullmatch(payload["nonce"]):
        raise invalid_authorization_code_error()
    return payload


def decode_signed_access_token(token_value: str, *, key: bytes) -> dict[str, Any] | None:
    """Return the verified v1 access-token payload, or ``None`` if unusable.

    ``None`` covers a missing/foreign prefix, oversize values, MAC mismatch,
    unparseable JSON, wrong field types, unknown version, wrong type, malformed
    nonce, and expiry. Resource binding, client registration, scope policy, and
    the durable revocation epoch depend on configuration/state and are applied
    by ``oauth_auth.OAuthState``.
    """
    if (
        not token_value.startswith(ACCESS_TOKEN_PREFIX)
        or len(token_value) > MAX_TOKEN_VALUE_LENGTH
    ):
        return None
    try:
        payload = _decode_signed_json(
            token_value, key=key, prefix=ACCESS_TOKEN_PREFIX
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not _matches_required_fields(payload, _ACCESS_TOKEN_FIELDS):
        return None
    if (
        payload["v"] not in (1, 2)
        or payload["typ"] != "access"
        or not _NONCE.fullmatch(payload["nonce"])
    ):
        return None
    if payload["expires_at"] <= time.time():
        return None
    return payload


def encode_access_token(
    *,
    key: bytes,
    client_id: str,
    scope: str,
    resource: str,
    expires_at: int,
) -> tuple[str, dict[str, Any]]:
    """Mint a signed access token and the matching store item.

    The MAC key is an explicit input (see :func:`access_token_key`) so the
    caller keeps control of key derivation. The item carries only public
    metadata (client id, scope, resource, expiry); token material is returned
    separately and is never logged.
    """
    payload = {
        "v": 1,
        "typ": "access",
        "nonce": secrets.token_urlsafe(24),
        "client_id": client_id,
        "scope": scope,
        "resource": resource,
        "expires_at": expires_at,
    }
    token_value = ACCESS_TOKEN_PREFIX + encode_signed_payload(payload, key=key)
    item = {
        "client_id": client_id,
        "scope": scope,
        "resource": resource,
        "expires_at": float(expires_at),
    }
    return token_value, item
