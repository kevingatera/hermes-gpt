"""A2A Fabric dispatch coordinator and idempotency ledger."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from operator_fabric_core import (
    _AUTH_RANK,
    _DEFAULT_FEATURES,
    _MAX_BODY,
    _SAFE_EVIDENCE_PROVENANCE,
    _TERMINAL_COORD,
    CAPABILITY_SCHEMA,
    COORDINATOR_DB_ENV,
    FABRIC_VERSION,
    FabricError,
    FabricNode,
    _audit,
    _auth_object,
    _bounded_coordinator_peer_values,
    _bounded_strings,
    _build_envelope,
    _closed,
    _connect,
    _connect_readonly,
    _contract_forbidden_actions_present,
    _contract_profile_scope,
    _contract_sha,
    _db_path,
    _dispatch_id,
    _fabric_options,
    _init_coordinator_db,
    _now,
    _request,
    _rpc_call,
    _validate_evidence,
    _validate_response,
    canonical_json,
    load_node_registry,
    sha256_json,
    strict_json_loads,
)


class FabricCoordinator:
    def __init__(
        self,
        *,
        registry_loader: Callable[[], dict[str, FabricNode]] | None = None,
        db_path: Path | None = None,
        rpc: Callable[[FabricNode, dict[str, Any], int], tuple[str | None, dict[str, Any]]] | None = None,
        hermes_root: Path | None = None,
    ):
        self.hermes_root = hermes_root
        self.registry_loader = registry_loader or (
            lambda: load_node_registry(hermes_root=hermes_root)
        )
        self.db_path = db_path or _db_path(COORDINATOR_DB_ENV, "coordinator.db", hermes_root)
        # Deliberately lazy: constructing the Fabric backend during ordinary
        # Work Contract validation must not create or mutate state.
        self.rpc = rpc or (lambda node, request, timeout: _rpc_call(node, request, timeout=timeout))
        self._lock = threading.RLock()

    def _ensure_db(self) -> None:
        _init_coordinator_db(self.db_path)

    def _node(self, name: str) -> FabricNode:
        node = self.registry_loader().get(name)
        if node is None or not node.enabled:
            raise FabricError("FABRIC_NODE_NOT_ENROLLED", "Fabric node is not enrolled/enabled")
        return node

    def _capabilities(self, node: FabricNode, timeout: int) -> dict[str, Any]:
        _, response = self.rpc(
            node,
            _request("capabilities", node.coordinator_principal, data={}),
            timeout,
        )
        response = _validate_response(response, operation="capabilities")
        if not response["ok"]:
            raise FabricError(response["code"], "Fabric capability negotiation failed")
        data = _closed(
            response["data"],
            required={
                "schema",
                "version",
                "node_name",
                "identity",
                "features",
                "operations",
                "policy_sha256",
                "snapshot_sha256",
            },
            name="capability response",
        )
        if (
            data["schema"] != CAPABILITY_SCHEMA
            or data["version"] != FABRIC_VERSION
            or data["node_name"] != node.name
            or data["identity"] != node.expected_identity
        ):
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "managed peer capability identity/version is incompatible",
            )
        features = set(
            _bounded_strings(data["features"], field="capability.features", maximum=32, item_max=128)
        )
        if not set(_DEFAULT_FEATURES).union(node.required_features) <= features:
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "managed peer lacks required Fabric features",
            )
        expected_sha = sha256_json({key: data[key] for key in data if key != "snapshot_sha256"})
        if data["snapshot_sha256"] != expected_sha:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "capability snapshot digest is invalid")
        return data

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        dry_run: bool,
        confirm: bool,
        timeout: int,
    ) -> dict[str, Any]:
        node_name, remote_backend, logical_workspace, remote_options, evidence_policy = _fabric_options(
            contract
        )
        node = self._node(node_name)
        assigned_profile, _profile_scope = _contract_profile_scope(contract)
        if _contract_forbidden_actions_present(contract):
            raise FabricError(
                "FABRIC_EVIDENCE_POLICY_INVALID",
                "verified Fabric cannot prove non-empty forbidden-action checks",
            )
        if contract.get("assigned_agent") != node.name:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "Fabric contract assigned_agent must match the managed node name",
            )
        if assigned_profile not in node.allowed_profiles:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "contract profile is outside managed-node policy",
            )
        auth = _auth_object(contract.get("authorization"))
        if _AUTH_RANK[auth["class"]] > _AUTH_RANK[node.max_authorization]:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "contract authorization exceeds managed-node ceiling",
            )
        if remote_backend not in node.allowed_remote_backends:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "remote backend is outside managed-node policy",
            )
        if logical_workspace not in node.logical_workspaces:
            raise FabricError(
                "FABRIC_WORKSPACE_DENIED",
                "logical workspace is outside managed-node policy",
            )

        contract_sha = _contract_sha(contract)
        stable_dispatch_id = _dispatch_id(contract_sha, str(contract["task_id"]), node.name)
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "changed": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": stable_dispatch_id,
                "remote_backend": remote_backend,
                "logical_workspace": logical_workspace,
                "live_peer_verification": "required_before_dispatch",
            }
        if not confirm:
            raise FabricError("CONFIRMATION_REQUIRED", "Fabric dispatch requires confirm=true")

        self._ensure_db()
        with _connect_readonly(self.db_path) as db:
            existing = db.execute(
                "SELECT a.* FROM attempts a WHERE a.dispatch_id=? ORDER BY a.created_at LIMIT 1",
                (stable_dispatch_id,),
            ).fetchone()
        if existing is not None:
            return {
                "success": existing["state"]
                in {"SUBMITTED", "RUNNING", "TERMINAL_REPORTED", "COMPLETED"},
                "changed": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": stable_dispatch_id,
                "attempt_id": existing["attempt_id"],
                "state": existing["state"],
                "remote_task_id": existing["remote_task_id"],
                "idempotent": True,
            }

        capabilities = self._capabilities(node, timeout)
        envelope = _build_envelope(
            contract,
            node,
            remote_backend=remote_backend,
            logical_workspace=logical_workspace,
            remote_options=remote_options,
            evidence_policy=evidence_policy,
            capability_sha=capabilities["snapshot_sha256"],
        )
        envelope_sha = sha256_json(envelope)
        dispatch_id = envelope["dispatch_id"]
        attempt_id = envelope["attempt_id"]
        now = _now()
        with self._lock, _connect(self.db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO dispatches"
                "(dispatch_id,task_id,contract_sha256,node_name,evidence_policy_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    dispatch_id,
                    envelope["task_id"],
                    envelope["contract_sha256"],
                    node.name,
                    canonical_json({key: list(value) for key, value in evidence_policy.items()}),
                    now,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,"
                "coordinator_principal,capability_sha256,peer_policy_sha256,state,remote_task_id,"
                "evidence_json,error_code,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL,NULL,NULL,?,?)",
                (
                    attempt_id,
                    dispatch_id,
                    envelope_sha,
                    node.name,
                    node.a2a_peer_name,
                    remote_backend,
                    node.coordinator_principal,
                    capabilities["snapshot_sha256"],
                    "SUBMITTING",
                    now,
                    now,
                ),
            )

        request = _request(
            "accept",
            node.coordinator_principal,
            data={"envelope": envelope},
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
        )
        try:
            remote_task_id, response = self.rpc(node, request, timeout)
        except FabricError as exc:
            state = "SUBMISSION_AMBIGUOUS" if exc.ambiguous else "BLOCKED"
            with _connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, _now(), attempt_id),
                )
            _audit(
                "hermes_fabric_dispatch",
                success=False,
                changed=exc.ambiguous,
                summary="Fabric dispatch ambiguous" if exc.ambiguous else "Fabric dispatch failed",
                extra={
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "node": node.name,
                    "code": exc.code,
                    "principal": node.coordinator_principal,
                },
            )
            if exc.ambiguous:
                return {
                    "success": False,
                    "changed": True,
                    "backend": "fabric",
                    "code": exc.code,
                    "node": node.name,
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": state,
                    "submission_may_have_succeeded": True,
                    "suggested_action": (
                        "Reconcile this Fabric attempt; do not create a replacement writer."
                    ),
                }
            raise

        response = _validate_response(response, operation="accept")
        data = response["data"]
        if data.get("dispatch_id") != dispatch_id or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer accept response lineage mismatch")
        state = "SUBMITTED" if response["ok"] else "BLOCKED"
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,remote_task_id=?,peer_policy_sha256=?,error_code=?,"
                "updated_at=? WHERE attempt_id=?",
                (
                    *(peer_values[key] for key in optional),
                    state,
                    remote_task_id,
                    data.get("policy_sha256"),
                    None if response["ok"] else response["code"],
                    _now(),
                    attempt_id,
                ),
            )
        _audit(
            "hermes_fabric_dispatch",
            success=response["ok"],
            changed=response["ok"],
            summary=f"Fabric attempt submitted to {node.name}",
            extra={
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "task_id": envelope["task_id"],
                "node": node.name,
                "remote_backend": remote_backend,
                "principal": node.coordinator_principal,
                "remote_task_id": remote_task_id or "",
            },
        )
        return {
            "success": response["ok"],
            "changed": response["ok"],
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "remote_task_id": remote_task_id,
            "state": state,
            "code": response["code"],
        }

    def _attempt(self, attempt_id: str) -> tuple[sqlite3.Row, sqlite3.Row, FabricNode]:
        if not self.db_path.is_file():
            raise FabricError(
                "FABRIC_ATTEMPT_NOT_FOUND",
                "coordinator Fabric attempt does not exist",
            )
        with _connect_readonly(self.db_path) as db:
            attempt = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise FabricError(
                    "FABRIC_ATTEMPT_NOT_FOUND",
                    "coordinator Fabric attempt does not exist",
                )
            dispatch = db.execute(
                "SELECT * FROM dispatches WHERE dispatch_id=?",
                (attempt["dispatch_id"],),
            ).fetchone()
        if dispatch is None:
            raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
        return attempt, dispatch, self._node(attempt["node_name"])

    def poll(
        self,
        attempt_id: str,
        *,
        reconcile: bool = False,
        timeout: int = 15,
    ) -> dict[str, Any]:
        attempt, dispatch, node = self._attempt(attempt_id)
        operation = "reconcile" if reconcile else "status"
        _, response = self.rpc(
            node,
            _request(
                operation,
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation=operation)
        data = response["data"]
        if data.get("dispatch_id") != attempt["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer status lineage mismatch")
        peer_state = str(data.get("state") or "")
        state = {
            "ACCEPTED": "SUBMITTED",
            "RUNNING": "RUNNING",
            "CANCEL_REQUESTED": "CANCEL_REQUESTED",
            "SUCCEEDED": "TERMINAL_REPORTED",
            "FAILED": "TERMINAL_REPORTED",
            "CANCELLED": "CANCELLED",
            "LOST_AMBIGUOUS": "BLOCKED",
            "BLOCKED": "BLOCKED",
        }.get(peer_state, "BLOCKED")
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,updated_at=? WHERE attempt_id=?",
                (*(peer_values[key] for key in optional), state, _now(), attempt_id),
            )
        result = {
            "success": True,
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": attempt["dispatch_id"],
            "attempt_id": attempt_id,
            "state": state,
            "peer_state": peer_state,
            "task_id": dispatch["task_id"],
        }
        result.update({key: peer_values[key] for key in optional})
        return result

    def collect(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        attempt, _dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            _request(
                "evidence",
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation="evidence")
        if not response["ok"]:
            raise FabricError(response["code"], "peer did not return admissible evidence")
        data = _closed(response["data"], required={"evidence"}, name="evidence response")
        with _connect_readonly(self.db_path) as db:
            dispatch = db.execute(
                "SELECT * FROM dispatches WHERE dispatch_id=?",
                (attempt["dispatch_id"],),
            ).fetchone()
        if dispatch is None:
            raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
        policy_raw = strict_json_loads(dispatch["evidence_policy_json"], maximum=16_000)
        allowed = (
            tuple(policy_raw.get("run_state") or [])
            if isinstance(policy_raw, dict)
            else ()
        )
        attempt_map = dict(attempt)
        attempt_map["_coordinator_db"] = str(self.db_path)
        admitted = _validate_evidence(
            data["evidence"],
            attempt=attempt_map,
            node=node,
            allowed_provenance=allowed,
        )
        terminal = admitted["terminal_state"]
        state = (
            "COMPLETED"
            if terminal == "SUCCEEDED"
            else "FAILED"
            if terminal == "FAILED"
            else "CANCELLED"
        )
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,evidence_json=?,updated_at=? WHERE attempt_id=?",
                (state, canonical_json(admitted), _now(), attempt_id),
            )
        _audit(
            "hermes_fabric_evidence",
            success=True,
            changed=False,
            summary=f"Fabric evidence admitted from {node.name}",
            extra={
                "dispatch_id": attempt["dispatch_id"],
                "attempt_id": attempt_id,
                "task_id": dispatch["task_id"],
                "node": node.name,
                "principal": node.coordinator_principal,
                "terminal_state": terminal,
            },
        )
        return {
            "success": True,
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": attempt["dispatch_id"],
            "attempt_id": attempt_id,
            "state": state,
            "evidence": admitted,
        }

    def cancel(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        attempt, dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            _request(
                "cancel",
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation="cancel")
        data = response["data"]
        if data.get("dispatch_id") != attempt["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer cancel lineage mismatch")
        state = "CANCELLED" if data.get("state") == "CANCELLED" else "CANCEL_REQUESTED"
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,updated_at=? WHERE attempt_id=?",
                (*(peer_values[key] for key in optional), state, _now(), attempt_id),
            )
        return {
            "success": True,
            "changed": bool(data.get("changed")),
            "backend": "fabric",
            "attempt_id": attempt_id,
            "dispatch_id": attempt["dispatch_id"],
            "task_id": dispatch["task_id"],
            "state": state,
        }

    def observed_runs(self, task_id: str, *, refresh: bool = True) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        if refresh:
            with _connect_readonly(self.db_path) as db:
                rows = db.execute(
                    "SELECT a.* FROM attempts a JOIN dispatches d ON d.dispatch_id=a.dispatch_id"
                    " WHERE d.task_id=? ORDER BY a.created_at",
                    (task_id,),
                ).fetchall()
            for row in rows:
                try:
                    if row["state"] not in _TERMINAL_COORD:
                        status = self.poll(
                            row["attempt_id"],
                            reconcile=row["state"] == "SUBMISSION_AMBIGUOUS",
                            timeout=10,
                        )
                        if status["state"] == "TERMINAL_REPORTED":
                            self.collect(row["attempt_id"], timeout=10)
                    elif row["state"] in {"COMPLETED", "FAILED", "CANCELLED"} and not row[
                        "evidence_json"
                    ]:
                        self.collect(row["attempt_id"], timeout=10)
                except FabricError:
                    continue

        with _connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT a.* FROM attempts a JOIN dispatches d ON d.dispatch_id=a.dispatch_id"
                " WHERE d.task_id=? ORDER BY a.created_at",
                (task_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row["evidence_json"]:
                continue
            try:
                evidence = strict_json_loads(row["evidence_json"], maximum=_MAX_BODY)
            except FabricError:
                continue
            if not isinstance(evidence, dict):
                continue
            for observation in evidence.get("observations", []):
                if (
                    not isinstance(observation, dict)
                    or observation.get("kind") != "run_state"
                    or observation.get("provenance") not in _SAFE_EVIDENCE_PROVENANCE
                ):
                    continue
                terminal = evidence.get("terminal_state")
                mapped = (
                    "completed"
                    if terminal == "SUCCEEDED"
                    else "failed"
                    if terminal == "FAILED"
                    else "cancelled"
                )
                out.append(
                    {
                        "task_id": task_id,
                        "status": mapped,
                        "outcome": mapped,
                        "error": observation.get("error") or "",
                        "started_at": observation.get("started_at") or "",
                        "ended_at": observation.get("ended_at") or "",
                        "scope": f"fabric:{row['node_name']}",
                        "backend": "fabric",
                        "remote_backend": row["remote_backend"],
                        "attempt_id": row["attempt_id"],
                        "dispatch_id": row["dispatch_id"],
                        "evidence_provenance": observation.get("provenance"),
                    }
                )
        return out
