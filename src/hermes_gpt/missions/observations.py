"""Observe Mission child records and derive safe parent lifecycle state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.missions import spec as mission_spec

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def workflow_state(root: Path, ref: str) -> str:
    """Read one workflow status while refusing paths outside its state directory."""
    if not mission_spec.WORKFLOW_REF_RE.fullmatch(ref):
        return "unknown"
    base = (root / "swarm-workflows").resolve()
    path = (base / f"{ref}.json").resolve()
    if path.parent != base or not path.is_file():
        return "unknown"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    status = raw.get("status")
    if not isinstance(status, str):
        return "unknown"
    return {
        "running": "running",
        "blocked": "blocked",
        "awaiting_approval": "blocked",
        "done": "succeeded",
    }.get(status, "unknown")


def delegation_state(
    root: Path, mission_id: str, attachment: dict[str, Any]
) -> tuple[str, bool, int | None]:
    """Re-observe one delegation from its authoritative durable lifecycle."""
    try:
        from hermes_gpt.execution import delegations

        payload = json.loads(
            delegations.hermes_delegation_reconcile(
                str(attachment["ref"]), apply=False, hermes_root=root
            )
        )
    except (
        ImportError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ):
        return "blocked", False, None
    if not isinstance(payload, dict):
        return "blocked", False, None
    row = payload.get("delegation")
    if not payload.get("success") or not isinstance(row, dict):
        return "blocked", False, None
    if str(row.get("mission_id") or "") != mission_id:
        return "blocked", False, None
    try:
        authority_version = int(row.get("authority_version") or 0)
    except (TypeError, ValueError, OverflowError):
        # Invalid authority cannot be used as a completion-guard snapshot.
        return "blocked", False, None
    if bool(row.get("cancellation_in_progress")) or (
        bool(row.get("cancel_requested"))
        and str(row.get("state") or "") != "cancelled"
    ):
        return "blocked", False, authority_version
    state = {
        "reserved": "pending",
        "queued": "pending",
        "running": "running",
        "reconciling": "blocked",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(str(row.get("state") or ""), "blocked")
    if state != "succeeded":
        return state, False, authority_version
    contract_sha = str(row.get("contract_sha256") or "")
    expected_evidence = f"contract:{contract_sha}" if contract_sha else ""
    verified = (
        bool(expected_evidence)
        and str(row.get("validation_verdict") or "") == "SATISFIED"
        and str(payload.get("evidence_ref") or "") == expected_evidence
    )
    return (
        ("succeeded", True, authority_version)
        if verified
        else ("blocked", False, authority_version)
    )


def observe_attachments(root: Path, mission: dict[str, Any]) -> list[dict[str, Any]]:
    """Refresh attached child state without persisting or assuming success."""
    observed: list[dict[str, Any]] = []
    for attachment in mission["attachments"]:
        state = str(attachment["state"])
        verified = bool(attachment.get("verified"))
        authority_version: int | None = None
        if attachment["kind"] == "workflow":
            state = workflow_state(root, str(attachment["ref"]))
            verified = state == "succeeded"
        elif attachment["kind"] == "delegation":
            state, verified, authority_version = delegation_state(
                root, str(mission["mission_id"]), attachment
            )
        elif state == "succeeded" and not verified:
            state = "blocked"

        item = {
            "kind": attachment["kind"],
            "ref": attachment["ref"],
            "state": state,
            "verified": verified,
        }
        if attachment["kind"] == "delegation" and authority_version is not None:
            item["authority_version"] = authority_version
        observed.append(item)
    return observed


def completion_guard(
    root: Path, mission_id: str, observed: list[dict[str, Any]]
):
    """Lock delegation authority while a parent completion is committed."""
    from hermes_gpt.execution import delegations

    snapshots = {
        str(item["ref"]): int(item["authority_version"])
        for item in observed
        if item["kind"] == "delegation" and "authority_version" in item
    }
    delegation_count = sum(1 for item in observed if item["kind"] == "delegation")
    if len(snapshots) != delegation_count:
        raise ValueError("delegation authority snapshot is incomplete")
    return delegations.mission_completion_guard(
        mission_id, snapshots, hermes_root=root
    )


def cancellation_guard(
    root: Path, mission_id: str, observed: list[dict[str, Any]]
):
    """Lock delegation authority while a parent cancellation is committed."""
    from hermes_gpt.execution import delegations

    snapshots = {
        str(item["ref"]): int(item["authority_version"])
        for item in observed
        if item["kind"] == "delegation" and "authority_version" in item
    }
    delegation_count = sum(1 for item in observed if item["kind"] == "delegation")
    if len(snapshots) != delegation_count:
        raise ValueError("delegation cancellation authority snapshot is incomplete")
    return delegations.mission_cancellation_guard(
        mission_id, snapshots, hermes_root=root
    )


def desired_status(mission: dict[str, Any], observed: list[dict[str, Any]]) -> str:
    """Derive parent status only from freshly observed child states."""
    current = str(mission["status"])
    if current in TERMINAL_STATUSES:
        return current
    states = {str(item["state"]) for item in observed}
    if "failed" in states:
        return "failed"
    if "blocked" in states or "unknown" in states:
        return "blocked"
    if "running" in states or "pending" in states:
        return "running"
    if observed and states <= {"succeeded", "cancelled"} and "succeeded" in states:
        if mission["final_approval_required"] and not mission["approval"].get(
            "approved"
        ):
            return "awaiting_approval"
        return "completed"
    return current
