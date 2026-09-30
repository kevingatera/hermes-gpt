"""Shared policy, cache, envelope, audit, and output helpers for Mission Control."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.6-mc.1"

# When configured, this allowlist restricts read-only surfaces.
MISSION_ALLOWED_SURFACES_ENV = "HERMES_GPT_MISSION_ALLOWED_SURFACES"

# All mission surfaces (keys used in the allowlist and in the overview).
MISSION_SURFACES: tuple[str, ...] = (
    "overview",
    "health",
    "profiles",
    "fleet",
    "codex",
    "cron",
    "delegations",
    "failures",
    "approvals",
    "vault",
    "usage",
    "audit",
)

# §8.4 size caps (bytes). Overview tighter than per-surface tools.
OVERVIEW_CAP_BYTES = 64 * 1024
OVERVIEW_HARD_CAP_BYTES = 128 * 1024
SURFACE_CAP_BYTES = 256 * 1024
SURFACE_HARD_CAP_BYTES = 512 * 1024

# TTL (seconds) per surface, per design §8.2. None = never cache.
SURFACE_TTL: dict[str, int] = {
    "profiles": 15,
    "delegations": 15,
    "codex": 10,
    "failures": 10,
    "usage": 10,
    "vault": 10,
    "health": 5,
    "cron": 5,
    "fleet": 5,
    "approvals": 5,
    "audit": 5,
    "overview": 5,
}

# Bounded list defaults.
_MAX_ERRORS = 50
_MAX_KANBAN_RUNS = 100
_MAX_DELEGATIONS = 200
_MAX_JOBS = 100
_MAX_APPROVALS = 50
_MAX_AUDIT_RECORDS = 50
_ERROR_STRING_CAP = 500
_LOG_TAIL_BYTES = 64 * 1024

# A list shorter than this is never auto-truncated for the overview (keeps the
# top-N meaningful summary small).
_OVERVIEW_LIST_KEEP = 8


# ---------------------------------------------------------------------------
# Cache (in-process, ephemeral, post-redaction only)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(surface: str, hermes_root: Path | None) -> str:
    root = str(hermes_root or "")
    return f"{surface}::{root}"


def _cache_get(surface: str, hermes_root: Path | None) -> tuple[dict[str, Any], float] | None:
    """Return (cached_payload, age_ms) if a fresh entry exists, else None."""
    ttl = SURFACE_TTL.get(surface)
    if not ttl:
        return None
    entry = _cache.get(_cache_key(surface, hermes_root))
    if not entry:
        return None
    expires_at, payload = entry
    now = time.monotonic()
    if now < expires_at:
        return payload, int((now - (expires_at - ttl)) * 1000)
    _cache.pop(_cache_key(surface, hermes_root), None)
    return None


def _cache_set(surface: str, hermes_root: Path | None, payload: dict[str, Any]) -> None:
    ttl = SURFACE_TTL.get(surface)
    if not ttl:
        return
    _cache[_cache_key(surface, hermes_root)] = (time.monotonic() + ttl, payload)


def _cache_clear() -> None:
    """Clear the in-process cache (used by tests)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Hermes root resolution (mirrors server.py)
# ---------------------------------------------------------------------------


def _default_hermes_root() -> Path | None:
    """Return the default Hermes data root (not the agent source root)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def _resolve_root(hermes_root: Path | None) -> Path:
    """Resolve a concrete Hermes root (falls back to the default)."""
    return hermes_root or _default_hermes_root() or Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------


def _mission_envelope(
    *,
    tool: str,
    surface: str,
    data: dict[str, Any] | None = None,
    available: bool = True,
    unavailable_reason: str | None = None,
    counts: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    trace_id: str | None = None,
    served_from_cache: bool | None = None,
    age_ms: int | None = None,
) -> dict[str, Any]:
    """Build the standard mission envelope (design §6.1)."""
    env: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "tool": tool,
        "surface": surface,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "available": bool(available),
        "unavailable_reason": unavailable_reason,
        "counts": counts or {},
        "data": data or {},
        "warnings": warnings or [],
        "trace_id": trace_id or op.new_trace_id(),
    }
    if served_from_cache is not None:
        env["served_from_cache"] = served_from_cache
        env["age_ms"] = age_ms if age_ms is not None else 0
    return env


def _mission_error(
    *,
    tool: str,
    surface: str,
    code: str,
    safe_message: str,
    suggested_action: str = "Run hermes_operator_doctor for more details.",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build a mission error envelope (still bounded and success:false)."""
    tid = trace_id or op.new_trace_id()
    env = _mission_envelope(
        tool=tool,
        surface=surface,
        available=False,
        unavailable_reason=safe_message,
        counts={},
        data={},
        warnings=[],
        trace_id=tid,
    )
    env["success"] = False
    env["ok"] = False
    env["error"] = safe_message
    env["layer"] = "mission"
    env["code"] = code
    env["safe_message"] = safe_message
    env["suggested_action"] = suggested_action
    return env


