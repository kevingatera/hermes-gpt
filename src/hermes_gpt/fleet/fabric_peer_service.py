"""Deterministic A2A peer acceptor and evidence observer."""

from __future__ import annotations

import hmac
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import runners as op_runners
from hermes_gpt.fleet.fabric_core import _AUTH_RANK, _DEFAULT_FEATURES, _ID_RE, _TERMINAL_PEER, CAPABILITY_SCHEMA, EVIDENCE_SCHEMA, FABRIC_VERSION, PEER_DB_ENV, FabricError, FabricPeerPolicy, WorkspaceMapping, _audit, _auth_object, _bounded_json, _bounded_string, _closed, _connect, _connect_readonly, _db_path, _init_peer_db, _latest_run, _now, _response, _validate_envelope, _validate_request, canonical_json, load_peer_policy, load_peer_tokens, sha256_json


class FabricPeerService:
    """Deterministic managed peer acceptor. No request reaches an LLM path."""

    def __init__(
        self,
        *,
        policy_loader: Callable[[], FabricPeerPolicy] | None = None,
        tokens: dict[str, str] | None = None,
        db_path: Path | None = None,
        dispatch_fn: Callable[..., dict[str, Any]] | None = None,
        observed_fn: Callable[[str], list[dict[str, Any]]] | None = None,
        cancel_fn: Callable[[str, str], dict[str, Any]] | None = None,
        hermes_root: Path | None = None,
    ):
        self.hermes_root = hermes_root
        self.policy_loader = policy_loader or (lambda: load_peer_policy(hermes_root=hermes_root))
        self.tokens = tokens or load_peer_tokens()
        self.db_path = db_path or _db_path(PEER_DB_ENV, "peer.db", hermes_root)
        _init_peer_db(self.db_path)
        self.dispatch_fn = dispatch_fn or self._dispatch_local
        self.observed_fn = observed_fn or (
            lambda task_id: op_runners.observed_runs(task_id, hermes_root=hermes_root)
        )
        self.cancel_fn = cancel_fn or self._cancel_local
        self._lock = threading.RLock()

    def _dispatch_local(self, contract: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return op_runners.dispatch_contract(
            contract,
            confirm=True,
            dry_run=False,
            timeout=int(kwargs.get("timeout", 30)),
            hermes_root=self.hermes_root,
        )

    def _cancel_local(self, backend: str, task_id: str) -> dict[str, Any]:
        return op_runners.get_backend(backend).cancel(task_id, hermes_root=self.hermes_root)

    def authenticate(self, authorization: str) -> str:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_REQUIRED",
                "verified Fabric requires bearer authentication",
            )
        presented = authorization[7:]
        matches = [
            principal
            for principal, token in self.tokens.items()
            if hmac.compare_digest(token, presented)
        ]
        if len(matches) != 1:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal authentication failed",
            )
        return matches[0]

    def capabilities(self, policy: FabricPeerPolicy) -> dict[str, Any]:
        payload = {
            "schema": CAPABILITY_SCHEMA,
            "version": FABRIC_VERSION,
            "node_name": policy.node_name,
            "identity": policy.identity,
            "features": list(dict.fromkeys((*_DEFAULT_FEATURES, *policy.required_features))),
            "operations": ["capabilities", "accept", "status", "reconcile", "cancel", "evidence"],
            "policy_sha256": policy.digest,
        }
        payload["snapshot_sha256"] = sha256_json(payload)
        return payload

    def handle(self, request_value: dict[str, Any], authorization: str) -> dict[str, Any]:
        request = _validate_request(request_value)
        principal = self.authenticate(authorization)
        if principal != request["coordinator_principal"]:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "authenticated principal does not match the request",
            )
        policy = self.policy_loader()
        if principal not in policy.allowed_coordinator_principals:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal is not authorized by peer policy",
            )
        operation = request["operation"]
        if operation == "capabilities":
            _closed(request["data"], required=set(), name="capabilities data")
            return _response(operation, ok=True, code="FABRIC_OK", data=self.capabilities(policy))
        if operation == "accept":
            return self._accept(request, principal, policy)

        _closed(request["data"], required=set(), name=f"{operation} data")
        dispatch_id = _bounded_string(request.get("dispatch_id"), field="dispatch_id", pattern=_ID_RE)
        attempt_id = _bounded_string(request.get("attempt_id"), field="attempt_id", pattern=_ID_RE)
        if operation == "status":
            data = self._status(dispatch_id, attempt_id, reconcile=False)
        elif operation == "reconcile":
            data = self._status(dispatch_id, attempt_id, reconcile=True)
        elif operation == "cancel":
            data = self._cancel(dispatch_id, attempt_id, principal, policy)
        elif operation == "evidence":
            data = self._evidence(dispatch_id, attempt_id, principal, policy)
        else:
            raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "unsupported Fabric operation")
        return _response(operation, ok=True, code="FABRIC_OK", data=data)

    def _authorize_envelope(
        self,
        envelope: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> WorkspaceMapping:
        if envelope["coordinator_principal"] != principal:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "dispatch principal mismatch")
        if envelope["target_node"] != policy.node_name:
            raise FabricError(
                "FABRIC_NODE_IDENTITY_MISMATCH",
                "dispatch targets a different managed node",
            )
        if envelope["assigned_profile"] not in policy.allowed_profiles:
            raise FabricError("FABRIC_AUTHORITY_DENIED", "assigned profile is outside peer policy")
        auth = _auth_object(envelope["authorization"])
        if _AUTH_RANK[auth["class"]] > _AUTH_RANK[policy.max_authorization]:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "dispatch exceeds peer authorization ceiling",
            )
        if envelope["remote_backend"] not in policy.allowed_backends:
            raise FabricError("FABRIC_AUTHORITY_DENIED", "remote backend is outside peer policy")
        mapping = policy.workspace_mappings.get(envelope["logical_workspace"])
        if mapping is None:
            raise FabricError(
                "FABRIC_WORKSPACE_DENIED",
                "logical workspace is not mapped on the peer",
            )
        supported = set(_DEFAULT_FEATURES).union(policy.required_features)
        if not set(envelope["required_features"]) <= supported:
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "peer does not support all required Fabric features",
            )
        return mapping

    def _local_contract(
        self,
        envelope: dict[str, Any],
        mapping: WorkspaceMapping,
    ) -> dict[str, Any]:
        return {
            "schema": "hermes.work-contract/v1",
            "task_id": envelope["attempt_id"],
            "objective": envelope["objective"],
            "assigned_agent": envelope["target_node"],
            "assigned_profile": envelope["assigned_profile"],
            "inputs": list(envelope["inputs"]),
            "constraints": list(envelope["constraints"]),
            "allowed_scope": {
                "workspaces": [str(mapping.local_path)],
                "profiles": [envelope["assigned_profile"]],
            },
            "forbidden_actions": [],
            "expected_artifacts": [],
            "tests": [],
            "review_requirements": {
                "required": False,
                "reviewer": "",
                "evidence": "",
                "approval_required": False,
            },
            "completion_criteria": {
                "run_state": {"terminal": True, "outcome_ok": ["completed"]},
                "artifacts_present": False,
                "tests_pass": False,
                "review_satisfied": False,
                "no_forbidden_actions": True,
            },
            "authorization": dict(envelope["authorization"]),
            "execution": {
                "backend": envelope["remote_backend"],
                "options": _bounded_json(envelope["remote_options"], field="remote_options"),
            },
        }

    def _accept(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = _closed(request["data"], required={"envelope"}, name="accept data")
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
        mapping = self._authorize_envelope(envelope, principal, policy)
        envelope_sha = sha256_json(envelope)
        now = _now()

        with self._lock, _connect(self.db_path) as db:
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
                return _response(
                    "accept",
                    ok=True,
                    code="FABRIC_IDEMPOTENT_REPLAY",
                    data={
                        "dispatch_id": existing["dispatch_id"],
                        "attempt_id": existing["attempt_id"],
                        "state": existing["state"],
                        "local_task_id": existing["local_task_id"],
                        "policy_sha256": existing["policy_sha256"],
                    },
                )

            auth_class = envelope["authorization"]["class"]
            if _AUTH_RANK[auth_class] >= _AUTH_RANK["reversible_write"]:
                claim = db.execute(
                    "SELECT * FROM write_claims WHERE conflict_domain=?",
                    (mapping.conflict_domain,),
                ).fetchone()
                if claim is not None and claim["state"] == "ACTIVE":
                    raise FabricError(
                        "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                        "peer write conflict domain already has an active claim",
                    )
                db.execute(
                    "INSERT OR REPLACE INTO write_claims"
                    "(conflict_domain,attempt_id,state,acquired_at,released_at) VALUES(?,?,?,?,NULL)",
                    (mapping.conflict_domain, envelope["attempt_id"], "ACTIVE", now),
                )

            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,contract_sha256,task_id,"
                "coordinator_principal,node_name,remote_backend,logical_workspace,"
                "conflict_domain,authorization_class,policy_sha256,local_task_id,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    auth_class,
                    policy.digest,
                    envelope["attempt_id"],
                    "ACCEPTED",
                    now,
                    now,
                ),
            )

        # G3-S06: re-read local policy immediately before runner start.
        prestart = self.policy_loader()
        prestart_mapping = self._authorize_envelope(envelope, principal, prestart)
        if (
            prestart_mapping.local_path != mapping.local_path
            or prestart_mapping.revision != mapping.revision
            or prestart_mapping.conflict_domain != mapping.conflict_domain
        ):
            with _connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,policy_sha256=?,updated_at=? WHERE attempt_id=?",
                    ("BLOCKED", prestart.digest, _now(), envelope["attempt_id"]),
                )
            raise FabricError(
                "FABRIC_POLICY_DRIFT",
                "peer workspace policy changed between acceptance and runner start",
            )

        try:
            backend = op_runners.get_backend(envelope["remote_backend"])
        except LookupError as exc:
            raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered") from exc
        if not bool(backend.availability(hermes_root=self.hermes_root).get("available")):
            raise FabricError(
                "FABRIC_RUNNER_UNAVAILABLE",
                "remote runner is unavailable at pre-start revalidation",
            )

        result = self.dispatch_fn(self._local_contract(envelope, prestart_mapping), timeout=30)
        if not isinstance(result, dict):
            result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
        state = "RUNNING" if bool(result.get("success")) else "FAILED"
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    canonical_json(_bounded_json(result, field="dispatch_result")),
                    prestart.digest,
                    _now(),
                    envelope["attempt_id"],
                ),
            )

        _audit(
            "hermes_fabric_peer_accept",
            success=bool(result.get("success")),
            changed=bool(result.get("success")),
            summary=f"Fabric attempt accepted on {policy.node_name}",
            extra={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "task_id": envelope["task_id"],
                "backend": envelope["remote_backend"],
                "principal": principal,
                "policy_sha256": prestart.digest,
            },
        )
        return _response(
            "accept",
            ok=bool(result.get("success")),
            code="FABRIC_ACCEPTED" if result.get("success") else str(result.get("code") or "FABRIC_RUNNER_REJECTED"),
            data={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "state": state,
                "local_task_id": envelope["attempt_id"],
                "policy_sha256": prestart.digest,
            },
        )

    def _row(self, dispatch_id: str, attempt_id: str) -> sqlite3.Row:
        with _connect_readonly(self.db_path) as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=? AND dispatch_id=?",
                (attempt_id, dispatch_id),
            ).fetchone()
        if row is None:
            raise FabricError(
                "FABRIC_ATTEMPT_NOT_FOUND",
                "Fabric attempt is not present in the peer journal",
            )
        return row

    def _status(self, dispatch_id: str, attempt_id: str, *, reconcile: bool) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["state"] in _TERMINAL_PEER:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
            }

        primary = _latest_run(self.observed_fn(row["local_task_id"]))
        state = row["state"]
        if primary is not None:
            run_state = str(primary.get("state") or primary.get("status") or "").lower()
            if run_state in {"completed", "succeeded", "success"}:
                state = "SUCCEEDED"
            elif run_state in {"failed", "error"}:
                state = "FAILED"
            elif run_state in {"cancelled", "canceled"}:
                state = "CANCELLED"
            else:
                state = "RUNNING"
        elif reconcile and state in {"ACCEPTED", "RUNNING", "CANCEL_REQUESTED"}:
            state = "LOST_AMBIGUOUS"

        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, _now(), attempt_id),
            )
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "local_task_id": row["local_task_id"],
            "policy_sha256": row["policy_sha256"],
        }

    def _cancel(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "cancel identity does not match accepted attempt",
            )
        if row["state"] in _TERMINAL_PEER:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "idempotent": True,
            }
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                ("CANCEL_REQUESTED", _now(), attempt_id),
            )
        result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
        state = (
            "CANCELLED"
            if bool(result.get("success"))
            and str(result.get("state") or "").lower() in {"cancelled", "canceled"}
            else "CANCEL_REQUESTED"
        )
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, _now(), attempt_id),
            )
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "changed": bool(result.get("changed")),
        }

    def _evidence(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        status = self._status(dispatch_id, attempt_id, reconcile=False)
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "evidence identity does not match accepted attempt",
            )
        if status["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise FabricError(
                "FABRIC_EVIDENCE_NOT_READY",
                "terminal remote execution evidence is not available",
            )

        primary = _latest_run(self.observed_fn(row["local_task_id"]))
        observations: list[dict[str, Any]] = []
        if primary is not None:
            observations.append(
                {
                    "kind": "run_state",
                    "provenance": "managed_peer_structured",
                    "state": str(primary.get("status") or primary.get("state") or ""),
                    "outcome": str(primary.get("outcome") or primary.get("state") or primary.get("status") or ""),
                    "started_at": str(primary.get("started_at") or primary.get("dispatched_at") or ""),
                    "ended_at": str(primary.get("ended_at") or primary.get("completed_at") or ""),
                    "error": op.redact_output(str(primary.get("error") or ""))[:1_000],
                    "source": f"runner:{row['remote_backend']}",
                }
            )
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "version": FABRIC_VERSION,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "contract_sha256": row["contract_sha256"],
            "task_id": row["task_id"],
            "node_name": row["node_name"],
            "peer_identity": policy.identity,
            "coordinator_principal": principal,
            "remote_backend": row["remote_backend"],
            "terminal_state": status["state"],
            "observations": observations,
            "policy_sha256": row["policy_sha256"],
            "created_at": _now(),
        }
        return {"evidence": evidence}
