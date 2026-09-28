"""Shared JSON validation and transport checks for Fabric peers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import urllib.parse
from typing import Any

_MAX_BODY = 128 * 1024
_MAX_STRING = 8_000
_MAX_ITEMS = 64
_MAX_DEPTH = 8
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_NODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRETISH_KEY_RE = re.compile(
    r"(?:secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)",
    re.IGNORECASE,
)
_URLISH_KEY_RE = re.compile(r"(?:url|uri|endpoint|host|hostname|proxy)", re.IGNORECASE)


class FabricError(RuntimeError):
    def __init__(self, code: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


class _DuplicateKey(ValueError):
    pass


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(f"duplicate JSON field {key!r}")
        out[key] = value
    return out


def strict_json_loads(raw: str | bytes, *, maximum: int = _MAX_BODY) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > maximum:
            raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "JSON payload exceeds the bounded limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FabricError("FABRIC_INVALID_JSON", "payload is not valid UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > maximum:
            raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "JSON payload exceeds the bounded limit")
        text = raw
    else:
        raise FabricError("FABRIC_INVALID_JSON", "payload must be UTF-8 JSON")
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs)
    except _DuplicateKey as exc:
        raise FabricError("FABRIC_AMBIGUOUS_JSON", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise FabricError("FABRIC_INVALID_JSON", "payload is not valid JSON") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _closed(
    obj: Any,
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    name: str,
) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} must be an object")
    keys = set(obj)
    if required - keys:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} is missing required fields")
    if keys - required - optional:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} contains unknown fields")
    return obj


def _bounded_string(
    value: Any,
    *,
    field: str,
    maximum: int = _MAX_STRING,
    pattern: re.Pattern[str] | None = None,
    required: bool = True,
) -> str:
    if not isinstance(value, str) or (required and not value) or len(value.encode("utf-8")) > maximum:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains control characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} has an invalid format")
    return value


def _bounded_strings(
    value: Any,
    *,
    field: str,
    maximum: int = _MAX_ITEMS,
    item_max: int = 2_000,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} must be a bounded list")
    return [_bounded_string(item, field=f"{field} item", maximum=item_max) for item in value]


def _bounded_json(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} exceeds nesting depth")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_STRING:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains an oversized string")
        if "://" in value:
            raise FabricError(
                "FABRIC_CALLER_NETWORK_TARGET",
                f"{field} may not contain caller-supplied URLs",
            )
        return value
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains too many items")
        return [_bounded_json(item, field=field, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains too many keys")
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} has an invalid key")
            if _SECRETISH_KEY_RE.search(key):
                raise FabricError(
                    "FABRIC_CALLER_CREDENTIAL",
                    f"{field} may not contain credential fields",
                )
            if _URLISH_KEY_RE.search(key):
                raise FabricError(
                    "FABRIC_CALLER_NETWORK_TARGET",
                    f"{field} may not contain network target fields",
                )
            out[key] = _bounded_json(item, field=f"{field}.{key}", depth=depth + 1)
        return out
    raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains a non-JSON value")


def _is_loopback_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_transport(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FabricError("FABRIC_TRANSPORT_INVALID", "configured Fabric peer URL is invalid")
    if parsed.username or parsed.password:
        raise FabricError("FABRIC_TRANSPORT_INVALID", "Fabric peer URL may not contain credentials")
    if parsed.scheme != "https" and not _is_loopback_url(url):
        raise FabricError("FABRIC_TRANSPORT_INSECURE", "non-loopback verified Fabric requires HTTPS")


__all__ = ["FabricError", "canonical_json", "sha256_json", "strict_json_loads"]
