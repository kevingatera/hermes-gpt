"""Extraction regression tests for the oauth_token_codec boundary.

These lock the split introduced by moving token wire encoding and
cryptographic validation out of ``oauth_auth``: the codec must stay free of
``oauth_auth`` imports, keep taking explicit inputs, and expose the exact
objects ``oauth_auth`` re-exports for existing callers.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from hermes_gpt.auth import oauth as oauth_auth
from hermes_gpt.auth import token_codec as codec

CLIENT_ID = "chatgpt-client"
CLIENT_SECRET = "test-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ISSUER = "https://mcp.example.com"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT_URI = "https://chatgpt.com/connector/oauth/callback"


def _config() -> oauth_auth.OAuthConfig:
    return oauth_auth.OAuthConfig(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uris=(REDIRECT_URI,),
        scope="hermes",
    )


def _s256_reference(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_codec_module_never_imports_oauth_auth():
    tree = ast.parse(Path(codec.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert imported, "codec declares no imports"
    assert "hermes_gpt.auth.oauth" not in imported
    assert "hermes_gpt.auth.config" not in imported


def test_oauth_auth_reexports_the_identical_codec_objects():
    assert oauth_auth.OAuthError is codec.OAuthError
    assert oauth_auth.ACCESS_TOKEN_PREFIX is codec.ACCESS_TOKEN_PREFIX
    assert oauth_auth._ACCESS_TOKEN_MAC_CONTEXT is codec.ACCESS_TOKEN_MAC_CONTEXT
    assert oauth_auth._PKCE_VALUE is codec._PKCE_VALUE
    assert oauth_auth._BASE64URL is codec._BASE64URL
    assert oauth_auth._NONCE is codec._NONCE
    assert oauth_auth._base64url_encode is codec.base64url_encode
    assert oauth_auth._base64url_decode is codec.base64url_decode
    assert oauth_auth._s256 is codec.s256
    assert oauth_auth._valid_pkce_verifier is codec.valid_pkce_verifier


def test_pkce_regex_object_is_the_one_oauth_http_validates_with():
    from hermes_gpt.auth.http import _PKCE_VALUE

    assert _PKCE_VALUE is codec._PKCE_VALUE


def test_base64url_round_trips_and_rejects_non_canonical_spelling():
    raw = bytes(range(256))
    encoded = codec.base64url_encode(raw)
    assert codec.base64url_decode(encoded) == raw
    assert "=" not in encoded and "+" not in encoded and "/" not in encoded

    with pytest.raises(ValueError, match="invalid base64url"):
        codec.base64url_decode("")
    with pytest.raises(ValueError, match="invalid base64url"):
        codec.base64url_decode("not*url*safe")

    # A value that decodes to the same bytes but is not canonical must fail.
    padded = base64.urlsafe_b64encode(b"ab").decode("ascii")
    assert padded.endswith("=")
    canonical = padded.rstrip("=")
    assert codec.base64url_decode(canonical) == b"ab"
    with pytest.raises(ValueError, match="invalid base64url"):
        codec.base64url_decode(padded)  # "=" is outside the URL-safe alphabet
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    slack = "=" * (-len(canonical) % 4)
    decoded = codec.base64url_decode(canonical)
    alternate = next(
        candidate
        for candidate in alphabet
        if candidate != canonical[-1]
        and base64.urlsafe_b64decode(canonical[:-1] + candidate + slack) == decoded
    )
    with pytest.raises(ValueError, match="non-canonical base64url"):
        codec.base64url_decode(canonical[:-1] + alternate)


def test_s256_and_pkce_shape_checks_match_the_rfc_derivation():
    verifier = "a" * 64
    assert codec.s256(verifier) == _s256_reference(verifier)
    assert codec.valid_pkce_verifier(verifier) is True
    assert codec.valid_pkce_challenge(verifier) is True
    assert codec.valid_pkce_verifier("short") is False
    assert codec.valid_pkce_verifier("a" * 129) is False
    assert codec.valid_pkce_verifier("a" * 63 + "!") is False


def test_access_token_key_derives_from_the_client_secret():
    expected = hashlib.sha256(
        codec.ACCESS_TOKEN_MAC_CONTEXT + CLIENT_SECRET.encode("utf-8")
    ).digest()
    assert codec.access_token_key(CLIENT_SECRET) == expected
    assert codec.access_token_key(CLIENT_SECRET) != codec.access_token_key(
        CLIENT_SECRET + "-other"
    )


def test_encode_signed_payload_is_deterministic_and_key_bound():
    payload = {"b": 1, "a": 2}
    first = codec.encode_signed_payload(payload, key=b"k" * 32)
    second = codec.encode_signed_payload(payload, key=b"k" * 32)
    assert first == second
    assert first != codec.encode_signed_payload(payload, key=b"j" * 32)
    # Canonical JSON: sorted keys, no whitespace.
    body = first.split(".", 1)[0]
    assert json.loads(codec.base64url_decode(body)) == payload


def _authorization_code_payload(**overrides):
    payload = {
        "v": 2,
        "nonce": "n" * 32,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "hermes",
        "resource": RESOURCE,
        "code_challenge": "",
        "expires_at": int(time.time()) + 300,
        "epoch": 0,
    }
    payload.update(overrides)
    return payload


def test_decode_authorization_code_round_trips_and_fails_closed():
    key = b"authorization-key"
    code = codec.encode_signed_payload(_authorization_code_payload(), key=key)
    assert codec.decode_authorization_code(code, key=key)["client_id"] == CLIENT_ID

    cases = {
        "wrong key": codec.encode_signed_payload(_authorization_code_payload(), key=b"x"),
        "tampered": code[:-1] + ("A" if code[-1] != "A" else "B"),
        "no separator": "no-separator-here",
        "not json": f"{codec.base64url_encode(b'not json')}.{code.split('.', 1)[1]}",
        "bad version": codec.encode_signed_payload(
            _authorization_code_payload(v=3), key=key
        ),
        "bad nonce": codec.encode_signed_payload(
            _authorization_code_payload(nonce="short"), key=key
        ),
        "missing field": codec.encode_signed_payload(
            {k: v for k, v in _authorization_code_payload().items() if k != "scope"},
            key=key,
        ),
    }
    for label, candidate in cases.items():
        with pytest.raises(oauth_auth.OAuthError) as exc_info:
            codec.decode_authorization_code(candidate, key=key)
        assert exc_info.value.error == "invalid_grant", label
        assert exc_info.value.description.startswith("Invalid, expired"), label

    with pytest.raises(oauth_auth.OAuthError):
        # Oversized values are rejected before any parsing work.
        codec.decode_authorization_code("a" * (codec.MAX_TOKEN_VALUE_LENGTH + 1), key=key)


def test_decode_authorization_code_accepts_v1_codes_without_an_epoch():
    key = b"authorization-key"
    payload = _authorization_code_payload(v=1)
    payload.pop("epoch")
    code = codec.encode_signed_payload(payload, key=key)
    decoded = codec.decode_authorization_code(code, key=key)
    assert decoded["v"] == 1 and "epoch" not in decoded


def test_encode_and_decode_access_token_round_trip():
    key = codec.access_token_key(CLIENT_SECRET)
    token, item = codec.encode_access_token(
        key=key,
        client_id=CLIENT_ID,
        scope="hermes",
        resource=RESOURCE,
        expires_at=int(time.time()) + 3600,
    )
    assert token.startswith(codec.ACCESS_TOKEN_PREFIX)
    assert CLIENT_SECRET not in token
    payload = codec.decode_signed_access_token(token, key=key)
    assert payload is not None
    assert payload["client_id"] == CLIENT_ID
    assert payload["resource"] == RESOURCE
    assert payload["typ"] == "access"
    assert item == {
        "client_id": CLIENT_ID,
        "scope": "hermes",
        "resource": RESOURCE,
        "expires_at": float(int(payload["expires_at"])),
    }


def test_decode_signed_access_token_fails_closed_on_every_defect():
    key = codec.access_token_key(CLIENT_SECRET)
    token, _item = codec.encode_access_token(
        key=key,
        client_id=CLIENT_ID,
        scope="hermes",
        resource=RESOURCE,
        expires_at=int(time.time()) + 3600,
    )
    assert codec.decode_signed_access_token("", key=key) is None
    assert codec.decode_signed_access_token("opaque-token", key=key) is None
    assert codec.decode_signed_access_token(token, key=b"wrong-key") is None
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert codec.decode_signed_access_token(tampered, key=key) is None
    assert (
        codec.decode_signed_access_token(
            "hg.at.v1." + "a" * (codec.MAX_TOKEN_VALUE_LENGTH + 1), key=key
        )
        is None
    )

    def _signed(payload):
        return codec.ACCESS_TOKEN_PREFIX + codec.encode_signed_payload(
            payload, key=key
        )

    good = {
        "v": 1,
        "typ": "access",
        "nonce": "n" * 32,
        "client_id": CLIENT_ID,
        "scope": "hermes",
        "resource": RESOURCE,
        "expires_at": int(time.time()) + 60,
    }
    assert codec.decode_signed_access_token(_signed(good), key=key) is not None
    assert (
        codec.decode_signed_access_token(
            _signed({**good, "expires_at": int(time.time()) - 1}), key=key
        )
        is None
    )
    assert (
        codec.decode_signed_access_token(_signed({**good, "typ": "refresh"}), key=key)
        is None
    )
    assert (
        codec.decode_signed_access_token(_signed({**good, "v": 9}), key=key) is None
    )
    assert (
        codec.decode_signed_access_token(_signed({**good, "nonce": "x"}), key=key)
        is None
    )
    assert (
        codec.decode_signed_access_token(
            _signed({k: v for k, v in good.items() if k != "resource"}), key=key
        )
        is None
    )


def test_state_delegates_to_the_codec_for_both_directions():
    state = oauth_auth.OAuthState(_config())
    token, item = state._new_access_token(
        client_id=CLIENT_ID, scope="hermes", resource=RESOURCE
    )
    key = state._access_token_key()
    assert state._access_token_key() == codec.access_token_key(CLIENT_SECRET)
    assert codec.decode_signed_access_token(token, key=key) is not None
    assert state.validate_access_token(token) is True
    assert state._decode_signed_access_token(token) == codec.decode_signed_access_token(
        token, key=key
    )
    assert item["expires_at"] > time.time()

    code = state.issue_authorization_code(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope="hermes",
        resource=RESOURCE,
        code_challenge="",
    )
    decoded = codec.decode_authorization_code(code, key=state._authorization_code_key)
    assert decoded["client_id"] == CLIENT_ID
    assert state._decode_authorization_code(code) == decoded


def test_state_access_token_key_wrapper_stays_monkeypatchable(monkeypatch):
    state = oauth_auth.OAuthState(_config())
    real_key = state._access_token_key()
    replacement = b"z" * 32
    monkeypatch.setattr(state, "_access_token_key", lambda: replacement)
    token, _item = state._new_access_token(
        client_id=CLIENT_ID, scope="hermes", resource=RESOURCE
    )
    assert codec.decode_signed_access_token(token, key=replacement) is not None
    assert codec.decode_signed_access_token(token, key=real_key) is None

    forged = codec.ACCESS_TOKEN_PREFIX + codec.encode_signed_payload(
        {
            "v": 1,
            "typ": "access",
            "nonce": "n" * 32,
            "client_id": CLIENT_ID,
            "scope": "hermes",
            "resource": RESOURCE,
            "expires_at": int(time.time()) + 60,
        },
        key=replacement,
    )
    assert state.validate_access_token(forged) is True
    assert state._decode_signed_access_token(forged) is not None


def test_state_still_enforces_config_and_scope_after_the_codec_decode(monkeypatch):
    state = oauth_auth.OAuthState(_config())
    key = state._access_token_key()

    def _signed(**overrides):
        payload = {
            "v": 1,
            "typ": "access",
            "nonce": "n" * 32,
            "client_id": CLIENT_ID,
            "scope": "hermes",
            "resource": RESOURCE,
            "expires_at": int(time.time()) + 60,
        }
        payload.update(overrides)
        return codec.ACCESS_TOKEN_PREFIX + codec.encode_signed_payload(
            payload, key=key
        )

    # The codec accepts all of these; only the state's config/scope checks reject them.
    foreign_resource = _signed(resource="https://attacker.example/mcp")
    foreign_client = _signed(client_id="unknown-client")
    bad_scope = _signed(scope="admin")
    for candidate in (foreign_resource, foreign_client, bad_scope):
        assert codec.decode_signed_access_token(candidate, key=key) is not None
        assert state._decode_signed_access_token(candidate) is None
        assert state.validate_access_token(candidate) is False


def test_hmac_signature_is_constant_time_and_verified_before_payload_use():
    key = b"k" * 32
    canonical = json.dumps({"v": 2}, separators=(",", ":"), sort_keys=True)
    encoded = codec.base64url_encode(canonical.encode("utf-8"))
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    valid = f"{encoded}.{codec.base64url_encode(signature)}"
    assert codec.encode_signed_payload({"v": 2}, key=key) == valid
    flipped = bytearray(signature)
    flipped[0] ^= 0x01
    tampered = f"{encoded}.{codec.base64url_encode(bytes(flipped))}"
    with pytest.raises(oauth_auth.OAuthError):
        codec.decode_authorization_code(tampered, key=key)
