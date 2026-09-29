"""Bounded placement inputs, schema constants, and manifest targets."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_capability_manifest as cm
import operator_mission_runtime as mission
import operator_policy as op

SCHEMA_VERSION = "0.9-placement.1"

PLACEMENT_SCHEMA = "hermes.placement/v1"

DECISION_SCHEMA = "hermes.placement-decision/v1"

MISSION_ID_RE = mission.MISSION_ID_RE

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

SHA_RE = re.compile(r"^[0-9a-f]{64}$")

AUTH_ORDER = ("read_only", "reversible_write", "high_impact")

AUTH_RANK = {name: i for i, name in enumerate(AUTH_ORDER)}

AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")

EXECUTOR_KINDS = ("profile", "fabric_node")

WEIGHTS: dict[str, float] = {
    "capability_fit": 0.30,
    "authorization_match": 0.20,
    "affinity": 0.15,
    "load_headroom": 0.15,
    "health": 0.10,
    "cost_priority": 0.10,
}

CLASS_ASSIGNED = "assigned"

CLASS_NO_TARGET = "no_capable_target"

CLASS_HUMAN = "human_approval"

CLASSES = (CLASS_ASSIGNED, CLASS_NO_TARGET, CLASS_HUMAN)

MAX_SKILLS = 32

MAX_FEATURES = 64

MAX_TARGETS = 256

MAX_STRING = 128

_MISSION_MAX = 68

_NODE_MAX = 64

_PII_STRIP = __import__("re").compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(text: Any, limit: int = MAX_STRING) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _clean_str_list(
    value: Any, *, field: str, maximum_items: int, item_max: int
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{field} must be a list (<= {maximum_items})")
    out: list[str] = []
    for item in value:
        item = _clean_text(item, field=field, maximum=item_max, required=True)
        if item not in out:
            out.append(item)
    return out


def _normalize_auth_class(value: Any) -> str:
    klass = str(value or "").strip().lower()
    if klass not in AUTH_CLASSES:
        raise ValueError(f"authorization_class must be one of {list(AUTH_CLASSES)}")
    return klass


def _stance(requirement: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical, bounded ``requirement`` for a work unit.

    Accepts a plan-node ``capability_req`` (profile / skills /
    authorization_class) plus optional ``features`` / ``workspace`` /
    ``backends`` and the node's bounded ``budget`` / ``kind`` / ``owner``.
    """
    profile = _clean_text(
        requirement.get("profile"), field="profile", maximum=64, required=True
    )
    if profile not in ("owner", "tony"):
        op.validate_profile_name(profile)
    skills = _clean_str_list(
        requirement.get("skills"),
        field="skills",
        maximum_items=MAX_SKILLS,
        item_max=128,
    )
    auth_class = _normalize_auth_class(
        requirement.get("authorization_class", "reversible_write")
    )
    features = _clean_str_list(
        requirement.get("features"),
        field="features",
        maximum_items=MAX_FEATURES,
        item_max=128,
    )
    workspace = _clean_text(
        requirement.get("workspace"), field="workspace", maximum=128
    )
    backends = _clean_str_list(
        requirement.get("backends"), field="backends", maximum_items=64, item_max=64
    )
    kind = _clean_text(requirement.get("kind"), field="kind", maximum=16) or "single"
    owner = _clean_text(requirement.get("owner"), field="owner", maximum=64)
    raw_budget = requirement.get("budget") or {}
    if not isinstance(raw_budget, dict):
        raise TypeError("budget must be an object")
    minutes = int(raw_budget.get("est_minutes", 0) or 0)
    tokens = int(raw_budget.get("est_tokens", 0) or 0)
    budget_req = {
        "est_minutes": max(0, min(minutes, 100_000)),
        "est_tokens": max(0, min(tokens, 100_000_000)),
    }
    stance = {
        "profile": _sanitize(profile, MAX_STRING),
        "skills": skills,
        "authorization_class": auth_class,
        "features": features,
        "workspace": workspace,
        "backends": backends,
        "kind": kind,
        "owner": _sanitize(owner, MAX_STRING),
        "budget": budget_req,
    }
    if op.redact_output(json.dumps(stance)) != json.dumps(stance):
        raise PermissionError(
            "placement requirement contains secret-like durable values"
        )
    return stance


