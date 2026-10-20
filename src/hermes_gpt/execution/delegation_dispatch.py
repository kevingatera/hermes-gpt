"""Create and submit a durable Work Contract delegation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_gpt.execution import contract as contract_mod
from hermes_gpt.missions import runtime as mission_runtime
from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import runners
from hermes_gpt.execution.delegation_lifecycle import _dispatch_cancelled, _dispatch_cas_lost, _dispatch_in_progress, _ensure_mission, _mission_dispatch_guard, _mission_state, _mission_sync_failure, _sync_mission_attachment
from hermes_gpt.execution.delegation_store import DELEGATION_ID_RE, DELEGATION_SCHEMA, SCHEMA_VERSION, _audit, _backend_ref, _bounded, _connect, _db_path, _error, _event, _get_row, _init, _live_event, _manifest_row, _new_id, _normalize_state, _now, _root, _surface


def hermes_delegation_dispatch(
    contract_json: str,
    mission_id: str = "",
    delegation_id: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    hermes_root: Path | None = None,
) -> str:
    """Dispatch a Work Contract and create one normalized delegation record."""
    policy = op.OperatorPolicy()
    invocation_claimed = False
    backend_called = False
    contract_sha = ""
    root: Path | None = None
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        canonical, contract, contract_sha = contract_mod._parse_contract(contract_json)
        manifest = contract_mod._validation_manifest(contract, contract_sha)
        task_id = contract["task_id"]
        backend = runners.selected_backend(contract)
        delegation_id = delegation_id.strip() or _new_id(contract_sha, task_id)
        if not DELEGATION_ID_RE.fullmatch(delegation_id):
            raise ValueError("delegation_id must match dlg-<bounded-id>")
        _ensure_mission(mission_id, hermes_root)
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        effective_dry = policy.effective_dry_run(dry_run)
        if effective_dry:
            dispatch = json.loads(
                contract_mod.hermes_contract_dispatch(
                    canonical,
                    confirm=confirm,
                    dry_run=True,
                    timeout=timeout,
                    hermes_root=root,
                )
            )
            _audit(
                tool="hermes_delegation_dispatch",
                policy=policy,
                dry_run=True,
                success=bool(dispatch.get("success")),
                changed=False,
                delegation_id=delegation_id,
                task_id=task_id,
                backend=backend,
            )
            return json.dumps(
                {
                    "success": bool(dispatch.get("success")),
                    "schema_version": SCHEMA_VERSION,
                    "delegation_id": delegation_id,
                    "mission_id": mission_id,
                    "task_id": task_id,
                    "contract_sha256": contract_sha,
                    "backend": backend,
                    "dry_run": True,
                    "changed": False,
                    "dispatch": dispatch,
                },
                ensure_ascii=False,
                indent=2,
            )

        now = _now()
        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            collisions = db.execute(
                "SELECT * FROM delegations WHERE delegation_id=? OR task_id=?",
                (delegation_id, task_id),
            ).fetchall()
            if collisions:
                if len(collisions) != 1:
                    raise ValueError(
                        "delegation_id/task_id collision has conflicting lineage"
                    )
                existing = dict(collisions[0])
                exact = (
                    existing["delegation_id"] == delegation_id
                    and existing["task_id"] == task_id
                    and existing["contract_sha256"] == contract_sha
                    and existing["mission_id"] == mission_id
                    and existing["backend"] == backend
                )
                if not exact:
                    raise ValueError(
                        "delegation_id/task_id already belongs to different lineage"
                    )
                stored_manifest = _manifest_row(db, delegation_id)
                if stored_manifest["context_sha256"] != manifest["context_sha256"]:
                    raise ValueError("delegation validation lineage mismatch")
                if existing["state"] == "cancelled":
                    db.commit()
                    return _dispatch_cancelled(existing)
                if existing["dispatch_phase"] != "reserved":
                    db.commit()
                    if existing["dispatch_phase"] == "invoking":
                        return _dispatch_in_progress(existing)
                    return json.dumps(
                        {
                            "success": True,
                            "schema_version": SCHEMA_VERSION,
                            "changed": False,
                            "idempotent": True,
                            "delegation": _surface(existing),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
            else:
                db.execute(
                    "INSERT INTO delegations(delegation_id,schema,mission_id,task_id,contract_sha256,backend,state,backend_state,outcome,backend_ref_json,validation_verdict,cancel_requested,dispatch_phase,created_at,dispatched_at,updated_at,terminal_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        delegation_id,
                        DELEGATION_SCHEMA,
                        mission_id,
                        task_id,
                        contract_sha,
                        backend,
                        "reserved",
                        "",
                        "",
                        "{}",
                        "",
                        0,
                        "reserved",
                        now,
                        "",
                        now,
                        None,
                    ),
                )
                db.execute(
                    "INSERT INTO delegation_validation_manifests(delegation_id,schema,context_sha256,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        delegation_id,
                        manifest["schema"],
                        manifest["context_sha256"],
                        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                _event(db, delegation_id, "delegation.reserved", to_state="reserved")
            db.commit()

        if mission_id and not mission_runtime.reserve_delegation_attachment(
            mission_id,
            delegation_id,
            evidence_ref=f"contract:{contract_sha}",
            hermes_root=root,
        ):
            raise RuntimeError("Mission delegation reservation was rejected")

        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE delegations SET dispatch_phase='invoking',updated_at=? "
                "WHERE delegation_id=? AND dispatch_phase='reserved' AND state='reserved' AND cancel_requested=0",
                (_now(), delegation_id),
            ).rowcount
            if changed != 1:
                row = dict(_get_row(db, delegation_id))
                db.commit()
                if row["state"] == "cancelled":
                    return _dispatch_cancelled(row)
                if row["dispatch_phase"] == "invoking":
                    return _dispatch_in_progress(row)
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "changed": False,
                        "idempotent": True,
                        "delegation": _surface(row),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            _event(
                db,
                delegation_id,
                "delegation.invoking",
                from_state="reserved",
                to_state="reserved",
            )
            db.commit()
        invocation_claimed = True

        try:
            _mission_dispatch_guard(mission_id, delegation_id, contract_sha, root)
        except Exception:
            with _connect(path, write=True) as db:
                _init(db)
                db.execute(
                    "UPDATE delegations SET dispatch_phase='reserved',updated_at=? WHERE delegation_id=? AND dispatch_phase='invoking'",
                    (_now(), delegation_id),
                )
                db.commit()
            invocation_claimed = False
            raise

        backend_called = True
        dispatch = json.loads(
            contract_mod.hermes_contract_dispatch(
                canonical,
                confirm=confirm,
                dry_run=False,
                timeout=timeout,
                hermes_root=root,
            )
        )
        ambiguous = bool(dispatch.get("submission_may_have_succeeded")) or (
            not dispatch.get("success") and dispatch.get("changed") is not False
        )
        if not dispatch.get("success") and not ambiguous:
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE delegations SET dispatch_phase='reserved',updated_at=? "
                    "WHERE delegation_id=? AND dispatch_phase='invoking' AND state='reserved' AND cancel_requested=0",
                    (_now(), delegation_id),
                ).rowcount
                if changed == 1:
                    _event(
                        db,
                        delegation_id,
                        "delegation.dispatch_rejected",
                        from_state="reserved",
                        to_state="reserved",
                    )
                db.commit()
                row = dict(_get_row(db, delegation_id))
            if changed != 1:
                invocation_claimed = False
                return _dispatch_cas_lost(row)
            invocation_claimed = False
            return json.dumps(
                {
                    "success": False,
                    "schema_version": SCHEMA_VERSION,
                    "delegation_id": delegation_id,
                    "task_id": task_id,
                    "backend": backend,
                    "changed": False,
                    "dispatch": dispatch,
                },
                ensure_ascii=False,
                indent=2,
            )

        now = _now()
        backend_state = str(
            dispatch.get("state")
            or dispatch.get("status")
            or ("ambiguous" if ambiguous else "queued")
        )
        state = "reconciling" if ambiguous else _normalize_state(backend_state)
        if state == "succeeded":
            # Dispatch self-report is never completion proof. Reconciliation must
            # validate the matching Work Contract before success is durable.
            state = "reconciling"
        if state == "reconciling" and not ambiguous and not backend_state:
            state = "queued"
        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            phase = "invoking" if ambiguous else "dispatched"
            changed = db.execute(
                "UPDATE delegations SET state=?,backend_state=?,backend_ref_json=?,dispatch_phase=?,dispatched_at=?,updated_at=? "
                "WHERE delegation_id=? AND dispatch_phase='invoking' AND state='reserved' AND cancel_requested=0",
                (
                    state,
                    _bounded(backend_state, 128),
                    json.dumps(_backend_ref(dispatch), sort_keys=True),
                    phase,
                    now,
                    now,
                    delegation_id,
                ),
            ).rowcount
            if changed == 1:
                _event(
                    db,
                    delegation_id,
                    "delegation.dispatched",
                    to_state=state,
                    backend_state=backend_state,
                )
            db.commit()
            row = dict(_get_row(db, delegation_id))
        invocation_claimed = False
        if changed != 1:
            return _dispatch_cas_lost(row)
        mission_linked = True
        if mission_id:
            mission_linked = _sync_mission_attachment(
                row,
                _mission_state(state),
                evidence_ref=f"contract:{contract_sha}",
                hermes_root=root,
            )
        _live_event("delegation.dispatched", row, hermes_root=root)
        if not mission_linked:
            _audit(
                tool="hermes_delegation_dispatch",
                policy=policy,
                dry_run=False,
                success=False,
                changed=True,
                delegation_id=delegation_id,
                task_id=task_id,
                backend=backend,
            )
            return _mission_sync_failure(
                "dispatch",
                row,
                changed=True,
                extra={"dispatch": dispatch, "submission_may_have_succeeded": True},
            )
        _audit(
            tool="hermes_delegation_dispatch",
            policy=policy,
            dry_run=False,
            success=True,
            changed=True,
            delegation_id=delegation_id,
            task_id=task_id,
            backend=backend,
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "changed": True,
                **({"submission_may_have_succeeded": True} if ambiguous else {}),
                "delegation": _surface(row),
                "mission_linked": bool(mission_id),
                "dispatch": dispatch,
            },
            ensure_ascii=False,
            indent=2,
        )
    except (
        ValueError,
        TypeError,
        LookupError,
        PermissionError,
        RuntimeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        if invocation_claimed and backend_called and root is not None:
            try:
                with _connect(_db_path(root), write=True) as db:
                    _init(db)
                    changed = db.execute(
                        "UPDATE delegations SET state='reconciling',backend_state='ambiguous',updated_at=? "
                        "WHERE delegation_id=? AND dispatch_phase='invoking' AND state='reserved' AND cancel_requested=0",
                        (_now(), delegation_id),
                    ).rowcount
                    if changed == 1:
                        _event(
                            db,
                            delegation_id,
                            "delegation.dispatch_ambiguous",
                            from_state="reserved",
                            to_state="reconciling",
                            backend_state="ambiguous",
                        )
                    db.commit()
            except (OSError, sqlite3.Error):
                pass
        envelope = _error(
            exc,
            "DELEGATION_DISPATCH_REJECTED",
            "Check Work Contract, Mission linkage, runner availability, and mutation policy.",
        )
        if invocation_claimed and backend_called:
            # Backend already accepted the submission; the failure was local
            # persistence, not dispatch rejection. Surface the ambiguity so
            # callers reconcile instead of treating this as a safe no-op.
            payload = json.loads(envelope)
            payload["submission_may_have_succeeded"] = True
            payload["changed"] = True
            payload["suggested_action"] = (
                "Backend invocation outcome is ambiguous. Reconcile the durable delegation; it will not be redispatched."
            )
            envelope = json.dumps(payload, ensure_ascii=False, indent=2)
        return envelope
