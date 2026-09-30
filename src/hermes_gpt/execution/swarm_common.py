"""Shared error, audit, and redaction helpers for Swarm tools."""

from __future__ import annotations

import hashlib
from typing import Any

from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import swarm_model
from hermes_gpt.execution import swarm_store


def swarm_error(
    *,
    code: str,
    safe_message: str,
    suggested_action: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = op.make_error_envelope(
        layer="operator",
        code=code,
        safe_message=safe_message,
        suggested_action=suggested_action,
        trace_id=trace_id,
        extra=extra,
    )
    env["schema_version"] = swarm_model.SCHEMA_VERSION
    return env


def truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def prompt_meta(text: str | None) -> dict[str, Any]:
    """Return ``{prompt_len, prompt_sha256}`` for an objective (never text)."""
    if text is None:
        return {"prompt_len": 0, "prompt_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "prompt_len": len(data),
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
    }


def audit_call(
    *,
    tool: str,
    workflow_id: str,
    stage_id: str,
    dry_run: bool,
    success: bool,
    changed: bool,
    summary: str,
    owner: str = "",
    verdict: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a swarm call in the operator audit log (D-SW10).

    Never includes objective text; only workflow_id/stage/owner/verdict.
    """
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=bool(dry_run),
            success=bool(success),
            changed=bool(changed),
            summary=truncate(summary, 500),
            extra={
                "workflow_id": workflow_id,
                "stage_id": stage_id,
                "owner": owner,
                "verdict": verdict,
                **(extra or {}),
            },
        )
    except Exception:
        pass
    # v0.9 wake-up channel. Swarm JSON/audit state remains authoritative;
    # notification failure is non-fatal and never advances work.
    try:
        from hermes_gpt.workspace import live_events

        live_events.publish_event(
            topic="swarm",
            kind=tool,
            subject_type="workflow-stage" if stage_id else "workflow",
            subject_id=f"{workflow_id}:{stage_id}" if stage_id else workflow_id,
            source="swarm",
            payload={
                "workflow_id": workflow_id,
                "stage_id": stage_id,
                "owner": owner,
                "verdict": verdict,
                "success": bool(success),
                "changed": bool(changed),
                **(extra or {}),
            },
            hermes_root=swarm_store.default_hermes_root(),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return