def _target_from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Normalize one capability-manifest entity into a placement target.

    Fields that are not available for a kind are carried as neutral defaults so
    filters/scores degrade deterministically (and mark ``*_unavailable`` flags)
    rather than failing open or breaking determinism.
    """
    kind = entity.get("entity_kind", "")
    name = _sanitize(entity.get("name", ""), MAX_STRING)
    if kind == "fabric_node":
        enabled = bool(entity.get("enabled"))
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": enabled,
            "reachable": enabled,
            "identity_configured": enabled,
            "authorization_ceiling": _sanitize(
                entity.get("authorization_ceiling", ""), 64
            ),
            "allowed_profiles": _clean_str_list(
                entity.get("allowed_profiles"),
                field="allowed_profiles",
                maximum_items=64,
                item_max=128,
            ),
            "features": _clean_str_list(
                entity.get("features"), field="features", maximum_items=64, item_max=128
            ),
            "workspaces": _clean_str_list(
                entity.get("workspaces", []),
                field="workspaces",
                maximum_items=64,
                item_max=128,
            ),
            "backends": _clean_str_list(
                entity.get("backends", []),
                field="backends",
                maximum_items=64,
                item_max=64,
            ),
            "skills": [],
            "model": "",
            "provider": "",
            "host_role": "",
            "allow_public_actions": False,
        }
    if kind == "fleet_peer":
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(entity.get("identity_configured")),
            "identity_configured": bool(entity.get("identity_configured")),
            "authorization_ceiling": _sanitize(
                entity.get("authorization_ceiling", ""), 64
            ),
            "allowed_profiles": _clean_str_list(
                entity.get("allowed_profiles"),
                field="allowed_profiles",
                maximum_items=64,
                item_max=128,
            ),
            "features": [],
            "workspaces": [],
            "backends": [],
            "skills": [],
            "model": "",
            "provider": "",
            "host_role": _sanitize(entity.get("host_role", ""), MAX_STRING),
            "allow_public_actions": bool(entity.get("allow_public_actions")),
        }
    if kind == "profile":
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(
                entity.get("model") or entity.get("skills") or entity.get("skill_count")
            ),
            "identity_configured": True,
            "authorization_ceiling": "",
            "allowed_profiles": [name],
            "features": [],
            "workspaces": ["host"],
            "backends": ["local"],
            "skills": _clean_str_list(
                entity.get("skills", []),
                field="skills",
                maximum_items=MAX_SKILLS,
                item_max=128,
            ),
            "model": _sanitize(entity.get("model", ""), MAX_STRING),
            "provider": _sanitize(entity.get("provider", ""), MAX_STRING),
            "host_role": "worker",
            "allow_public_actions": False,
        }
    if kind == "provider":
        profiles = _clean_str_list(
            entity.get("profiles", []), field="profiles", maximum_items=64, item_max=128
        )
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(profiles),
            "identity_configured": bool(profiles),
            "authorization_ceiling": "",
            "allowed_profiles": profiles,
            "features": [],
            "workspaces": [],
            "backends": ["remote"],
            "skills": [],
            "model": "",
            "provider": name,
            "host_role": "provider",
            "allow_public_actions": False,
        }
    raise ValueError(f"unknown manifest entity_kind {kind!r}")


def load_manifest_targets(
    hermes_root: Path | None,
    *,
    source: str = "",
    limit: int = MAX_TARGETS,
) -> list[dict[str, Any]]:
    """Read the derived capability manifest and normalize to placement targets.

    Read-only. Respects the manifest's per-source allowlist env. Returns
    targets sorted by ``entity_id`` for determinism.
    """
    root = cm._resolve_root(hermes_root)
    readers = {
        "fabric": cm._read_fabric_entities,
        "fleet": cm._read_fleet_entities,
        "profile": cm._read_profile_entities,
        "provider": cm._read_provider_entities,
    }
    sources = [source] if source else list(readers.keys())
    if source and source not in readers:
        raise ValueError(f"unknown manifest source {source!r}")
    targets: list[dict[str, Any]] = []
    for src in sources:
        if not cm._source_allowed(src):
            continue
        for ent in readers[src](root):
            try:
                targets.append(_target_from_entity(ent))
            except ValueError:
                continue
            if len(targets) >= MAX_TARGETS:
                break
        if len(targets) >= MAX_TARGETS:
            break
    return sorted(targets, key=lambda t: t["entity_id"])
