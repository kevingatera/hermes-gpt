"""G4-C peer service with write ownership and artifact admission."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import fabric_artifacts as artifacts
from hermes_gpt.fleet import fabric_write_guard as write_guard
from hermes_gpt.fleet import fabric as base
from hermes_gpt.fleet import fabric_g4c_peer_write as peer_write
from hermes_gpt.fleet.fabric_g4c_protocol import _bounded_peer_observation, _terminal_state, _validate_request

FEATURE_RECONCILE = "reconcile-v1"
FabricError = base.FabricError
FabricPeerPolicy = base.FabricPeerPolicy
SystemdUserUnitManager = write_guard.SystemdUserUnitManager
sha256_json = base.sha256_json


class FabricPeerService(base.FabricPeerService):
    """Managed peer with G4-C writer and artifact guarantees."""

    def __init__(
        self,
        *,
        unit_manager: Any | None = None,
        write_dispatch_fn: Callable[..., dict[str, Any]] | None = None,
        artifact_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        write_guard.migrate_peer(self.db_path)
        artifacts.migrate_peer(self.db_path)
        self.claims = write_guard.WriteClaims(self.db_path)
        self.unit_manager = unit_manager or SystemdUserUnitManager()
        self.write_dispatch_fn = write_dispatch_fn or self._dispatch_contained_write
        self._invocations_in_flight: dict[str, object] = {}
        self._invocations_in_flight_lock = threading.Lock()
        snapshot_root = artifact_root or base._root(self.hermes_root) / "fabric" / "artifacts"
        self.artifact_store = artifacts.PeerArtifactStore(self.db_path, snapshot_root)

    def _invocation_in_flight(self, attempt_id: str) -> bool:
        with self._invocations_in_flight_lock:
            return attempt_id in self._invocations_in_flight

    def _owns_invocation(self, attempt_id: str, marker: object) -> bool:
        with self._invocations_in_flight_lock:
            return self._invocations_in_flight.get(attempt_id) is marker

    def capabilities(self, policy: FabricPeerPolicy) -> dict[str, Any]:
        payload = super().capabilities(policy)
        features = list(payload["features"])
        for feature in (*artifacts.ARTIFACT_FEATURES, FEATURE_RECONCILE):
            if feature not in features:
                features.append(feature)
        if self.unit_manager.available():
            for feature in write_guard.WRITE_FEATURES:
                if feature not in features:
                    features.append(feature)
        payload["features"] = features
        payload["operations"] = [
            "capabilities",
            "accept",
            "status",
            "reconcile",
            "cancel",
            "evidence",
            "artifact_manifest",
            "artifact_chunk",
        ]
        payload.pop("snapshot_sha256", None)
        payload["snapshot_sha256"] = sha256_json(payload)
        return payload

    def handle(self, request_value: dict[str, Any], authorization: str) -> dict[str, Any]:
        request = _validate_request(request_value)
        principal = self.authenticate(authorization)
        if principal != request["coordinator_principal"]:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "authenticated principal does not match request",
            )
        policy = self.policy_loader()
        if principal not in policy.allowed_coordinator_principals:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal is not authorized by peer policy",
            )
        operation = request["operation"]
        if operation == "capabilities":
            base._closed(request["data"], required=set(), name="capabilities data")
            return base._response(
                operation,
                ok=True,
                code="FABRIC_OK",
                data=self.capabilities(policy),
            )
        if operation == "accept":
            return self._accept(request, principal, policy)

        dispatch_id = base._bounded_string(
            request.get("dispatch_id"),
            field="dispatch_id",
            pattern=base._ID_RE,
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"),
            field="attempt_id",
            pattern=base._ID_RE,
        )
        if operation in {"status", "reconcile", "cancel", "evidence"}:
            base._closed(request["data"], required=set(), name=f"{operation} data")
        if operation == "status":
            data = self._status(dispatch_id, attempt_id, reconcile=False)
        elif operation == "reconcile":
            data = self._status(dispatch_id, attempt_id, reconcile=True)
        elif operation == "cancel":
            data = self._cancel(dispatch_id, attempt_id, principal, policy)
        elif operation == "evidence":
            data = self._evidence(dispatch_id, attempt_id, principal, policy)
        elif operation == "artifact_manifest":
            data = self._artifact_manifest(request, principal, policy)
        elif operation == "artifact_chunk":
            data = self._artifact_chunk(request, principal, policy)
        else:
            raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "unsupported Fabric operation")
        return base._response(operation, ok=True, code="FABRIC_OK", data=data)

    def _write_backend_eligible(self, backend_name: str) -> None:
        peer_write.write_backend_eligible(self, backend_name)

    def _dispatch_contained_write(
        self,
        contract: dict[str, Any],
        *,
        backend_name: str,
        unit_id: str,
        timeout: int,
    ) -> dict[str, Any]:
        return peer_write.dispatch_contained_write(
            self,
            contract,
            backend_name=backend_name,
            unit_id=unit_id,
            timeout=timeout,
        )

    def _accept(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        return peer_write.accept(self, request, principal, policy)

    def _accept_impl(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
        invocation_marker: object,
        invocation_markers_owned: set[str],
    ) -> dict[str, Any]:
        return peer_write.accept_impl(
            self,
            request,
            principal,
            policy,
            invocation_marker,
            invocation_markers_owned,
        )

    def _status(
        self,
        dispatch_id: str,
        attempt_id: str,
        *,
        reconcile: bool,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["authorization_class"] not in write_guard.WRITE_AUTH:
            return super()._status(dispatch_id, attempt_id, reconcile=reconcile)
        if row["state"] == "ACCEPTED":
            invocation_in_flight = self._invocation_in_flight(attempt_id)
            if reconcile and not invocation_in_flight:
                proof = "abandoned_accepted_prelaunch_no_execution"
                with self._lock, base._connect(self.db_path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT * FROM attempts WHERE attempt_id=? AND dispatch_id=?",
                        (attempt_id, dispatch_id),
                    ).fetchone()
                    if (
                        current is not None
                        and current["state"] == "ACCEPTED"
                        and not self._invocation_in_flight(attempt_id)
                    ):
                        # Recheck while serialized with insertion: launch is
                        # invoked only after durable LAUNCHING, so absence of
                        # this invocation marker positively proves that an
                        # ACCEPTED attempt never reached external execution.
                        db.execute(
                            "UPDATE attempts SET state='BLOCKED',updated_at=?"
                            " WHERE attempt_id=? AND state='ACCEPTED'",
                            (base._now(), attempt_id),
                        )
                        db.execute(
                            "UPDATE write_claims SET state='RELEASED',released_at=?,"
                            "release_proof=? WHERE conflict_domain=? AND attempt_id=?"
                            " AND epoch=? AND state='ACTIVE'",
                            (
                                base._now(),
                                proof,
                                current["conflict_domain"],
                                current["attempt_id"],
                                int(current["write_epoch"]),
                            ),
                        )
                row = self._row(dispatch_id, attempt_id)
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        if row["state"] == "CANCEL_REQUESTED" and self._invocation_in_flight(attempt_id):
            unit = self.unit_manager.inspect(str(row["execution_unit_id"] or ""))
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state", unit.get("state")
                ),
            }
        if row["state"] in {"LAUNCHING", "PRELAUNCH_CANCEL_REQUESTED"}:
            unit_id = str(row["execution_unit_id"] or "")
            run = base._latest_run(self.observed_fn(row["local_task_id"]))
            terminal = _terminal_state(run)
            unit = self.unit_manager.inspect(unit_id)
            invocation_in_flight = self._invocation_in_flight(attempt_id)
            if invocation_in_flight or (row["state"] == "LAUNCHING" and not reconcile):
                state = (
                    "CANCEL_REQUESTED"
                    if row["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    else "LAUNCHING"
                )
                return {
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": state,
                    "local_task_id": row["local_task_id"],
                    "policy_sha256": row["policy_sha256"],
                    "write_epoch": row["write_epoch"],
                    "write_claim_state": self.claims.state(row),
                    "execution_unit_state": _bounded_peer_observation(
                        "execution_unit_state", unit.get("state")
                    ),
                }
            if row["state"] == "PRELAUNCH_CANCEL_REQUESTED" and unit.get("active") and reconcile:
                unit = self.unit_manager.stop(unit_id)
            if terminal and unit.get("quiescent"):
                state = terminal
                self.claims.release(row, proof="launching_unit_terminal_and_quiescent")
            elif row["state"] == "PRELAUNCH_CANCEL_REQUESTED" and unit.get("quiescent"):
                state = "CANCELLED"
                self.claims.release(row, proof="cancelled_launch_unit_quiescent")
            elif unit.get("active"):
                state = (
                    "CANCEL_REQUESTED"
                    if row["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    else "RUNNING"
                )
            else:
                # A persisted launch fence with neither a durable terminal
                # outcome nor a provably quiescent cancellation has uncertain
                # execution history.  Keep the non-expiring claim.
                state = "LOST_AMBIGUOUS"
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                    (state, base._now(), attempt_id),
                )
            refreshed = self._row(dispatch_id, attempt_id)
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": state,
                "local_task_id": refreshed["local_task_id"],
                "policy_sha256": refreshed["policy_sha256"],
                "write_epoch": refreshed["write_epoch"],
                "write_claim_state": self.claims.state(refreshed),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state", unit.get("state")
                ),
            }
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": "terminal",
            }
        run = base._latest_run(self.observed_fn(row["local_task_id"]))
        terminal = _terminal_state(run)
        unit = self.unit_manager.inspect(str(row["execution_unit_id"] or ""))
        if terminal and unit.get("quiescent"):
            state = terminal
            self.claims.release(row, proof="execution_unit_terminal_and_quiescent")
        elif terminal and unit.get("active"):
            state = "RUNNING"
        elif terminal:
            state = "LOST_AMBIGUOUS"
        elif unit.get("active"):
            state = "RUNNING"
        elif unit.get("quiescent"):
            state = "LOST_AMBIGUOUS"
            if reconcile:
                self.claims.release(
                    row,
                    proof="execution_unit_quiescent_without_terminal_observation",
                )
        elif reconcile:
            state = "LOST_AMBIGUOUS"
        else:
            state = str(row["state"])
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, base._now(), attempt_id),
            )
        refreshed = self._row(dispatch_id, attempt_id)
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "local_task_id": refreshed["local_task_id"],
            "policy_sha256": refreshed["policy_sha256"],
            "write_epoch": refreshed["write_epoch"],
            "write_claim_state": self.claims.state(refreshed),
            "execution_unit_state": str(unit.get("state") or "unknown"),
        }

    def _cancel(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["authorization_class"] not in write_guard.WRITE_AUTH:
            return super()._cancel(dispatch_id, attempt_id, principal, policy)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "cancel identity does not match accepted attempt",
            )
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "idempotent": True,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
            }
        if row["state"] in {"ACCEPTED", "LAUNCHING"}:
            with self._lock, base._connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                        "UPDATE attempts SET state=CASE"
                        " WHEN state='ACCEPTED' THEN 'PRELAUNCH_CANCEL_REQUESTED'"
                        " ELSE 'CANCEL_REQUESTED' END,updated_at=?"
                        " WHERE attempt_id=? AND state IN ('ACCEPTED','LAUNCHING')",
                        (base._now(), attempt_id),
                    )
            if changed.rowcount:
                refreshed = self._row(dispatch_id, attempt_id)
                invocation_in_flight = self._invocation_in_flight(attempt_id)
                if (
                    refreshed["state"] == "CANCEL_REQUESTED"
                    and not invocation_in_flight
                ):
                    row = refreshed
                else:
                    return {
                        "dispatch_id": dispatch_id,
                        "attempt_id": attempt_id,
                        "state": "CANCEL_REQUESTED",
                        "changed": True,
                        "write_epoch": refreshed["write_epoch"],
                        "write_claim_state": self.claims.state(refreshed),
                        "execution_unit_state": _bounded_peer_observation(
                            "execution_unit_state",
                            self.unit_manager.inspect(
                                str(refreshed["execution_unit_id"] or "")
                            ).get("state"),
                        ),
                    }
            row = self._row(dispatch_id, attempt_id)
            if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
                return {
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": row["state"],
                    "idempotent": True,
                    "write_epoch": row["write_epoch"],
                    "write_claim_state": self.claims.state(row),
                }
        if row["state"] == "PRELAUNCH_CANCEL_REQUESTED":
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "changed": False,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        if row["state"] == "CANCEL_REQUESTED" and self._invocation_in_flight(attempt_id):
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "changed": False,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state='CANCEL_REQUESTED',updated_at=? WHERE attempt_id=?",
                (base._now(), attempt_id),
            )
        unit = self.unit_manager.stop(str(row["execution_unit_id"] or ""))
        if unit.get("quiescent"):
            result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
            terminal = _terminal_state(base._latest_run(self.observed_fn(row["local_task_id"])))
            state = terminal or "CANCELLED"
            self.claims.release(row, proof="cancel_execution_unit_quiescent")
            changed = bool(result.get("changed", True))
        else:
            state = "CANCEL_REQUESTED"
            changed = False
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, base._now(), attempt_id),
            )
        refreshed = self._row(dispatch_id, attempt_id)
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "changed": changed,
            "write_epoch": refreshed["write_epoch"],
            "write_claim_state": self.claims.state(refreshed),
            "execution_unit_state": _bounded_peer_observation(
                "execution_unit_state", unit.get("state")
            ),
        }

    def _artifact_manifest(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = base._closed(
            request["data"],
            required={"artifacts", "max_artifact_bytes", "max_total_bytes"},
            name="artifact manifest data",
        )
        if (
            isinstance(data["max_artifact_bytes"], bool)
            or not isinstance(data["max_artifact_bytes"], int)
            or isinstance(data["max_total_bytes"], bool)
            or not isinstance(data["max_total_bytes"], int)
            or data["max_artifact_bytes"] <= 0
            or data["max_total_bytes"] <= 0
        ):
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact size limits are invalid")
        dispatch_id = base._bounded_string(
            request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE
        )
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "artifact identity does not match accepted attempt",
            )
        status = self._status(dispatch_id, attempt_id, reconcile=False)
        if status["state"] != "SUCCEEDED":
            raise FabricError(
                "FABRIC_ARTIFACT_NOT_READY",
                "artifacts may be finalized only after successful terminal execution",
            )
        if self.claims.state(row) == "ACTIVE":
            raise FabricError("FABRIC_ARTIFACT_NOT_READY", "write claim is still active")
        raw_specs = data["artifacts"]
        if not isinstance(raw_specs, list) or len(raw_specs) > artifacts.MAX_ARTIFACTS:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact request is not bounded")
        specs: list[dict[str, Any]] = []
        for raw in raw_specs:
            spec = base._closed(
                raw,
                required={"path", "must_exist", "min_bytes"},
                name="artifact spec",
            )
            name = artifacts.logical_name(spec["path"])
            min_bytes = spec["min_bytes"]
            if isinstance(min_bytes, bool) or not isinstance(min_bytes, int) or min_bytes < 0:
                raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact min_bytes is invalid")
            specs.append(
                {
                    "path": name,
                    "must_exist": bool(spec["must_exist"]),
                    "min_bytes": min_bytes,
                }
            )
        mapping = policy.workspace_mappings.get(row["logical_workspace"])
        if mapping is None:
            raise FabricError("FABRIC_WORKSPACE_DENIED", "attempt workspace is no longer mapped")
        manifest = self.artifact_store.manifest(row, mapping, specs)
        if (
            manifest["total_bytes"] > min(data["max_total_bytes"], artifacts.MAX_TOTAL_ARTIFACT_BYTES)
            or any(
                item["size_bytes"] > min(data["max_artifact_bytes"], artifacts.MAX_ARTIFACT_BYTES)
                for item in manifest["artifacts"]
            )
        ):
            raise FabricError(
                "FABRIC_ARTIFACT_TOO_LARGE",
                "artifact exceeds coordinator-requested bound",
            )
        return {"manifest": manifest}

    def _artifact_chunk(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = base._closed(
            request["data"],
            required={"artifact_id", "offset", "max_bytes"},
            name="artifact chunk data",
        )
        dispatch_id = base._bounded_string(
            request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE
        )
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "artifact chunk identity mismatch",
            )
        artifact_id = base._bounded_string(
            data["artifact_id"], field="artifact_id", pattern=base._ID_RE
        )
        offset = data["offset"]
        maximum = data["max_bytes"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
        ):
            raise FabricError(
                "FABRIC_ARTIFACT_CHUNK_INVALID",
                "artifact chunk bounds are invalid",
            )
        return {
            "chunk": self.artifact_store.chunk(
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                artifact_id=artifact_id,
                offset=offset,
                maximum=maximum,
            )
        }
