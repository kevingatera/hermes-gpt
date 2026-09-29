"""Write containment and acceptance behavior for the G4-C peer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fabric_write_guard as write_guard
import operator_fabric as base
import operator_runner_common as runner_common
import operator_runner_local as runner_local
import operator_runners as runners
from operator_fabric_g4c_protocol import (
    _bounded_peer_observation,
    _terminal_state,
    _validate_envelope,
)

FabricError = base.FabricError
FabricPeerPolicy = base.FabricPeerPolicy
canonical_json = base.canonical_json
sha256_json = base.sha256_json


def write_backend_eligible(service, backend_name: str) -> None:
    if not service.unit_manager.available():
        raise FabricError(
            "FABRIC_EXECUTION_UNIT_UNAVAILABLE",
            "verified Fabric writes require a usable whole-tree execution unit",
        )
    try:
        backend = runners.get_backend(backend_name)
    except LookupError as exc:
        raise FabricError(
            "FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered"
        ) from exc
    if not isinstance(backend, runner_local._LocalProcessBackend):
        raise FabricError(
            "FABRIC_EXECUTION_UNIT_UNSUPPORTED",
            "remote backend does not support verified whole-tree write containment",
        )


def dispatch_contained_write(
    service,
    contract: dict[str, Any],
    *,
    backend_name: str,
    unit_id: str,
    timeout: int,
) -> dict[str, Any]:
    backend = runners.get_backend(backend_name)
    if not isinstance(backend, runner_local._LocalProcessBackend):
        return {"success": False, "code": "FABRIC_EXECUTION_UNIT_UNSUPPORTED"}
    workspace = backend._policy_workspace(contract)
    if not backend.executable():
        return {"success": False, "code": "RUNNER_UNAVAILABLE"}
    backend.build_plan(contract)
    task_id = str(contract["task_id"])
    meta_path, request_path, _log_path = runner_common._job_paths(
        task_id, service.hermes_root
    )
    if meta_path.exists():
        return {"success": False, "code": "RUNNER_JOB_EXISTS"}
    request = {
        "backend": backend_name,
        "contract": contract,
        "timeout": max(10, min(int(timeout), 3600)),
        "hermes_root": str(
            (service.hermes_root or Path.home() / ".hermes").expanduser()
        ),
    }
    runner_common._atomic_json(request_path, request)
    runner_common._atomic_json(
        meta_path,
        {
            "schema_version": runner_common.SCHEMA_VERSION,
            "task_id": task_id,
            "backend": backend_name,
            "state": "queued",
            "outcome": "",
            "workspace": str(workspace),
            "created_at": runner_common._now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "returncode": None,
            "error": "",
        },
    )
    launch = service.unit_manager.launch(
        unit_id,
        task_id,
        workspace,
        runner_common._root(service.hermes_root),
        runner_common._RUNNER_ENTRYPOINT,
    )
    if launch.get("accepted"):
        return {
            "success": True,
            "changed": True,
            "state": "queued",
            "backend": backend_name,
            "task_id": task_id,
        }
    if launch.get("ambiguous"):
        return {
            "success": False,
            "changed": True,
            "ambiguous": True,
            "code": str(launch.get("code") or "FABRIC_EXECUTION_UNIT_START_AMBIGUOUS"),
        }
    try:
        request_path.unlink()
    except OSError:
        pass
    return {
        "success": False,
        "changed": False,
        "code": str(launch.get("code") or "FABRIC_EXECUTION_UNIT_START_FAILED"),
    }


def accept(
    service,
    request: dict[str, Any],
    principal: str,
    policy: FabricPeerPolicy,
) -> dict[str, Any]:
    invocation_marker = object()
    invocation_markers_owned: set[str] = set()
    try:
        return service._accept_impl(
            request,
            principal,
            policy,
            invocation_marker,
            invocation_markers_owned,
        )
    finally:
        if invocation_markers_owned:
            with service._invocations_in_flight_lock:
                for attempt_id in invocation_markers_owned:
                    if (
                        service._invocations_in_flight.get(attempt_id)
                        is invocation_marker
                    ):
                        del service._invocations_in_flight[attempt_id]


def accept_impl(
    service,
    request: dict[str, Any],
    principal: str,
    policy: FabricPeerPolicy,
    invocation_marker: object,
    invocation_markers_owned: set[str],
) -> dict[str, Any]:
    data = base._closed(request["data"], required={"envelope"}, name="accept data")
    envelope = _validate_envelope(data["envelope"])
    if request.get("dispatch_id") and request["dispatch_id"] != envelope["dispatch_id"]:
        raise FabricError(
            "FABRIC_IDEMPOTENCY_CONFLICT",
            "request and envelope dispatch identity differ",
        )
    if request.get("attempt_id") and request["attempt_id"] != envelope["attempt_id"]:
        raise FabricError(
            "FABRIC_IDEMPOTENCY_CONFLICT",
            "request and envelope attempt identity differ",
        )
    mapping = service._authorize_envelope(envelope, principal, policy)
    envelope_sha = sha256_json(data["envelope"])
    is_write = write_guard.is_write(envelope)
    unit_id = ""
    if is_write:
        service._write_backend_eligible(envelope["remote_backend"])
        unit_id = service.unit_manager.unit_name(envelope["attempt_id"])

    with service._lock, base._connect(service.db_path) as db:
        existing = db.execute(
            "SELECT * FROM attempts WHERE attempt_id=?",
            (envelope["attempt_id"],),
        ).fetchone()
        if existing is not None:
            if (
                existing["dispatch_id"] != envelope["dispatch_id"]
                or existing["envelope_sha256"] != envelope_sha
            ):
                raise FabricError(
                    "FABRIC_IDEMPOTENCY_CONFLICT",
                    "attempt identity was reused with different canonical content",
                )
            return base._response(
                "accept",
                ok=True,
                code="FABRIC_IDEMPOTENT_REPLAY",
                data={
                    "dispatch_id": existing["dispatch_id"],
                    "attempt_id": existing["attempt_id"],
                    "state": existing["state"],
                    "local_task_id": existing["local_task_id"],
                    "policy_sha256": existing["policy_sha256"],
                    "write_epoch": existing["write_epoch"],
                    "write_claim_state": service.claims.state(existing),
                    "execution_unit_state": _bounded_peer_observation(
                        "execution_unit_state",
                        service.unit_manager.inspect(
                            str(existing["execution_unit_id"] or "")
                        ).get("state"),
                    ),
                },
            )
        epoch = None
        if is_write:
            epoch = service.claims.acquire(
                db,
                conflict_domain=mapping.conflict_domain,
                attempt_id=envelope["attempt_id"],
                unit_id=unit_id,
            )
        now = base._now()
        db.execute(
            "INSERT INTO attempts"
            "(attempt_id,dispatch_id,envelope_sha256,contract_sha256,task_id,coordinator_principal,"
            "node_name,remote_backend,logical_workspace,conflict_domain,authorization_class,policy_sha256,"
            "local_task_id,state,created_at,updated_at,write_epoch,execution_unit_kind,execution_unit_id,retry_parent_attempt_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                envelope["attempt_id"],
                envelope["dispatch_id"],
                envelope_sha,
                envelope["contract_sha256"],
                envelope["task_id"],
                principal,
                policy.node_name,
                envelope["remote_backend"],
                envelope["logical_workspace"],
                mapping.conflict_domain,
                envelope["authorization"]["class"],
                policy.digest,
                envelope["attempt_id"],
                "ACCEPTED",
                now,
                now,
                epoch,
                "systemd-user-unit" if is_write else None,
                unit_id or None,
                envelope.get("retry_parent_attempt_id"),
            ),
        )
        if is_write:
            # Install the process-local marker before ACCEPTED can become
            # visible outside this transaction.  Only this inserting
            # invocation owns the token and may clear it in _accept's
            # finally block.
            with service._invocations_in_flight_lock:
                service._invocations_in_flight[envelope["attempt_id"]] = (
                    invocation_marker
                )
            invocation_markers_owned.add(envelope["attempt_id"])

    try:
        prestart = service.policy_loader()
        prestart_mapping = service._authorize_envelope(envelope, principal, prestart)
        mapping_changed = (
            prestart_mapping.local_path != mapping.local_path
            or prestart_mapping.revision != mapping.revision
            or prestart_mapping.conflict_domain != mapping.conflict_domain
        )
        if mapping_changed:
            raise FabricError(
                "FABRIC_POLICY_DRIFT",
                "peer workspace policy changed before runner start",
            )
        backend = runners.get_backend(envelope["remote_backend"])
        if not bool(
            backend.availability(hermes_root=service.hermes_root).get("available")
        ):
            raise FabricError(
                "FABRIC_RUNNER_UNAVAILABLE",
                "remote runner is unavailable at pre-start revalidation",
            )
        if is_write:
            service._write_backend_eligible(envelope["remote_backend"])
    except (FabricError, LookupError) as exc:
        with service._lock, base._connect(service.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT state FROM attempts WHERE attempt_id=?",
                (envelope["attempt_id"],),
            ).fetchone()
            terminal_state = (
                "CANCELLED"
                if current is not None
                and current["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                else "BLOCKED"
            )
            changed = db.execute(
                "UPDATE attempts SET state=?,updated_at=?"
                " WHERE attempt_id=?"
                " AND state IN ('ACCEPTED','PRELAUNCH_CANCEL_REQUESTED')",
                (terminal_state, base._now(), envelope["attempt_id"]),
            )
        row = service._row(envelope["dispatch_id"], envelope["attempt_id"])
        if is_write and changed.rowcount:
            service.claims.release(
                row,
                proof=(
                    "prelaunch_cancelled_no_execution"
                    if terminal_state == "CANCELLED"
                    else "prestart_blocked_no_execution"
                ),
            )
        if isinstance(exc, FabricError):
            raise
        raise FabricError(
            "FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered"
        ) from exc

    local_contract = service._local_contract(envelope, prestart_mapping)
    state_persisted = False
    if is_write:
        # ACCEPTED is the only prelaunch-cancellable state.  Commit launch
        # permission durably while serialized with cancellation, then use
        # one short final fence check before calling external code.  Never
        # hold the process lock or a SQLite transaction across dispatch.
        with service._lock, base._connect(service.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            launch_row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (envelope["attempt_id"],),
            ).fetchone()
            claim = db.execute(
                "SELECT * FROM write_claims WHERE conflict_domain=?",
                (mapping.conflict_domain,),
            ).fetchone()
            owns_claim = bool(
                launch_row is not None
                and launch_row["state"] == "ACCEPTED"
                and claim is not None
                and claim["state"] == "ACTIVE"
                and claim["attempt_id"] == envelope["attempt_id"]
                and int(claim["epoch"] or 0) == int(launch_row["write_epoch"] or 0)
                and service._owns_invocation(envelope["attempt_id"], invocation_marker)
            )
            if owns_claim:
                db.execute(
                    "UPDATE attempts SET state='LAUNCHING',updated_at=?"
                    " WHERE attempt_id=? AND state='ACCEPTED'",
                    (base._now(), envelope["attempt_id"]),
                )
        with service._lock:
            owns_invocation = service._owns_invocation(
                envelope["attempt_id"], invocation_marker
            )
            with base._connect(service.db_path) as db:
                launch_row = db.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?",
                    (envelope["attempt_id"],),
                ).fetchone()
                claim = db.execute(
                    "SELECT * FROM write_claims WHERE conflict_domain=?",
                    (mapping.conflict_domain,),
                ).fetchone()
                owns_launch_fence = bool(
                    owns_claim
                    and launch_row is not None
                    and launch_row["state"] in {"LAUNCHING", "CANCEL_REQUESTED"}
                    and owns_invocation
                    and claim is not None
                    and claim["state"] == "ACTIVE"
                    and claim["attempt_id"] == envelope["attempt_id"]
                    and int(claim["epoch"] or 0) == int(launch_row["write_epoch"] or 0)
                )
        if owns_launch_fence:
            try:
                result = service.write_dispatch_fn(
                    local_contract,
                    backend_name=envelope["remote_backend"],
                    unit_id=unit_id,
                    timeout=30,
                )
            except Exception:  # noqa: BLE001 - external runner boundary fails closed
                # Once the external launcher has been invoked, an exception
                # cannot prove that no write execution occurred. A unit that
                # is already quiescent may still have run and mutated state.
                # Preserve durable ownership and force reconciliation.
                result = {
                    "success": False,
                    "changed": False,
                    "ambiguous": True,
                    "code": "FABRIC_WRITE_LAUNCH_EXCEPTION_AMBIGUOUS",
                }
        else:
            result = {
                "success": False,
                "changed": False,
                "code": "FABRIC_LAUNCH_FENCE_REVOKED",
            }
        if not isinstance(result, dict):
            result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
        launch_state = (
            "LOST_AMBIGUOUS"
            if result.get("ambiguous")
            else "RUNNING"
            if result.get("success")
            else "FAILED"
        )
        release_cancelled_launch = False
        with base._connect(service.db_path) as db:
            persisted_state = launch_state
            changed = db.execute(
                "UPDATE attempts SET state=CASE WHEN state='CANCEL_REQUESTED'"
                " THEN state ELSE ? END,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                " WHERE attempt_id=? AND state IN ('LAUNCHING','CANCEL_REQUESTED')",
                (
                    persisted_state,
                    canonical_json(base._bounded_json(result, field="dispatch_result")),
                    prestart.digest,
                    base._now(),
                    envelope["attempt_id"],
                ),
            )
            if changed.rowcount:
                current = db.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?",
                    (envelope["attempt_id"],),
                ).fetchone()
                persisted_state = str(current["state"])
            if not changed.rowcount:
                current = db.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?",
                    (envelope["attempt_id"],),
                ).fetchone()
                cancel_during_launch = bool(
                    current is not None
                    and current["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                )
                if cancel_during_launch and (
                    result.get("success") or result.get("ambiguous")
                ):
                    persisted_state = "CANCEL_REQUESTED"
                    db.execute(
                        "UPDATE attempts SET state='CANCEL_REQUESTED',"
                        "dispatch_result_json=?,policy_sha256=?,updated_at=?"
                        " WHERE attempt_id=? AND state='PRELAUNCH_CANCEL_REQUESTED'",
                        (
                            canonical_json(
                                base._bounded_json(result, field="dispatch_result")
                            ),
                            prestart.digest,
                            base._now(),
                            envelope["attempt_id"],
                        ),
                    )
                elif cancel_during_launch:
                    if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED":
                        persisted_state = "CANCELLED"
                        release_cancelled_launch = True
                    else:
                        persisted_state = "LOST_AMBIGUOUS"
                    db.execute(
                        "UPDATE attempts SET state=?,dispatch_result_json=?,"
                        "policy_sha256=?,updated_at=?"
                        " WHERE attempt_id=? AND state='PRELAUNCH_CANCEL_REQUESTED'",
                        (
                            persisted_state,
                            canonical_json(
                                base._bounded_json(result, field="dispatch_result")
                            ),
                            prestart.digest,
                            base._now(),
                            envelope["attempt_id"],
                        ),
                    )
                elif current is not None:
                    persisted_state = str(current["state"])
        if release_cancelled_launch:
            service.claims.release(
                service._row(envelope["dispatch_id"], envelope["attempt_id"]),
                proof=(
                    "launch_fence_revoked_cancelled_no_execution"
                    if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED"
                    else "cancelled_launch_failure_unit_quiescent"
                ),
            )
        launch_state = persisted_state
        state_persisted = True
    else:
        result = service.dispatch_fn(local_contract, timeout=30)
    if not isinstance(result, dict):
        result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
    state = (
        "LOST_AMBIGUOUS"
        if is_write and result.get("ambiguous")
        else "RUNNING"
        if result.get("success")
        else "FAILED"
    )
    if is_write and state_persisted:
        state = launch_state
    if not state_persisted:
        with base._connect(service.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    canonical_json(base._bounded_json(result, field="dispatch_result")),
                    prestart.digest,
                    base._now(),
                    envelope["attempt_id"],
                ),
            )
    row = service._row(envelope["dispatch_id"], envelope["attempt_id"])
    if is_write and state == "CANCEL_REQUESTED":
        unit_state = service.unit_manager.stop(unit_id)
        if unit_state.get("quiescent"):
            cancel_result = service.cancel_fn(
                row["remote_backend"], row["local_task_id"]
            )
            terminal = _terminal_state(
                base._latest_run(service.observed_fn(row["local_task_id"]))
            )
            state = terminal or "CANCELLED"
            service.claims.release(row, proof="cancel_execution_unit_quiescent")
            with base._connect(service.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                    (state, base._now(), envelope["attempt_id"]),
                )
            result = dict(result)
            result["cancel_changed"] = bool(cancel_result.get("changed", True))
    if is_write and state == "FAILED":
        if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED":
            # The transactional ownership check proved launch was never
            # invoked.  Release only now, after the accepting call has
            # observed and persisted the revoked fence.
            service.claims.release(row, proof="launch_fence_revoked_no_execution")
        else:
            unit_state = service.unit_manager.inspect(unit_id)
            if unit_state.get("quiescent"):
                service.claims.release(row, proof="known_start_failure_unit_quiescent")
            else:
                state = "LOST_AMBIGUOUS"
                with base._connect(service.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                        (state, base._now(), envelope["attempt_id"]),
                    )
    return base._response(
        "accept",
        ok=bool(result.get("success")),
        code=(
            "FABRIC_ACCEPTED"
            if result.get("success")
            else str(result.get("code") or "FABRIC_RUNNER_REJECTED")
        ),
        data={
            "dispatch_id": envelope["dispatch_id"],
            "attempt_id": envelope["attempt_id"],
            "state": state,
            "local_task_id": envelope["attempt_id"],
            "policy_sha256": prestart.digest,
            "write_epoch": row["write_epoch"],
            "write_claim_state": service.claims.state(row),
            "execution_unit_state": _bounded_peer_observation(
                "execution_unit_state",
                "unknown"
                if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED"
                else service.unit_manager.inspect(
                    str(row["execution_unit_id"] or "")
                ).get("state"),
            ),
        },
    )
