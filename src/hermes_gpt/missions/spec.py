"""Validation and normalization for durable Hermes mission specifications."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

MISSION_SPEC_SCHEMA = "hermes.mission-spec/v1"
MISSION_ID_RE = re.compile(r"^msn-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
WORKFLOW_REF_RE = re.compile(r"^sw-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_TITLE = 200
MAX_OBJECTIVE = 8_000
MAX_ACCEPTANCE = 32
MAX_ACCEPTANCE_ITEM = 500
MAX_CONTEXT = 64
MAX_SKILLS = 64

_ALLOWED_SPEC_KEYS = {
    "schema",
    "mission_id",
    "title",
    "objective",
    "owner_profile",
    "acceptance_criteria",
    "context_refs",
    "skills",
    "final_approval_required",
}
_ALLOWED_PATCH_KEYS = {
    "title",
    "objective",
    "owner_profile",
    "acceptance_criteria",
    "context_refs",
    "skills",
}
_ALLOWED_CONTEXT_KEYS = {"kind", "ref", "label", "sha256"}
_ALLOWED_SKILL_KEYS = {"name", "version", "ref", "sha256"}


def _closed(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


def _bounded_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
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


def _normalize_acceptance(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ACCEPTANCE:
        raise ValueError(f"acceptance_criteria must be a list with at most {MAX_ACCEPTANCE} items")
    return [_bounded_text(value, "acceptance_criteria item", MAX_ACCEPTANCE_ITEM, required=True) for value in raw]


def _normalize_context(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CONTEXT:
        raise ValueError(f"context_refs must be a list with at most {MAX_CONTEXT} items")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("context_refs items must be objects")
        _closed(item, _ALLOWED_CONTEXT_KEYS, "context ref")
        kind = _bounded_text(item.get("kind"), "context kind", 64, required=True)
        ref = _bounded_text(item.get("ref"), "context ref", 256, required=True)
        if not REF_RE.fullmatch(ref):
            raise ValueError("context ref contains unsupported characters")
        normalized = {"kind": kind, "ref": ref}
        label = _bounded_text(item.get("label"), "context label", 160)
        if label:
            normalized["label"] = label
        sha = _bounded_text(item.get("sha256"), "context sha256", 64)
        if sha:
            if not SHA_RE.fullmatch(sha):
                raise ValueError("context sha256 must be lowercase SHA-256")
            normalized["sha256"] = sha
        out.append(normalized)
    return out


def _normalize_skills(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SKILLS:
        raise ValueError(f"skills must be a list with at most {MAX_SKILLS} items")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("skills items must be objects")
        _closed(item, _ALLOWED_SKILL_KEYS, "skill")
        name = _bounded_text(item.get("name"), "skill name", 128, required=True)
        if name in seen:
            raise ValueError(f"duplicate skill {name!r}")
        seen.add(name)
        normalized = {"name": name}
        for key, limit in (("version", 64), ("ref", 256), ("sha256", 64)):
            value = _bounded_text(item.get(key), f"skill {key}", limit)
            if value:
                if key == "ref" and not REF_RE.fullmatch(value):
                    raise ValueError("skill ref contains unsupported characters")
                if key == "sha256" and not SHA_RE.fullmatch(value):
                    raise ValueError("skill sha256 must be lowercase SHA-256")
                normalized[key] = value
        out.append(normalized)
    return out


def _normalize_spec(raw: dict[str, Any], *, mission_id: str | None = None) -> dict[str, Any]:
    _closed(raw, _ALLOWED_SPEC_KEYS, "mission spec")
    schema = raw.get("schema", MISSION_SPEC_SCHEMA)
    if schema != MISSION_SPEC_SCHEMA:
        raise ValueError(f"mission spec schema must be {MISSION_SPEC_SCHEMA!r}")
    mid = mission_id or _bounded_text(raw.get("mission_id"), "mission_id", 68)
    if not mid:
        digest = hashlib.sha256(
            (str(raw.get("title") or "") + "\0" + str(raw.get("objective") or "") + "\0" + datetime.now(timezone.utc).isoformat()).encode()
        ).hexdigest()[:20]
        mid = f"msn-{digest}"
    if not MISSION_ID_RE.fullmatch(mid):
        raise ValueError("mission_id is invalid")
    final_approval = raw.get("final_approval_required", True)
    if not isinstance(final_approval, bool):
        raise TypeError("final_approval_required must be boolean")
    return {
        "schema": MISSION_SPEC_SCHEMA,
        "mission_id": mid,
        "title": _bounded_text(raw.get("title"), "title", MAX_TITLE, required=True),
        "objective": _bounded_text(raw.get("objective"), "objective", MAX_OBJECTIVE, required=True),
        "owner_profile": _bounded_text(raw.get("owner_profile") or "default", "owner_profile", 128, required=True),
        "acceptance_criteria": _normalize_acceptance(raw.get("acceptance_criteria")),
        "context_refs": _normalize_context(raw.get("context_refs")),
        "skills": _normalize_skills(raw.get("skills")),
        "final_approval_required": final_approval,
    }
