"""Reconcile backend observations into durable delegation state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import operator_contract as contract_mod
import operator_policy as op
from operator_delegation_lifecycle import (
    _latest_observation,
    _mission_state,
    _mission_sync_failure,
    _observation_is_fresh_for_cancellation,
    _sync_mission_attachment,
)
from operator_delegation_store import (
    DELEGATION_ID_RE,
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
    _manifest_row,
    _normalize_state,
    _now,
    _root,
    _surface,
)


def hermes_delegation_reconcile(
    delegation_id: str,
    contract_json: str = "",
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Derive normalized state from authoritative runner/Fabric observations.

    Backend terminal success is necessary but never sufficient. A delegation can
    become ``succeeded`` only when its matching Work Contract is currently
    ``SATISFIED`` (or a previously persisted SATISFIED verdict exists for the
    same immutable contract digest). Missing/unreadable/UNVERIFIED evidence
    therefore remains fail-closed as ``reconciling``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if apply:
            policy.require_level("workspace")
            policy.require_mutation(False)
        if not DELEGATION_ID_RE.fullmatch(delegation_id or ""):
            raise ValueError("delegation_id is invalid")
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            stored = dict(_get_row(db, delegation_id))

        if contract_json:
            _canonical, contract, sha = contract_mod._parse_contract(contract_json)
            if (
                sha != stored["contract_sha256"]
                or contract["task_id"] != stored["task_id"]
            ):
                raise ValueError(
                    "contract_json does not match delegation validation lineage"
                )
            manifest = contract_mod._validation_manifest(contract, sha)
        else:
            with _connect(path, write=False) as db:
                manifest = _manifest_row(db, delegation_id)
        manifest_contract, manifest_sha = (
            contract_mod._contract_from_validation_manifest(manifest)
        )
        if (
            manifest_sha != stored["contract_sha256"]
            or manifest_contract["task_id"] != stored["task_id"]
        ):
            raise ValueError("delegation validation manifest does not match lineage")

        observed = _latest_observation(stored["task_id"], root)
        if observed is None:
            observed_desired = (
                "reconciling"
                if stored["state"] not in TERMINAL_STATES
                else stored["state"]
            )
            backend_state = "unobserved"
            outcome = stored.get("outcome") or ""
        else:
            backend_state = str(
                observed.get("status")
                or observed.get("state")
                or observed.get("outcome")
                or "unknown"
            )
            outcome = str(
                observed.get("outcome") or observed.get("state") or backend_state
            )
            observed_desired = _normalize_state(outcome or backend_state)
            if observed.get("error"):
                observed_desired = "failed"

        validation = contract_mod._validate_manifest_impl(manifest, None, root)
        verdict = str(validation.get("verdict") or "")

        authoritative_cancel = stored["state"] == "cancelled" and bool(
            stored.get("cancel_requested")
        )
        cancellation_pending = bool(stored.get("cancel_requested")) or bool(
            stored.get("cancellation_in_progress")
        )
        resolved_cancel_requested = bool(stored.get("cancel_requested"))
        resolved_cancellation_in_progress = bool(stored.get("cancellation_in_progress"))
        dispatch_phase = str(stored.get("dispatch_phase") or "dispatched")
        desired = observed_desired
        if authoritative_cancel:
            desired = "cancelled"
            outcome = stored.get("outcome") or "cancelled"
            resolved_cancellation_in_progress = False
            dispatch_phase = "cancelled"
        elif (
            cancellation_pending
            and observed_desired in TERMINAL_STATES
            and _observation_is_fresh_for_cancellation(stored, observed)
        ):
            # A fresh terminal backend observation resolves an ambiguous cancel.
            # Cancellation is durable only when the backend explicitly reports
            # it; other terminal states return to the normal contract path.
            resolved_cancellation_in_progress = False
            if observed_desired == "cancelled":
                desired = "cancelled"
                outcome = "cancelled"
                resolved_cancel_requested = True
                dispatch_phase = "cancelled"
            else:
                resolved_cancel_requested = False
                if observed_desired == "succeeded" and verdict != "SATISFIED":
                    desired = "reconciling"
        elif cancellation_pending:
            desired = "reconciling"
            outcome = ""
        elif observed is None or (
            observed_desired == "succeeded" and verdict != "SATISFIED"
        ):
            desired = "reconciling"
        verified_success = desired == "succeeded" and verdict == "SATISFIED"

        changed = (
            desired != stored["state"]
            or _bounded(backend_state, 128) != stored["backend_state"]
            or _bounded(outcome, 128) != stored.get("outcome", "")
            or verdict != stored.get("validation_verdict", "")
            or resolved_cancel_requested != bool(stored.get("cancel_requested"))
            or resolved_cancellation_in_progress
            != bool(stored.get("cancellation_in_progress"))
            or dispatch_phase != stored.get("dispatch_phase")
        )
        preview = dict(stored)
        preview.update(
            {
                "state": desired,
                "backend_state": _bounded(backend_state, 128),
                "outcome": _bounded(outcome, 128),
                "validation_verdict": _bounded(verdict, 64),
                "cancel_requested": resolved_cancel_requested,
                "cancellation_in_progress": resolved_cancellation_in_progress,
                "dispatch_phase": dispatch_phase,
            }
        )
        if not apply:
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "changed": changed,
                    "applied": False,
                    "delegation": _surface(preview),
                    "observed": observed,
                    "evidence_ref": f"contract:{stored['contract_sha256']}"
                    if verified_success
                    else "",
                },
                ensure_ascii=False,
                indent=2,
            )

        now = _now()
        terminal_at = stored.get("terminal_at") or (
            now if desired in TERMINAL_STATES else None
        )
        if desired not in TERMINAL_STATES:
            terminal_at = None
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
                "cancel_requested",
                "cancellation_in_progress",
                "authority_version",
                "cancellation_claimed_at",
                "cancellation_observation_sha256",
                "cancellation_watermark_ready",
                "dispatch_phase",
                "terminal_at",
                "updated_at",
            )
            stale = any(current.get(key) != stored.get(key) for key in authority_fields)
            if stale:
                db.commit()
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "changed": False,
                        "applied": False,
                        "stale_observation": True,
                        "delegation": _surface(current),
                        "observed": observed,
                        "evidence_ref": "",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            if contract_json:
                db.execute(
                    "INSERT INTO delegation_validation_manifests(delegation_id,schema,context_sha256,manifest_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(delegation_id) DO UPDATE SET schema=excluded.schema,context_sha256=excluded.context_sha256,manifest_json=excluded.manifest_json,updated_at=excluded.updated_at",
                    (
                        delegation_id,
                        manifest["schema"],
                        manifest["context_sha256"],
                        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
            db.execute(
                "UPDATE delegations SET state=?,backend_state=?,outcome=?,validation_verdict=?,"
                "cancel_requested=?,cancellation_in_progress=?,dispatch_phase=?,"
                "authority_version=authority_version+?,updated_at=?,terminal_at=? WHERE delegation_id=?",
                (
                    desired,
                    _bounded(backend_state, 128),
                    _bounded(outcome, 128),
                    _bounded(verdict, 64),
                    1 if resolved_cancel_requested else 0,
                    1 if resolved_cancellation_in_progress else 0,
                    dispatch_phase,
                    1 if changed else 0,
                    now,
                    terminal_at,
                    delegation_id,
                ),
            )
            if changed:
                _event(
                    db,
                    delegation_id,
                    "delegation.reconciled",
                    from_state=stored["state"],
                    to_state=desired,
                    backend_state=backend_state,
                    observed=observed,
                )
            db.commit()
            row = dict(_get_row(db, delegation_id))

        mission_synced = True
        if row.get("mission_id"):
            evidence_ref = (
                f"contract:{row['contract_sha256']}"
                if verified_success
                else f"delegation:{delegation_id}"
            )
            mission_synced = _sync_mission_attachment(
                row,
                _mission_state(desired),
                evidence_ref=evidence_ref,
                verified=verified_success,
                hermes_root=root,
            )
        if changed:
            _live_event("delegation.reconciled", row, hermes_root=root)
        if not mission_synced:
            _audit(
                tool="hermes_delegation_reconcile",
                policy=policy,
                dry_run=False,
                success=False,
                changed=changed,
                delegation_id=delegation_id,
                task_id=row["task_id"],
                backend=row["backend"],
            )
            return _mission_sync_failure(
                "reconciliation",
                row,
                changed=changed,
                extra={"applied": True, "observed": observed},
            )
        _audit(
            tool="hermes_delegation_reconcile",
            policy=policy,
            dry_run=False,
            success=True,
            changed=changed,
            delegation_id=delegation_id,
            task_id=row["task_id"],
            backend=row["backend"],
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "changed": changed,
                "applied": True,
                "delegation": _surface(row),
                "observed": observed,
                "evidence_ref": f"contract:{row['contract_sha256']}"
                if verified_success
                else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    except (
        ValueError,
        TypeError,
        LookupError,
        PermissionError,
        OSError,
        sqlite3.Error,
    ) as exc:
        return _error(
            exc,
            "DELEGATION_RECONCILE_FAILED",
            "Check delegation lineage, observed backend state, and mutation policy.",
        )
