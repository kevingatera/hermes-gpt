"""Apply cancellation claims and backend results to delegation state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import operator_mission_runtime as mission_runtime
import operator_policy as op
import operator_runners as runners
from operator_delegation_lifecycle import (
    _cancellation_in_progress,
    _dispatched_cancel_cas_lost,
    _latest_observation,
    _mission_state,
    _mission_sync_failure,
    _observation_sha256,
    _reserved_cancel_cas_lost,
    _sync_mission_attachment,
)
from operator_delegation_store import (
    SCHEMA_VERSION,
    TERMINAL_STATES,
    _audit,
    _bounded,
    _connect,
    _db_path,
    _error,
    _event,
    _get_row,
    _init,
    _live_event,
    _now,
    _root,
    _surface,
)


def hermes_delegation_cancel(
    delegation_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            stored = dict(_get_row(db, delegation_id))
        if stored["state"] in TERMINAL_STATES:
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "changed": False,
                    "delegation": _surface(stored),
                },
                ensure_ascii=False,
                indent=2,
            )
        if stored.get("dispatch_phase") == "reserved":
            if dry_run or policy.effective_dry_run(dry_run):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "changed": False,
                        "dry_run": True,
                        "delegation_id": delegation_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            if not confirm:
                return json.dumps(
                    {
                        "success": False,
                        "schema_version": SCHEMA_VERSION,
                        "code": "CONFIRMATION_REQUIRED",
                        "safe_message": "delegation cancellation requires confirm=true",
                        "changed": False,
                        "delegation_id": delegation_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            now = _now()
            with _connect(path, write=True) as db:
                _init(db)
                changed = db.execute(
                    "UPDATE delegations SET state='cancelled',backend_state='not_invoked',outcome='cancelled',"
                    "cancel_requested=1,cancellation_in_progress=0,authority_version=authority_version+1,dispatch_phase='cancelled',updated_at=?,terminal_at=? "
                    "WHERE delegation_id=? AND dispatch_phase='reserved' AND state='reserved' AND cancel_requested=0",
                    (now, now, delegation_id),
                ).rowcount
                if changed != 1:
                    row = dict(_get_row(db, delegation_id))
                    db.commit()
                    return _reserved_cancel_cas_lost(row)
                _event(
                    db,
                    delegation_id,
                    "delegation.cancelled",
                    from_state="reserved",
                    to_state="cancelled",
                    backend_state="not_invoked",
                )
                db.commit()
                row = dict(_get_row(db, delegation_id))
            mission_synced = True
            if row.get("mission_id"):
                mission_synced = _sync_mission_attachment(
                    row,
                    "cancelled",
                    evidence_ref=f"delegation:{delegation_id}",
                    hermes_root=root,
                )
            _live_event("delegation.cancelled", row, hermes_root=root)
            if not mission_synced:
                _audit(
                    tool="hermes_delegation_cancel",
                    policy=policy,
                    dry_run=False,
                    success=False,
                    changed=True,
                    delegation_id=delegation_id,
                    task_id=row["task_id"],
                    backend=row["backend"],
                )
                return _mission_sync_failure("cancellation", row, changed=True)
            _audit(
                tool="hermes_delegation_cancel",
                policy=policy,
                dry_run=False,
                success=True,
                changed=True,
                delegation_id=delegation_id,
                task_id=row["task_id"],
                backend=row["backend"],
            )
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "changed": True,
                    "delegation": _surface(row),
                },
                ensure_ascii=False,
                indent=2,
            )
        effective_dry = policy.effective_dry_run(dry_run)
        prior_cancel_requested = bool(stored.get("cancel_requested"))
        if not effective_dry and not confirm:
            return json.dumps(
                {
                    "success": False,
                    "schema_version": SCHEMA_VERSION,
                    "code": "CONFIRMATION_REQUIRED",
                    "safe_message": "delegation cancellation requires confirm=true",
                    "changed": False,
                    "delegation_id": delegation_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        if not effective_dry:
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("BEGIN IMMEDIATE")
                current = dict(_get_row(db, delegation_id))
                prior_cancel_requested = bool(current.get("cancel_requested"))
                if current["state"] in TERMINAL_STATES:
                    db.commit()
                    return json.dumps(
                        {
                            "success": True,
                            "schema_version": SCHEMA_VERSION,
                            "changed": False,
                            "delegation": _surface(current),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                if current.get("mission_id"):
                    mission = json.loads(
                        mission_runtime.hermes_mission_get(
                            current["mission_id"], hermes_root=root
                        )
                    )
                    if (
                        not mission.get("success")
                        or mission.get("status") == "completed"
                    ):
                        db.commit()
                        raise ValueError(
                            "completed Mission delegation cancellation authority is closed"
                        )
                claimed_at = _now()
                changed = db.execute(
                    "UPDATE delegations SET cancel_requested=1,cancellation_in_progress=1,cancellation_claimed_at=?,"
                    "cancellation_observation_sha256='',cancellation_watermark_ready=0,authority_version=authority_version+1,updated_at=? "
                    "WHERE delegation_id=? AND state NOT IN ('succeeded','failed','cancelled') AND cancellation_in_progress=0",
                    (claimed_at, claimed_at, delegation_id),
                ).rowcount
                if changed != 1:
                    row = dict(_get_row(db, delegation_id))
                    if (
                        bool(row.get("cancellation_in_progress"))
                        and bool(row.get("cancel_requested"))
                        and row.get("state") not in TERMINAL_STATES
                    ):
                        db.commit()
                        return _cancellation_in_progress(row)
                    else:
                        db.commit()
                        return _dispatched_cancel_cas_lost(row, {})
                else:
                    db.commit()
                    stored = dict(_get_row(db, delegation_id))
            watermark = _observation_sha256(
                _latest_observation(stored["task_id"], root)
            )
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE delegations SET cancellation_observation_sha256=?,cancellation_watermark_ready=1,updated_at=? "
                    "WHERE delegation_id=? AND cancellation_in_progress=1 AND authority_version=? "
                    "AND cancellation_claimed_at=? AND cancellation_watermark_ready=0",
                    (
                        watermark,
                        _now(),
                        delegation_id,
                        stored["authority_version"],
                        stored["cancellation_claimed_at"],
                    ),
                ).rowcount
                current = dict(_get_row(db, delegation_id))
                db.commit()
            if changed != 1:
                return (
                    _cancellation_in_progress(current)
                    if current.get("cancellation_in_progress")
                    else _dispatched_cancel_cas_lost(current, {})
                )
            stored = current
        result = json.loads(
            runners.hermes_runner_cancel(
                stored["task_id"],
                backend=stored["backend"],
                confirm=confirm,
                dry_run=dry_run,
                hermes_root=root,
            )
        )
        if effective_dry:
            return json.dumps(
                {
                    "success": bool(result.get("success")),
                    "schema_version": SCHEMA_VERSION,
                    "changed": False,
                    "dry_run": True,
                    "delegation_id": delegation_id,
                    "cancel": result,
                },
                ensure_ascii=False,
                indent=2,
            )
        if not result.get("success"):
            # A backend's explicit unchanged rejection proves that this call had
            # no side effect, so release our provisional cancellation latch.  A
            # missing/true ``changed`` value is ambiguous and must stay latched
            # until reconciliation establishes authority.
            if result.get("changed") is False:
                with _connect(path, write=True) as db:
                    _init(db)
                    db.execute("BEGIN IMMEDIATE")
                    current = dict(_get_row(db, delegation_id))
                    authority_fields = (
                        "schema",
                        "mission_id",
                        "task_id",
                        "contract_sha256",
                        "backend",
                        "state",
                        "backend_state",
                        "outcome",
                        "validation_verdict",
                        "cancel_requested",
                        "cancellation_in_progress",
                        "authority_version",
                        "cancellation_claimed_at",
                        "cancellation_observation_sha256",
                        "cancellation_watermark_ready",
                        "dispatch_phase",
                        "dispatched_at",
                        "updated_at",
                        "terminal_at",
                    )
                    if all(
                        current.get(key) == stored.get(key) for key in authority_fields
                    ):
                        db.execute(
                            "UPDATE delegations SET cancel_requested=?,cancellation_in_progress=0,"
                            "authority_version=authority_version+1,updated_at=? WHERE delegation_id=?",
                            (1 if prior_cancel_requested else 0, _now(), delegation_id),
                        )
                    db.commit()
                    current = dict(_get_row(db, delegation_id))
                return json.dumps(
                    {
                        "success": False,
                        "schema_version": SCHEMA_VERSION,
                        "changed": False,
                        "delegation_id": delegation_id,
                        "delegation": _surface(current),
                        "cancel": result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            return json.dumps(
                {
                    "success": False,
                    "schema_version": SCHEMA_VERSION,
                    "changed": False,
                    "delegation_id": delegation_id,
                    "cancel": result,
                },
                ensure_ascii=False,
                indent=2,
            )
        now = _now()
        backend_state = str(result.get("state") or "").strip()
        normalized_backend_state = backend_state.lower().replace("-", "_")
        # Cancellation finality requires an explicit cancelled/canceled backend
        # confirmation.  Every other response remains reconciling until normal
        # authoritative observation establishes the terminal execution result.
        desired = (
            "cancelled"
            if normalized_backend_state in {"cancelled", "canceled"}
            else "reconciling"
        )
        outcome = desired if desired in TERMINAL_STATES else ""
        event_type = (
            "delegation.cancelled"
            if desired == "cancelled"
            else "delegation.cancel_requested"
        )
        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            current = dict(_get_row(db, delegation_id))
            promoted_cancellation = False
            authority_fields = (
                "schema",
                "mission_id",
                "task_id",
                "contract_sha256",
                "backend",
                "state",
                "backend_state",
                "outcome",
                "validation_verdict",
                "cancel_requested",
                "cancellation_in_progress",
                "authority_version",
                "cancellation_claimed_at",
                "cancellation_observation_sha256",
                "cancellation_watermark_ready",
                "dispatch_phase",
                "dispatched_at",
                "updated_at",
                "terminal_at",
            )
            if any(current.get(key) != stored.get(key) for key in authority_fields):
                immutable_lineage = (
                    "schema",
                    "mission_id",
                    "task_id",
                    "contract_sha256",
                    "backend",
                    "dispatched_at",
                )
                same_lineage = all(
                    current.get(key) == stored.get(key) for key in immutable_lineage
                )
                can_promote = (
                    desired == "cancelled"
                    and same_lineage
                    and current.get("state") not in TERMINAL_STATES
                    and bool(current.get("cancel_requested"))
                    and current.get("dispatch_phase") == stored.get("dispatch_phase")
                )
                if not can_promote:
                    db.commit()
                    return _dispatched_cancel_cas_lost(current, result)
                terminal_at = current.get("terminal_at") or now
                db.execute(
                    "UPDATE delegations SET state='cancelled',backend_state=?,outcome='cancelled',"
                    "cancel_requested=1,cancellation_in_progress=0,dispatch_phase='cancelled',updated_at=?,terminal_at=? WHERE delegation_id=?",
                    (_bounded(backend_state, 128), now, terminal_at, delegation_id),
                )
                _event(
                    db,
                    delegation_id,
                    "delegation.cancelled",
                    from_state=current["state"],
                    to_state="cancelled",
                    backend_state=backend_state,
                )
                promoted_cancellation = True
            else:
                dispatch_phase = (
                    "cancelled"
                    if desired == "cancelled"
                    else stored.get("dispatch_phase", "dispatched")
                )
                db.execute(
                    "UPDATE delegations SET state=?,backend_state=?,outcome=?,cancel_requested=1,cancellation_in_progress=?,dispatch_phase=?,updated_at=?,terminal_at=? WHERE delegation_id=?",
                    (
                        desired,
                        _bounded(backend_state, 128),
                        outcome,
                        0 if desired == "cancelled" else 1,
                        dispatch_phase,
                        now,
                        now if desired in TERMINAL_STATES else None,
                        delegation_id,
                    ),
                )
                _event(
                    db,
                    delegation_id,
                    event_type,
                    from_state=stored["state"],
                    to_state=desired,
                    backend_state=backend_state,
                )
            db.commit()
            row = dict(_get_row(db, delegation_id))
        if promoted_cancellation:
            desired = "cancelled"
            event_type = "delegation.cancelled"
        mission_synced = True
        if row.get("mission_id"):
            mission_synced = _sync_mission_attachment(
                row,
                _mission_state(desired),
                evidence_ref=f"delegation:{delegation_id}",
                hermes_root=root,
            )
        _live_event(event_type, row, hermes_root=root)
        if not mission_synced:
            _audit(
                tool="hermes_delegation_cancel",
                policy=policy,
                dry_run=False,
                success=False,
                changed=True,
                delegation_id=delegation_id,
                task_id=row["task_id"],
                backend=row["backend"],
            )
            return _mission_sync_failure(
                "cancellation", row, changed=True, extra={"cancel": result}
            )
        _audit(
            tool="hermes_delegation_cancel",
            policy=policy,
            dry_run=False,
            success=True,
            changed=True,
            delegation_id=delegation_id,
            task_id=row["task_id"],
            backend=row["backend"],
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "changed": True,
                "delegation": _surface(row),
                "cancel": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "DELEGATION_CANCEL_FAILED",
            "Check delegation state, backend cancellation support, and mutation policy.",
        )
