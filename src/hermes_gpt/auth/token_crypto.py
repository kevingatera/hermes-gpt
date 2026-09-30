"""Key management and AES-GCM helpers for Hermes OAuth token storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = 1

ENVELOPE_FILENAME = "hermes_gpt_tokens.json"

KEY_FILENAME = "hermes_gpt_token_key"

SECRETS_DIR = "secrets"

MASTER_KEY_ENV = "HERMES_GPT_TOKEN_MASTER_KEY"

SERVICE_NAME = "hermes-gpt"

USERNAME = "oauth-tokens"


class TokenStoreError(RuntimeError):
    pass


def _secrets_dir(hermes_root: Path) -> Path:
    return hermes_root / SECRETS_DIR


def envelope_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / ENVELOPE_FILENAME


def key_file_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / KEY_FILENAME


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _key_from_env() -> bytes | None:
    raw = os.environ.get(MASTER_KEY_ENV)
    if not raw:
        return None
    # Derive a 32-byte key from any env material (documented weakest path).
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _key_from_keyring() -> bytes | None:
    try:
        import keyring  # optional dependency

        raw = keyring.get_password(SERVICE_NAME, USERNAME)
    except Exception:  # noqa: BLE001 - keyring backend failures must fall back to the key file
        return None
    if not raw:
        return None
    try:
        return _unb64(raw)
    except ValueError:
        return None


def _store_key_in_keyring(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, USERNAME, _b64(key))
        return True
    except Exception:  # noqa: BLE001 - keyring backends expose provider-specific failures
        return False


def _key_from_file(hermes_root: Path) -> bytes | None:
    path = key_file_path(hermes_root)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) == 32:
        return raw
    try:
        return _unb64(raw.decode("ascii").strip())
    except ValueError:
        return None


def _write_key_file(hermes_root: Path, key: bytes) -> None:
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = key_file_path(hermes_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _rotate_active_key(hermes_root: Path) -> dict[str, Any]:
    """Rotate whichever key source is ACTIVE. Returns a structured result:
    ``{"outcome": "rotated"|"env_managed"|"failed", "source": str}``.

    - env key (HERMES_GPT_TOKEN_MASTER_KEY): cannot be rotated from here
      (operator-managed); outcome ``env_managed``.
    - keyring: overwrite the stored key with a fresh random key.
    - key file: delete it; the next _resolve_key generates a new one.
    Failures are reported as ``failed`` with the source, never silently
    conflated with intentional external key management.
    """
    env_key = _key_from_env()
    if env_key is not None:
        return {"outcome": "env_managed", "source": "env"}
    keyring_key = _key_from_keyring()
    if keyring_key is not None:
        fresh = secrets.token_bytes(32)
        if _store_key_in_keyring(fresh):
            return {"outcome": "rotated", "source": "keyring"}
        return {"outcome": "failed", "source": "keyring"}
    # key file (or nothing yet): rotate ATOMICALLY — generate and write the
    # new key to a temp file, then rename over the old one. A failure at any
    # point leaves the old key intact, so a reported rotation failure can
    # truthfully say the old key remains active.
    try:
        fresh = secrets.token_bytes(32)
        path = key_file_path(hermes_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".new")
        tmp.write_bytes(fresh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return {"outcome": "failed", "source": "keyfile"}
    return {"outcome": "rotated", "source": "keyfile"}


def _resolve_key(hermes_root: Path) -> tuple[bytes, str, str]:
    """Return (key, kid, source). Key precedence env → keyring → key file."""
    env_key = _key_from_env()
    if env_key is not None:
        return env_key, "env", "env"
    keyring_key = _key_from_keyring()
    if keyring_key is not None:
        return keyring_key, "keyring", "keyring"
    file_key = _key_from_file(hermes_root)
    if file_key is not None:
        return file_key, "keyfile", "keyfile"
    # First use: generate a key, prefer keyring, else key file (0600).
    generated = secrets.token_bytes(32)
    if _store_key_in_keyring(generated):
        return generated, "keyring", "keyring"
    _write_key_file(hermes_root, generated)
    return generated, "keyfile", "keyfile"


def load_envelope(hermes_root: Path) -> dict[str, Any] | None:
    """Read the envelope file if present. Returns None when absent."""
    path = envelope_path(hermes_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TokenStoreError("token envelope is corrupt or unreadable")
    if data.get("version") != ENVELOPE_VERSION:
        raise TokenStoreError("unsupported token envelope version")
    for field in ("kid", "ciphertext", "nonce"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise TokenStoreError(f"token envelope missing {field!r}")
    return data


def decrypt_envelope(envelope: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    """Decrypt an envelope to its plaintext token bundle."""
    key, _, _ = _resolve_key(hermes_root)
    try:
        nonce = _unb64(envelope["nonce"])
        ciphertext = _unb64(envelope["ciphertext"])
        if len(nonce) != 12:
            raise TokenStoreError("token envelope nonce must be 12 bytes")
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))
    except TokenStoreError:
        raise
    except Exception as exc:
        raise TokenStoreError(
            f"could not decrypt token envelope: {exc.__class__.__name__}"
        ) from exc


def _write_envelope(
    hermes_root: Path, kid: str, plaintext: dict[str, Any], key: bytes
) -> None:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        json.dumps(plaintext, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        None,
    )
    envelope = {
        "version": ENVELOPE_VERSION,
        "kid": kid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ciphertext": _b64(ciphertext),
        "nonce": _b64(nonce),
    }
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = envelope_path(hermes_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_tokens(hermes_root: Path, tokens: dict[str, Any]) -> dict[str, Any]:
    """Encrypt and persist a token bundle. Returns {kid, source, path}."""
    key, kid, source = _resolve_key(hermes_root)
    _write_envelope(hermes_root, kid, tokens, key)
    return {"kid": kid, "source": source, "path": str(envelope_path(hermes_root))}


def _token_key(kind: str, token_value: str) -> str:
    """Stable row key: opaque hash of the token value (never the value)."""
    digest = hashlib.sha256(f"{kind}\0{token_value}".encode()).hexdigest()
    return "sha256:" + digest


def issue_key(kind: str, token_value: str) -> str:
    """Row key for an access/refresh token value."""
    return _token_key(kind, token_value)


def _encrypt_record(key: bytes, record: dict[str, Any]) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(
        nonce,
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        None,
    )
    return nonce, ct


def _decrypt_record(key: bytes, nonce: bytes, ciphertext: bytes) -> dict[str, Any]:
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise TokenStoreError("token record plaintext is not an object")
    return data