def _mission_denied(*, tool: str, surface: str) -> dict[str, Any]:
    """Authorization denial for a surface not on the per-client allowlist."""
    tid = op.new_trace_id()
    env = _mission_envelope(
        tool=tool,
        surface=surface,
        available=False,
        unavailable_reason=(
            f"Surface {surface!r} is not on the allowlist "
            f"({MISSION_ALLOWED_SURFACES_ENV})."
        ),
        counts={},
        data={},
        warnings=[],
        trace_id=tid,
    )
    env["success"] = False
    env["ok"] = False
    env["error"] = f"Surface {surface!r} not allowed"
    env["layer"] = "mission"
    env["code"] = "AUTHZ_DENIED"
    env["safe_message"] = f"Surface {surface!r} is not allowed for this client."
    env["suggested_action"] = "Request access to this surface on the allowlist."
    return env


# ---------------------------------------------------------------------------
# Allowlist (deny-by-default)
# ---------------------------------------------------------------------------


def _allowed_surfaces() -> set[str]:
    """Return the set of surfaces this deployment permits.

    If unset, all read-only surfaces are allowed. A configured comma-separated
    list restricts access, and an empty value denies every surface.
    """
    raw = os.environ.get(MISSION_ALLOWED_SURFACES_ENV)
    if raw is None:
        return set(MISSION_SURFACES)
    allowed: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item in MISSION_SURFACES:
            allowed.add(item)
    return allowed


def _surface_allowed(surface: str) -> bool:
    return surface in _allowed_surfaces()


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def _prompt_meta(text: str | None) -> dict[str, Any]:
    """Return ``{prompt_len, prompt_sha256}`` for a prompt, never the text."""
    if text is None:
        return {"prompt_len": 0, "prompt_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "prompt_len": len(data),
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
    }


