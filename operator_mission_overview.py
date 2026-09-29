"""Bounded, cached Mission Control overview composition."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_mission_common import (
    _OVERVIEW_LIST_KEEP,
    OVERVIEW_CAP_BYTES,
    OVERVIEW_HARD_CAP_BYTES,
    SCHEMA_VERSION,
    _audit_call,
    _bounded_json,
    _cache_get,
    _cache_set,
    _mission_denied,
    _resolve_root,
    _surface_allowed,
)
from operator_mission_operational import (
    hermes_mission_audit,
    hermes_mission_cron,
    hermes_mission_fleet,
    hermes_mission_health,
    hermes_mission_profiles,
)
from operator_mission_resources import (
    hermes_mission_codex,
    hermes_mission_usage,
    hermes_mission_vault,
)
from operator_mission_work import (
    hermes_mission_approvals,
    hermes_mission_delegations,
    hermes_mission_failures,
)


def hermes_mission_overview(
    hermes_root: Path | None = None,
    force_refresh: bool = False,
    trace_id: str | None = None,
) -> str:
    """Composite bounded overview: all surfaces summarized (S1)."""
    if not _surface_allowed("overview"):
        return _bounded_json(
            _mission_denied(tool="hermes_mission_overview", surface="overview")
        )

    tid = trace_id or op.new_trace_id()

    cached = None if force_refresh else _cache_get("overview", hermes_root)
    if cached is not None:
        payload, age_ms = cached
        payload = dict(payload)
        payload["served_from_cache"] = True
        payload["age_ms"] = age_ms
        payload["trace_id"] = tid
        _audit_call(
            tool="hermes_mission_overview",
            success=True,
            summary="overview (cache)",
            extra={"trace_id": tid},
        )
        return _bounded_json(
            payload,
            cap_bytes=OVERVIEW_CAP_BYTES,
            hard_cap_bytes=OVERVIEW_HARD_CAP_BYTES,
        )

    # Build the summary view from each surface (respecting allowlist).
    root = _resolve_root(hermes_root)

    def _section(builder, tool: str, surface: str) -> dict[str, Any]:
        if not _surface_allowed(surface):
            return {"available": False, "unavailable_reason": "surface not allowed"}
        try:
            env = builder(hermes_root=root, trace_id=tid)
            if not env.get("success", True):
                return {
                    "available": False,
                    "unavailable_reason": env.get("unavailable_reason")
                    or env.get("safe_message"),
                }
            # Summary form: keep it small for the overview.
            return {
                "available": env.get("available", True),
                "counts": env.get("counts", {}),
                "data": env.get("data", {}),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "unavailable_reason": op.redact_output(str(exc))[:200],
            }

    # Cheap surfaces.
    health = _section(hermes_mission_health, "hermes_mission_health", "health")
    cron = _section(hermes_mission_cron, "hermes_mission_cron", "cron")
    fleet = _section(hermes_mission_fleet, "hermes_mission_fleet", "fleet")
    audit = _section(hermes_mission_audit, "hermes_mission_audit", "audit")

    # Sensitive surfaces.
    profiles = _section(hermes_mission_profiles, "hermes_mission_profiles", "profiles")
    delegations = _section(
        hermes_mission_delegations, "hermes_mission_delegations", "delegations"
    )
    failures = _section(hermes_mission_failures, "hermes_mission_failures", "failures")
    approvals = _section(
        hermes_mission_approvals, "hermes_mission_approvals", "approvals"
    )

    # Conditional surfaces.
    codex = _section(hermes_mission_codex, "hermes_mission_codex", "codex")
    vault = _section(hermes_mission_vault, "hermes_mission_vault", "vault")
    usage = _section(hermes_mission_usage, "hermes_mission_usage", "usage")

    # Compact per-section summary (trim to small lists for the overview).
    def _compact(section: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"available": section.get("available", True)}
        if not section.get("available", True):
            out["unavailable_reason"] = section.get("unavailable_reason")
            return out
        data = section.get("data", {})
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and len(value) > _OVERVIEW_LIST_KEEP:
                    out[key] = value[:_OVERVIEW_LIST_KEEP]
                    out["truncated"] = True
                    out["count_total"] = len(value)
                else:
                    out[key] = value
        out["counts"] = section.get("counts", {})
        return out

    unavailable = [
        s
        for s, section in (
            ("health", health),
            ("profiles", profiles),
            ("fleet", fleet),
            ("codex", codex),
            ("cron", cron),
            ("delegations", delegations),
            ("failures", failures),
            ("approvals", approvals),
            ("vault", vault),
            ("usage", usage),
            ("audit", audit),
        )
        if not section.get("available", True)
    ]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_mission_overview",
        "surface": "overview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fleet_health": {
            "overall": (health.get("data") or {}).get("overall", "unknown")
        },
        "profiles": _compact(profiles),
        "fleet_agents": _compact(fleet),
        "codex": _compact(codex),
        "cron": _compact(cron),
        "delegations": _compact(delegations),
        "failures": _compact(failures),
        "pending_approvals": _compact(approvals),
        "vault": _compact(vault),
        "usage": _compact(usage),
        "audit": _compact(audit),
        "surfaces_unavailable": unavailable,
        "trace_id": tid,
    }

    _audit_call(
        tool="hermes_mission_overview",
        success=True,
        summary="overview",
        extra={"trace_id": tid},
    )
    _cache_set("overview", hermes_root, dict(payload))

    return _bounded_json(
        payload, cap_bytes=OVERVIEW_CAP_BYTES, hard_cap_bytes=OVERVIEW_HARD_CAP_BYTES
    )
