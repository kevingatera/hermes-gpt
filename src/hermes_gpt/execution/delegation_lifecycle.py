"""Mission synchronization and concurrency guards for delegations."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.execution import contract as contract_mod
from hermes_gpt.missions import runtime as mission_runtime
from hermes_gpt.execution.delegation_store import SCHEMA_VERSION, TERMINAL_STATES, _connect, _db_path, _get_row, _init, _root, _surface


def _ensure_mission(mission_id: str, hermes_root: Path | None) -> None:
    if not mission_id:
        return
    payload = json.loads(
        mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root)
    )
    if (
        not payload.get("success")
        or payload.get("found") is False
        or payload.get("mission_id") != mission_id
        or "status" not in payload
    ):
        raise LookupError(f"mission {mission_id!r} was not found")


def _mission_state(state: str) -> str:
    if state in {"reserved", "queued", "reconciling"}:
        return "pending" if state in {"reserved", "queued"} else "blocked"
    return state


def _sync_mission_attachment(
    row: dict[str, Any],
    state: str,
    *,
    evidence_ref: str,
    verified: bool = False,
    hermes_root: Path | None = None,
) -> bool:
    mission_id = str(row.get("mission_id") or "")
    if not mission_id:
        return True
    return mission_runtime.record_attachment_state(
        mission_id,
        "delegation",
        str(row["delegation_id"]),
        state,
        evidence_ref=evidence_ref,
        verified=verified,
        hermes_root=_root(hermes_root),
    )


@contextmanager
def mission_completion_guard(
    mission_id: str,
    snapshots: dict[str, int],
    *,
    hermes_root: Path | None = None,
):
    """Linearize Mission completion against delegation cancellation authority."""
    if not snapshots:
        yield
        return
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        for delegation_id, authority_version in snapshots.items():
            row = dict(_get_row(db, delegation_id))
            if (
                row.get("mission_id") != mission_id
                or int(row.get("authority_version") or 0) != int(authority_version)
                or bool(row.get("cancel_requested"))
                or bool(row.get("cancellation_in_progress"))
            ):
                raise ValueError(
                    "delegation authority changed after Mission observation"
                )
        try:
            yield
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()


@contextmanager
def mission_cancellation_guard(
    mission_id: str,
    snapshots: dict[str, int],
    *,
    hermes_root: Path | None = None,
):
    """Linearize parent cancellation against terminal delegation authority."""
    if not snapshots:
        yield
        return
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        for delegation_id, authority_version in snapshots.items():
            row = dict(_get_row(db, delegation_id))
            terminal_cancel = row.get("state") == "cancelled" and bool(
                row.get("cancel_requested")
            )
            if (
                row.get("mission_id") != mission_id
                or int(row.get("authority_version") or 0) != int(authority_version)
                or row.get("state") not in TERMINAL_STATES
                or bool(row.get("cancellation_in_progress"))
                or (bool(row.get("cancel_requested")) and not terminal_cancel)
            ):
                raise ValueError(
                    "delegation authority changed after Mission cancellation observation"
                )
        try:
            yield
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()


def _mission_sync_failure(
    operation: str,
    row: dict[str, Any],
    *,
    changed: bool,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "success": False,
        "schema_version": SCHEMA_VERSION,
        "code": "DELEGATION_MISSION_SYNC_FAILED",
        "safe_message": f"Delegation {operation} committed but Mission linkage could not be synchronized.",
        "changed": changed,
        "delegation": _surface(row),
        "suggested_action": "Reconcile the Delegation and Mission before allowing Mission completion.",
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _dispatch_in_progress(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_DISPATCH_AMBIGUOUS",
            "safe_message": "Delegation dispatch is already in progress and its submission outcome is not yet authoritative.",
            "changed": False,
            "submission_may_have_succeeded": True,
            "delegation": _surface(row),
            "suggested_action": "Reconcile the durable delegation; do not redispatch while its dispatch phase is invoking.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _dispatch_cancelled(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_DISPATCH_CANCELLED",
            "safe_message": "Cancelled delegation lineage cannot be dispatched again.",
            "changed": False,
            "delegation": _surface(row),
            "suggested_action": "Create a new delegation and task lineage if new work is required.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _cancellation_in_progress(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_CANCELLATION_IN_PROGRESS",
            "safe_message": "Cancellation is already in progress for this exact delegation lineage; its backend outcome is not yet authoritative.",
            "changed": False,
            "cancellation_in_progress": True,
            "cancellation_outcome_ambiguous": True,
            "idempotent_retry": True,
            "delegation": _surface(row),
            "suggested_action": "Reconcile the durable delegation; do not invoke backend cancellation again while cancellation_in_progress is set.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _dispatch_cas_lost(row: dict[str, Any]) -> str:
    if row.get("state") == "cancelled" or row.get("cancel_requested"):
        return _dispatch_cancelled(row)
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_DISPATCH_AMBIGUOUS",
            "safe_message": "Delegation authority changed while backend invocation was in progress.",
            "changed": False,
            "submission_may_have_succeeded": True,
            "delegation": _surface(row),
            "suggested_action": "Preserve the current durable state and reconcile; do not redispatch this lineage.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _reserved_cancel_cas_lost(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_CANCEL_AMBIGUOUS",
            "safe_message": "Delegation dispatch advanced before reserved cancellation could commit.",
            "changed": False,
            "submission_may_have_succeeded": row.get("dispatch_phase") == "invoking",
            "delegation": _surface(row),
            "suggested_action": "Reconcile the delegation, then retry cancellation against the authoritative dispatched state if needed.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _dispatched_cancel_cas_lost(row: dict[str, Any], result: dict[str, Any]) -> str:
    if row.get("state") == "cancelled" and bool(row.get("cancel_requested")):
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "changed": False,
                "stale_cancellation": True,
                "delegation": _surface(row),
                "cancel": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "code": "DELEGATION_CANCEL_AMBIGUOUS",
            "safe_message": "Delegation authority changed while backend cancellation was in progress.",
            "changed": False,
            "stale_cancellation": True,
            "delegation": _surface(row),
            "cancel": result,
            "suggested_action": "Preserve the current durable state and reconcile before retrying cancellation.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _latest_observation(task_id: str, hermes_root: Path) -> dict[str, Any] | None:
    runs = contract_mod._observed_runs(task_id, hermes_root)
    if not runs:
        return None

    def key(run: dict[str, Any]) -> tuple[str, str, str]:
        latest = max(
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("created_at") or run.get("updated_at") or ""),
        )
        return (
            latest,
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or run.get("state") or ""),
        )

    return max(runs, key=key)


def _observation_sha256(observed: dict[str, Any] | None) -> str:
    if observed is None:
        return ""
    encoded = json.dumps(observed, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _observation_is_fresh_for_cancellation(
    stored: dict[str, Any], observed: dict[str, Any] | None
) -> bool:
    if observed is None or not bool(stored.get("cancellation_watermark_ready")):
        return False

    def ordered_time(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    claimed_at = ordered_time(stored.get("cancellation_claimed_at"))
    if claimed_at is None:
        return False
    # Only backend terminal timestamps are ordering authority. created/started,
    # generic updated/observed times, and payload/hash changes can describe when
    # a record became visible without proving that terminality followed the
    # cancellation attempt. Supported sources currently normalize to ended_at or
    # completed_at; the additional names preserve the same terminal-only rule for
    # backends that expose an equivalent field directly.
    terminal_times = [
        parsed
        for key in ("ended_at", "completed_at", "finished_at", "terminal_at")
        if (parsed := ordered_time(observed.get(key))) is not None
    ]
    terminal_at = max(terminal_times) if terminal_times else None
    return terminal_at is not None and terminal_at > claimed_at


def _mission_dispatch_guard(
    mission_id: str, delegation_id: str, contract_sha: str, root: Path
) -> None:
    if not mission_id:
        return
    payload = json.loads(
        mission_runtime.hermes_mission_get(mission_id, hermes_root=root)
    )
    if (
        not payload.get("success")
        or payload.get("status") in mission_runtime.TERMINAL_STATUSES
    ):
        raise RuntimeError("Mission is not dispatchable")
    expected = f"contract:{contract_sha}"
    attached = next(
        (
            a
            for a in payload.get("attachments", [])
            if a.get("kind") == "delegation" and a.get("ref") == delegation_id
        ),
        None,
    )
    if (
        not attached
        or attached.get("evidence_ref") != expected
        or attached.get("state") != "pending"
    ):
        raise RuntimeError("Mission delegation reservation is not current")
