"""Validate Fleet authority manifests and authorize bounded work orders."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import a2a as fleet_a2a
from hermes_gpt.policy import authorization as op

AUTHORITY_MANIFEST_ENV = "HERMES_GPT_FLEET_AUTHORITY_MANIFEST"
_AGENT_RE = fleet_a2a._AGENT_RE
_TASK_ID_RE = fleet_a2a._TASK_ID_RE
_CARD_IDENTITY_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|"
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
_AUTH_CLASSES = frozenset({"none", "read_only", "reversible_write", "high_impact"})
_MAX_MANIFEST_BYTES = 64_000
_MAX_TEXT = 4_000
_MAX_ITEMS = 64

_BUILTIN_PROFILES: dict[str, frozenset[str]] = {
    "nous-girl": frozenset({"default"}),
    "rza": frozenset(
        {
            "default",
            "gza",
            "masta-killa",
            "inspectah-deck",
            "ghostface-killah",
            "method-man",
            "raekwon",
        }
    ),
    "gaming-4090": frozenset({"default"}),
}


@dataclass(frozen=True)
class AuthorityPeer:
    """The locally approved identity and authority ceiling for one peer."""

    name: str
    expected_host_role: str
    expected_card_identity: str
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allow_public_actions: bool = False


def _clean_text(
    value: Any,
    *,
    field: str,
    maximum: int = _MAX_TEXT,
    required: bool = True,
) -> str:
    if not isinstance(value, str):
        # Callers map all malformed input to one established validation error.
        raise ValueError(f"{field} must be a string")  # noqa: TRY004
    value = _CONTROL_RE.sub("", value).strip()
    if required and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its {maximum} byte limit")
    return value


def _string_list(value: Any, *, field: str, maximum: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a list with at most {maximum} items")
    return [_clean_text(item, field=f"{field} item", maximum=1_000) for item in value]


def _manifest_path(path: Path | None = None) -> Path:
    if path is None:
        configured = os.environ.get(AUTHORITY_MANIFEST_ENV)
        if configured:
            path = Path(configured)
        else:
            root = op.normalize_hermes_data_root(os.environ.get("HERMES_HOME"))
            path = (root or Path.home() / ".hermes") / "config" / "fleet-authority.json"
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("authority manifest path must be absolute")
    if op.is_denied_path(path):
        raise PermissionError(
            "authority manifest path is denied by the secret-path policy"
        )
    if path.is_symlink():
        raise PermissionError("authority manifest must not be a symbolic link")
    return path


def _load_authority(
    path: Path | None = None,
    *,
    builtin_profiles: dict[str, frozenset[str]] | None = None,
) -> dict[str, AuthorityPeer]:
    """Read bounded peer rules without mutating the built-in profile ceilings."""
    manifest_path = _manifest_path(path)
    data = manifest_path.read_bytes()
    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("authority manifest exceeds 64 KB")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authority manifest is not valid UTF-8 JSON") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) - {"version", "peers"}
        or raw.get("version") != 1
        or not isinstance(raw.get("peers"), list)
    ):
        raise ValueError("authority manifest schema is invalid")

    profile_ceilings = dict(
        builtin_profiles if builtin_profiles is not None else _BUILTIN_PROFILES
    )
    peers: dict[str, AuthorityPeer] = {}
    allowed_keys = {
        "name",
        "expected_host_role",
        "expected_card_identity",
        "allowed_profiles",
        "max_authorization",
        "allow_public_actions",
    }
    for item in raw["peers"]:
        if not isinstance(item, dict) or set(item) - allowed_keys:
            raise ValueError("authority peer schema is invalid")
        name = item.get("name")
        role = item.get("expected_host_role")
        identity = item.get("expected_card_identity")
        profiles = item.get("allowed_profiles")
        auth = item.get("max_authorization", "read_only")
        if not isinstance(name, str) or not _AGENT_RE.fullmatch(name) or name in peers:
            raise ValueError("authority peer name is invalid or duplicated")
        if name not in profile_ceilings:
            # New peers start with access to the default profile only.
            profile_ceilings[name] = frozenset({"default"})
        if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
            raise ValueError("expected_host_role is invalid")
        if (
            not isinstance(identity, str)
            or identity != identity.strip()
            or not _CARD_IDENTITY_RE.fullmatch(identity)
        ):
            raise ValueError("expected_card_identity is invalid")
        if (
            not isinstance(profiles, list)
            or not profiles
            or len(profiles) > 16
            or any(
                not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile)
                for profile in profiles
            )
        ):
            raise ValueError("allowed_profiles is invalid")
        if not set(profiles) <= profile_ceilings[name]:
            raise PermissionError(
                "authority manifest exceeds the built-in profile ceiling"
            )
        if not isinstance(auth, str) or auth not in _AUTH_CLASSES:
            raise ValueError("max_authorization is invalid")
        allow_public = item.get("allow_public_actions", False)
        if not isinstance(allow_public, bool) or (name == "nous-girl" and allow_public):
            raise PermissionError("Nous Girl may not receive public actions")
        peers[name] = AuthorityPeer(
            name,
            role,
            identity,
            tuple(sorted(set(profiles))),
            auth,
            allow_public,
        )
    return peers


__all__ = [
    "AUTHORITY_MANIFEST_ENV",
    "_AUTH_CLASSES",
    "_BUILTIN_PROFILES",
    "_CARD_IDENTITY_RE",
    "_CONTROL_RE",
    "_MAX_ITEMS",
    "_MAX_MANIFEST_BYTES",
    "_MAX_TEXT",
    "_PROFILE_RE",
    "_ROLE_RE",
    "AuthorityPeer",
    "_clean_text",
    "_load_authority",
    "_manifest_path",
    "_string_list",
]