def _truncate(text: str | None, limit: int = _ERROR_STRING_CAP) -> str:
    """Truncate a string to ``limit`` chars with an ellipsis marker."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _sanitize_error(text: str | None) -> str:
    """Return a bounded, secret- and PII-stripped operational summary.

    This function is intentionally used only at the Mission Control view boundary
    for free-text fields (failures, audit, cron, and delegation summaries).  It
    is conservative: a false positive loses diagnostic detail, while a false
    negative can disclose third-party data to a trusted client.
    """
    value = op.redact_output(_truncate(text, _ERROR_STRING_CAP))
    # Contact details and handles are never useful for operational status.
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted-email]", value)
    value = re.sub(r"(?<!\w)(?:\+?\d[\d().\-\s]{6,}\d)(?!\w)", "[redacted-phone]", value)
    value = re.sub(r"(?<![\w@])@[A-Za-z0-9_]{1,32}\b", "[redacted-username]", value)
    # Explicit identity labels and common two-token personal-name patterns.
    value = re.sub(
        r"(?i)\b(name|contact|customer|client|user|owner|assignee)\s*[:=]\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b",
        lambda m: f"{m.group(1)}=[redacted-name]",
        value,
    )
    value = re.sub(r"\b(?:Mr|Mrs|Ms|Dr)\.\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", "[redacted-name]", value)
    value = re.sub(r"\b[A-Z][a-z]{1,30}\s+[A-Z][a-z]{1,30}\b", "[redacted-name]", value)
    return value


# ---------------------------------------------------------------------------
# Bounded output
# ---------------------------------------------------------------------------


def _bound_lists(value: Any, max_items: int) -> Any:
    """Recursively truncate lists to ``max_items``.

    For each truncated list inside a dict, add ``truncated: true`` and
    ``count_total: N`` to the containing dict. Returns (value, truncated_any).
    """
    truncated_any = False

    def walk(node: Any) -> Any:
        nonlocal truncated_any
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, sub in node.items():
                if isinstance(sub, list) and len(sub) > max_items:
                    total = len(sub)
                    out[key] = sub[:max_items]
                    out["truncated"] = True
                    out["count_total"] = total
                    truncated_any = True
                else:
                    out[key] = walk(sub)
            return out
        if isinstance(node, list):
            return [walk(sub) for sub in node]
        return node

    return walk(value), truncated_any


def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _bounded_json(
    payload: dict[str, Any],
    cap_bytes: int = SURFACE_CAP_BYTES,
    hard_cap_bytes: int = SURFACE_HARD_CAP_BYTES,
) -> str:
    """Serialize ``payload`` to JSON, enforcing a byte cap.

    Lists are truncated with ``truncated``/``count_total`` (design §8.4).
    If even the single-item form exceeds the hard cap, returns a bounded error
    envelope rather than emitting an unbounded body.
    """
    if _json_size(payload) <= cap_bytes:
        return json.dumps(payload, ensure_ascii=False, default=str, indent=2)

    # Try progressively smaller list bounds until it fits (or hit the hard cap).
    max_items = 100
    while max_items >= 1:
        bounded, truncated = _bound_lists(payload, max_items)
        if truncated:
            bounded["truncated"] = True
            bounded["count_total"] = True  # replaced below by real totals
        size = _json_size(bounded)
        if size <= hard_cap_bytes:
            return json.dumps(bounded, ensure_ascii=False, default=str, indent=2)
        max_items = max_items // 2 if max_items > 1 else 0

    # Still over the hard cap: emit a bounded error envelope.
    return json.dumps(
        _mission_error(
            tool=str(payload.get("tool", "hermes_mission_*")),
            surface=str(payload.get("surface", "overview")),
            code="OUTPUT_TOO_LARGE",
            safe_message="The surface exceeded the hard output cap even after truncation.",
            suggested_action="Request a narrower surface or use limit/offset.",
            trace_id=payload.get("trace_id"),
        ),
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Audit (design D10: every mission call is audited)
# ---------------------------------------------------------------------------


def _audit_call(*, tool: str, success: bool, summary: str, extra: dict[str, Any] | None = None) -> None:
    """Record a mission call in the operator audit log.

    Mission calls are structurally read-only (dry_run=True, no change).
    The audit record itself never contains raw bodies.
    """
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode="read_only",
            dry_run=True,
            success=bool(success),
            changed=False,
            summary=_truncate(summary, 500),
            extra=extra,
        )
    except Exception:  # noqa: BLE001, S110 - audit storage failures must not break a read.
        # Audit must never break the tool.
        pass


def _audited(
    *,
    tool: str,
    surface: str,
    fn: Callable[..., dict[str, Any]],
    hermes_root: Path | None,
    summary: str,
    allow_deny: bool = True,
    force_refresh: bool = False,
) -> str:
    """Run a surface builder with allowlist enforcement, caching, and audit."""
    tid = op.new_trace_id()
    if allow_deny and not _surface_allowed(surface):
        payload = _mission_denied(tool=tool, surface=surface)
        _audit_call(tool=tool, success=False, summary=f"denied:{surface}", extra={"trace_id": tid})
        return _bounded_json(payload)

    cached = None if force_refresh else _cache_get(surface, hermes_root)
    try:
        if cached is not None:
            payload, age_ms = cached
            payload = dict(payload)
            payload["served_from_cache"] = True
            payload["age_ms"] = age_ms
            payload["trace_id"] = tid
            _audit_call(tool=tool, success=True, summary=f"{summary} (cache)", extra={"trace_id": tid})
            return _bounded_json(payload)
        payload = fn(hermes_root=hermes_root, trace_id=tid)
    except Exception as exc:  # noqa: BLE001 - surfaces degrade gracefully
        payload = _mission_error(
            tool=tool,
            surface=surface,
            code="MISSION_SURFACE_ERROR",
            safe_message=op.redact_output(str(exc))[:300] or "Mission surface failed.",
            suggested_action="Check the underlying source and retry.",
            trace_id=tid,
        )

    _audit_call(tool=tool, success=bool(payload.get("success", True)), summary=summary, extra={"trace_id": tid})

    # Cache only post-redaction/bounded data; never cache raw or secret bodies.
    if payload.get("success", True) and payload.get("available", True):
        _cache_set(surface, hermes_root, dict(payload))

    return _bounded_json(payload)

