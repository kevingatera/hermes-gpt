"""Validate Fleet authority manifests and authorize bounded work orders."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_fleet_a2a as fleet_a2a
import operator_policy as op

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

_PUBLIC_ACTION_RE = re.compile(
    r"\b(?:"
    r"publish\b"
    r"|post\b(?=.{0,80}\b(?:online|publicly|on\s+(?:x|twitter|facebook|instagram|linkedin))\b)"
    r"|deploy\b(?=.{0,80}\b(?:publicly|public|website|site)\b)"
    r"|send\b(?=.{0,80}\b(?:an?\s+|this\s+|the\s+)?emails?\b)"
    r"|release\b(?=.{0,80}\bpublicly\b)"
    r"|make\b(?=.{0,40}\bpublic\b)"
    r")",
    re.IGNORECASE,
)
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|without)\s+"
    r"(?:\w+\s+){0,3}$",
    re.IGNORECASE,
)
_COMMAND_PREFIX_RE = re.compile(
    r"(?:^|[.!?;:]\s*|(?:\b(?:and|then)\b)\s+)"
    r"(?:please\s+|(?:can|could|would|will)\s+you\s+|"
    r"you\s+(?:must|should|need\s+to)\s+)?$",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b(raw|reveal|return|print|show|read|extract|dump|expose)\b.{0,40}"
    r"\b(secret|token|password|credential|api[_ -]?key|private key|"
    r"environment values?)s?\b",
    re.IGNORECASE | re.DOTALL,
)
_VAULT_RE = re.compile(
    r"\b(vault|credential store)\b.{0,40}\b(policy|policies|edit|write|change|update)\b"
    r"|\b(edit|write|change|update)\b.{0,40}\b(vault|credential store)\b",
    re.IGNORECASE | re.DOTALL,
)
_FLEET_POLICY_ACTION_RE = re.compile(
    r"\b(?:edit|write|change|update|modify|replace|delete|create)\b.{0,80}"
    r"\b(?:fleet|authority)\s+(?:policy|manifest|rules?)\b"
    r"|\b(?:fleet|authority)\s+(?:policy|manifest|rules?)\b.{0,80}"
    r"\b(?:edit|write|change|update|modify|replace|delete|create)\b",
    re.IGNORECASE | re.DOTALL,
)
_CHILD_MCP_ACTION_RE = re.compile(
    r"\b(?:enable|allow|turn\s+on|set)\b.{0,80}"
    r"\b(?:inherit_mcp_toolsets|(?:child|subagent|delegate)\b.{0,40}"
    r"\b(?:mcp|toolsets?|inherit(?:ance)?))"
    r"|\binherit_mcp_toolsets\b.{0,40}\b(?:true|on|enabled)\b",
    re.IGNORECASE | re.DOTALL,
)


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


def _authorization(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"class": value}
    allowed_fields = {"class", "approved", "approved_by", "approval_reference"}
    if not isinstance(value, dict) or set(value) - allowed_fields:
        raise ValueError("authorization metadata has invalid fields")
    auth_class = value.get("class")
    if not isinstance(auth_class, str) or auth_class not in _AUTH_CLASSES:
        raise ValueError("authorization class is invalid")
    approved = value.get("approved", False)
    if not isinstance(approved, bool):
        # Keep malformed authorization on the dispatch validation path.
        raise ValueError("authorization.approved must be boolean")  # noqa: TRY004
    result: dict[str, Any] = {"class": auth_class, "approved": approved}
    for key in ("approved_by", "approval_reference"):
        if key in value:
            result[key] = _clean_text(
                value[key], field=f"authorization.{key}", maximum=128
            )
    if auth_class == "high_impact" and (
        not approved
        or "approved_by" not in result
        or "approval_reference" not in result
    ):
        raise PermissionError("high-impact work requires explicit approval metadata")
    return result


def _requests_affirmative_action(objective: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(objective):
        prefix = objective[max(0, match.start() - 48) : match.start()]
        if _COMMAND_PREFIX_RE.search(prefix) and not _NEGATED_ACTION_RE.search(prefix):
            return True
    return False


def _requests_public_action(objective: str) -> bool:
    """Detect affirmative public-action commands in the work-order objective."""
    return _requests_affirmative_action(objective, _PUBLIC_ACTION_RE)


def _requests_fleet_policy_change(objective: str) -> bool:
    return _requests_affirmative_action(objective, _FLEET_POLICY_ACTION_RE)


def _requests_child_mcp_inheritance(objective: str) -> bool:
    return _requests_affirmative_action(objective, _CHILD_MCP_ACTION_RE)


def _work_order_text_fields(envelope: dict[str, Any]) -> list[str]:
    values = [envelope["objective"]]
    for field in ("inputs", "constraints", "acceptance_checks", "deliverables"):
        values.extend(envelope[field])
    return values


def _requests_raw_secret(envelope: dict[str, Any]) -> bool:
    return any(
        _requests_affirmative_action(text, _SECRET_RE)
        for text in _work_order_text_fields(envelope)
    )


def _requests_vault_policy_change(envelope: dict[str, Any]) -> bool:
    return any(
        _requests_affirmative_action(text, _VAULT_RE)
        for text in _work_order_text_fields(envelope)
    )


def _canonical_work_order(
    *,
    agent: Any,
    task_id: Any,
    target_profile: Any,
    objective: Any,
    workspace: Any,
    inputs: Any,
    constraints: Any,
    acceptance_checks: Any,
    deliverables: Any,
    authorization: Any,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(agent, str) or not _AGENT_RE.fullmatch(agent):
        raise ValueError("agent is invalid")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    if not isinstance(target_profile, str) or not _PROFILE_RE.fullmatch(target_profile):
        raise ValueError("target_profile is invalid")
    objective_text = _clean_text(objective, field="objective", maximum=8_000)
    workspace_text = _clean_text(workspace, field="workspace", maximum=1_000)
    if op.is_denied_path(workspace_text):
        raise PermissionError("workspace is denied by the secret-path policy")
    envelope = {
        "schema": "hermes.fleet.work-order/v1",
        "agent": agent,
        "task_id": task_id,
        "target_profile": target_profile,
        "objective": objective_text,
        "workspace": workspace_text,
        "inputs": _string_list(inputs, field="inputs"),
        "constraints": _string_list(constraints, field="constraints"),
        "acceptance_checks": _string_list(acceptance_checks, field="acceptance_checks"),
        "deliverables": _string_list(deliverables, field="deliverables"),
        "authorization": _authorization(authorization),
    }
    canonical = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(canonical.encode("utf-8")) > 32_000:
        raise ValueError("canonical work order exceeds 32 KB")
    return canonical, envelope


def _authorize_order(
    peer: AuthorityPeer, envelope: dict[str, Any], canonical: str
) -> None:
    if envelope["target_profile"] not in peer.allowed_profiles:
        raise PermissionError("target profile is not authorized for this peer")
    ranks = {"none": 0, "read_only": 1, "reversible_write": 2, "high_impact": 3}
    if ranks[envelope["authorization"]["class"]] > ranks[peer.max_authorization]:
        raise PermissionError("request exceeds peer role authority")
    if _requests_raw_secret(envelope):
        raise PermissionError("raw-secret requests are forbidden")
    if _requests_vault_policy_change(envelope):
        raise PermissionError("Vault-policy edits by peers are forbidden")
    if _requests_fleet_policy_change(envelope["objective"]):
        raise PermissionError("fleet-policy edits by peers are forbidden")
    if _requests_child_mcp_inheritance(envelope["objective"]):
        raise PermissionError("child MCP inheritance is forbidden")
    if _requests_public_action(envelope["objective"]) and (
        peer.name == "nous-girl" or not peer.allow_public_actions
    ):
        raise PermissionError("public actions are not authorized for this peer")


__all__ = [
    "AUTHORITY_MANIFEST_ENV",
    "_AUTH_CLASSES",
    "_BUILTIN_PROFILES",
    "_CARD_IDENTITY_RE",
    "_CHILD_MCP_ACTION_RE",
    "_COMMAND_PREFIX_RE",
    "_CONTROL_RE",
    "_FLEET_POLICY_ACTION_RE",
    "_MAX_ITEMS",
    "_MAX_MANIFEST_BYTES",
    "_MAX_TEXT",
    "_NEGATED_ACTION_RE",
    "_PROFILE_RE",
    "_PUBLIC_ACTION_RE",
    "_ROLE_RE",
    "_SECRET_RE",
    "_VAULT_RE",
    "AuthorityPeer",
    "_authorization",
    "_authorize_order",
    "_canonical_work_order",
    "_clean_text",
    "_load_authority",
    "_manifest_path",
    "_requests_affirmative_action",
    "_requests_child_mcp_inheritance",
    "_requests_fleet_policy_change",
    "_requests_public_action",
    "_requests_raw_secret",
    "_requests_vault_policy_change",
    "_string_list",
    "_work_order_text_fields",
]
