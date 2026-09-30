"""Canonicalize and authorize Fleet work orders before peer dispatch."""

from __future__ import annotations

import json
import re
from typing import Any

from hermes_gpt.fleet import authority as fleet_authority
from hermes_gpt.policy import authorization as op

AuthorityPeer = fleet_authority.AuthorityPeer
_AGENT_RE = fleet_authority._AGENT_RE
_TASK_ID_RE = fleet_authority._TASK_ID_RE
_PROFILE_RE = fleet_authority._PROFILE_RE
_AUTH_CLASSES = fleet_authority._AUTH_CLASSES
_clean_text = fleet_authority._clean_text
_string_list = fleet_authority._string_list

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
    "_CHILD_MCP_ACTION_RE",
    "_COMMAND_PREFIX_RE",
    "_FLEET_POLICY_ACTION_RE",
    "_NEGATED_ACTION_RE",
    "_PUBLIC_ACTION_RE",
    "_SECRET_RE",
    "_VAULT_RE",
    "_authorization",
    "_authorize_order",
    "_canonical_work_order",
    "_requests_affirmative_action",
    "_requests_child_mcp_inheritance",
    "_requests_fleet_policy_change",
    "_requests_public_action",
    "_requests_raw_secret",
    "_requests_vault_policy_change",
    "_work_order_text_fields",
]
