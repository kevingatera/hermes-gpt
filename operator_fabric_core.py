"""Shared Fabric request, transport, contract, and evidence helpers."""

from __future__ import annotations

# Protocol helpers are re-exported here for the Fabric implementation modules.
# ruff: noqa: F401
import hashlib
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_fabric_config as fabric_config
import operator_fabric_store as fabric_store
import operator_fabric_transport as fabric_transport
import operator_fleet as op_fleet
import operator_policy as op
from operator_fabric_protocol import (
    _BACKEND_RE,
    _ID_RE,
    _MAX_BODY,
    _MAX_ITEMS,
    _NODE_RE,
    _PRINCIPAL_RE,
    _PROFILE_RE,
    _SHA_RE,
    FabricError,
    _bounded_json,
    _bounded_string,
    _bounded_strings,
    _closed,
    _require_secure_transport,
    canonical_json,
    sha256_json,
    strict_json_loads,
)

# Keep the configuration names available from this module for existing callers.
FabricNode = fabric_config.FabricNode
FabricPeerPolicy = fabric_config.FabricPeerPolicy
NODE_REGISTRY_ENV = fabric_config.NODE_REGISTRY_ENV
NODE_REGISTRY_SCHEMA = fabric_config.NODE_REGISTRY_SCHEMA
PEER_POLICY_ENV = fabric_config.PEER_POLICY_ENV
PEER_POLICY_SCHEMA = fabric_config.PEER_POLICY_SCHEMA
PEER_TOKENS_ENV = fabric_config.PEER_TOKENS_ENV
WorkspaceMapping = fabric_config.WorkspaceMapping
_AUTH_RANK = fabric_config._AUTH_RANK
_config_path = fabric_config._config_path
_read_closed_json = fabric_config._read_closed_json
_root = fabric_config._root
load_node_registry = fabric_config.load_node_registry
load_peer_policy = fabric_config.load_peer_policy
load_peer_tokens = fabric_config.load_peer_tokens

FABRIC_VERSION = 1
REQUEST_SCHEMA = "hermes.fabric-request/v1"
RESPONSE_SCHEMA = "hermes.fabric-response/v1"
DISPATCH_SCHEMA = "hermes.fabric-dispatch/v1"
EVIDENCE_SCHEMA = "hermes.fabric-evidence/v1"
CAPABILITY_SCHEMA = "hermes.fabric-capability/v1"
COORDINATOR_DB_ENV = "HERMES_GPT_FABRIC_COORDINATOR_DB"
PEER_DB_ENV = "HERMES_GPT_FABRIC_PEER_DB"

# Keep the journal helpers available from this public module for existing callers.
_db_path = fabric_store._db_path
_prepare_db_parent = fabric_store._prepare_db_parent
_connect = fabric_store._connect
_connect_readonly = fabric_store._connect_readonly
_init_coordinator_db = fabric_store._init_coordinator_db
_init_peer_db = fabric_store._init_peer_db

_TERMINAL_PEER = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "LOST_AMBIGUOUS", "BLOCKED"})
_TERMINAL_COORD = frozenset({"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"})
_PEER_WRITE_CLAIM_STATES = frozenset(
    {"NONE", "ACTIVE", "RELEASED", "SUPERSEDED", "UNKNOWN"}
)
_PEER_EXECUTION_UNIT_STATES = frozenset(
    {
        "active", "activating", "deactivating", "reloading", "inactive",
        "failed", "dead", "not-found", "terminal", "unknown",
    }
)
_SAFE_EVIDENCE_PROVENANCE = frozenset(
    {"coordinator_observed", "managed_peer_structured", "artifact_verified", "coordinator_local"}
)
_DEFAULT_FEATURES = (
    "dispatch-v1",
    "evidence-v1",
    "idempotency-v1",
    "principal-auth-v1",
    "closed-schema-v1",
)
_EXPECTED_ERRORS = (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error)


def _bounded_coordinator_peer_values(data: dict[str, Any]) -> dict[str, Any]:
    epoch = data.get("write_epoch")
    claim = data.get("write_claim_state")
    unit = data.get("execution_unit_state")
    return {
        "write_epoch": epoch
        if isinstance(epoch, int) and not isinstance(epoch, bool)
        else None,
        "write_claim_state": claim if claim in _PEER_WRITE_CLAIM_STATES else "UNKNOWN",
        "execution_unit_state": unit if unit in _PEER_EXECUTION_UNIT_STATES else "unknown",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return fabric_transport.http_json(
        url, headers=headers, timeout=timeout, body=body
    )


def _fabric_card(base_url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    return _http_json(
        base_url.rstrip("/") + "/.well-known/agent-card.json",
        headers=headers,
        timeout=timeout,
    )


def _peer_entry(node: FabricNode) -> dict[str, Any]:
    return fabric_transport.peer_entry(node)


def _extract_data_response(task: Any) -> tuple[str | None, dict[str, Any]]:
    return fabric_transport.extract_data_response(task)


def _validate_response(response: Any, *, operation: str) -> dict[str, Any]:
    response = _closed(
        response,
        required={"schema", "version", "operation", "ok", "code", "data"},
        name="Fabric response",
    )
    if (
        response["schema"] != RESPONSE_SCHEMA
        or response["version"] != FABRIC_VERSION
        or response["operation"] != operation
    ):
        raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric response binding does not match the request")
    if not isinstance(response["ok"], bool) or not isinstance(response["code"], str) or not isinstance(response["data"], dict):
        raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric response fields are invalid")
    return response


def _rpc_call(
    node: FabricNode,
    request: dict[str, Any],
    *,
    timeout: int,
) -> tuple[str | None, dict[str, Any]]:
    entry = _peer_entry(node)
    base_url = str(entry["url"])
    headers = op_fleet._auth_header(entry)
    card = _fabric_card(base_url, headers, min(max(timeout, 1), 15))
    if card.get("name") != node.expected_identity:
        raise FabricError(
            "FABRIC_PEER_IDENTITY_MISMATCH",
            "managed peer identity does not match the node registry",
        )

    rpc_url = base_url.rstrip("/")
    interfaces = card.get("supportedInterfaces")
    if isinstance(interfaces, list):
        for interface in interfaces:
            if (
                isinstance(interface, dict)
                and interface.get("protocolBinding") == "JSONRPC"
                and isinstance(interface.get("url"), str)
            ):
                candidate = interface["url"]
                _require_secure_transport(candidate)
                if urllib.parse.urlparse(candidate).hostname != urllib.parse.urlparse(base_url).hostname:
                    raise FabricError(
                        "FABRIC_PEER_IDENTITY_MISMATCH",
                        "Agent Card redirected Fabric to a different host",
                    )
                rpc_url = candidate
                break

    request_id = "frpc-" + hashlib.sha256(
        (canonical_json(request) + str(time.time_ns())).encode()
    ).hexdigest()[:20]
    dispatch_id = str(request.get("dispatch_id") or request.get("request_id") or request_id)
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"data": request, "mediaType": "application/json"}],
                "messageId": "msg-" + request_id[5:],
                "contextId": dispatch_id,
            }
        },
    }
    outer = _http_json(rpc_url, headers=headers, timeout=max(1, min(timeout, 120)), body=body)
    _closed(
        outer,
        required={"jsonrpc", "id"},
        optional={"result", "error"},
        name="A2A JSON-RPC response",
    )
    if outer.get("jsonrpc") != "2.0" or outer.get("id") != request_id:
        raise FabricError("FABRIC_PROTOCOL_ERROR", "A2A response identity mismatch")
    if "error" in outer:
        error = outer["error"] if isinstance(outer["error"], dict) else {}
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        raise FabricError(
            str(data.get("code") or "FABRIC_REMOTE_ERROR"),
            str(error.get("message") or "Fabric peer rejected the request"),
        )
    if "result" not in outer:
        raise FabricError("FABRIC_PROTOCOL_ERROR", "A2A response contains no result")
    remote_task_id, response = _extract_data_response(outer["result"])
    return remote_task_id, _validate_response(response, operation=str(request["operation"]))


def _evidence_policy(options: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    raw = options.get("evidence_provenance")
    if raw is None:
        return {"run_state": ("managed_peer_structured", "coordinator_observed")}
    raw = _closed(
        raw,
        required={"run_state"},
        name="execution.options.evidence_provenance",
    )
    values = tuple(
        _bounded_strings(raw["run_state"], field="evidence_provenance.run_state", maximum=8, item_max=64)
    )
    if not values or any(value not in _SAFE_EVIDENCE_PROVENANCE for value in values):
        raise FabricError(
            "FABRIC_EVIDENCE_POLICY_INVALID",
            "run_state evidence provenance is invalid",
        )
    return {"run_state": values}


def _fabric_options(
    contract: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], dict[str, tuple[str, ...]]]:
    execution = contract.get("execution")
    if not isinstance(execution, dict) or execution.get("backend") != "fabric":
        raise FabricError(
            "FABRIC_EXECUTION_INVALID",
            "Fabric backend requires execution.backend=fabric",
        )
    options = execution.get("options")
    if not isinstance(options, dict):
        raise FabricError("FABRIC_EXECUTION_INVALID", "Fabric execution options must be an object")
    allowed = {"node", "remote_backend", "logical_workspace", "remote_options", "evidence_provenance"}
    if set(options) - allowed:
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric execution options contain unknown fields")
    node_name = _bounded_string(options.get("node"), field="execution.options.node", pattern=_NODE_RE)
    remote_backend = _bounded_string(
        options.get("remote_backend"),
        field="execution.options.remote_backend",
        pattern=_BACKEND_RE,
    )
    if remote_backend in {"fabric", "fleet"}:
        raise FabricError("FABRIC_EXECUTION_INVALID", "nested Fabric/fleet delegation is not allowed")
    logical_workspace = _bounded_string(
        options.get("logical_workspace"),
        field="execution.options.logical_workspace",
        maximum=128,
    )
    remote_options = _bounded_json(
        options.get("remote_options") or {},
        field="execution.options.remote_options",
    )
    if not isinstance(remote_options, dict):
        raise FabricError("FABRIC_EXECUTION_INVALID", "remote_options must be an object")
    return (
        node_name,
        remote_backend,
        logical_workspace,
        remote_options,
        _evidence_policy(options),
    )


def _contract_sha(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def _dispatch_id(contract_sha: str, task_id: str, node_name: str) -> str:
    return "fabd-" + hashlib.sha256(f"{contract_sha}:{task_id}:{node_name}".encode()).hexdigest()[:32]


def _attempt_id(dispatch_id: str, sequence: int = 1) -> str:
    return "faba-" + hashlib.sha256(f"{dispatch_id}:{sequence}".encode()).hexdigest()[:32]


def _contract_profile_scope(contract: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Fail closed if Fabric placement would widen Work Contract profile scope."""
    scope = contract.get("allowed_scope")
    if not isinstance(scope, dict):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "contract allowed_scope must be an object",
        )
    profiles = tuple(
        _bounded_strings(
            scope.get("profiles"),
            field="contract.allowed_scope.profiles",
            maximum=16,
            item_max=64,
        )
    )
    if not profiles or any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "contract profile scope is invalid",
        )
    assigned_profile = _bounded_string(
        contract.get("assigned_profile"),
        field="contract.assigned_profile",
        pattern=_PROFILE_RE,
    )
    if assigned_profile not in profiles:
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "assigned profile is outside the Work Contract profile scope",
        )
    return assigned_profile, profiles


def _contract_forbidden_actions_present(contract: dict[str, Any]) -> bool:
    """Return whether the contract declares checks Fabric v1 cannot yet prove."""
    forbidden = contract.get("forbidden_actions", [])
    if not isinstance(forbidden, list) or len(forbidden) > 32:
        raise FabricError(
            "FABRIC_SCHEMA_INVALID",
            "contract forbidden_actions must be a bounded list",
        )
    return bool(forbidden)


def _auth_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization must be an object")
    allowed = {"class", "approved", "approved_by", "approval_reference"}
    if set(value) - allowed:
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization contains unknown fields")
    auth_class = value.get("class")
    if auth_class not in _AUTH_RANK or not isinstance(value.get("approved"), bool):
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization is invalid")
    out: dict[str, Any] = {"class": auth_class, "approved": value["approved"]}
    for key in ("approved_by", "approval_reference"):
        if key in value:
            out[key] = _bounded_string(value[key], field=f"authorization.{key}", maximum=128)
    if auth_class == "high_impact" and (
        not out["approved"] or "approved_by" not in out or "approval_reference" not in out
    ):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "high-impact authorization requires explicit approval metadata",
        )
    return out


def _build_envelope(
    contract: dict[str, Any],
    node: FabricNode,
    *,
    remote_backend: str,
    logical_workspace: str,
    remote_options: dict[str, Any],
    evidence_policy: dict[str, tuple[str, ...]],
    capability_sha: str,
) -> dict[str, Any]:
    contract_sha = _contract_sha(contract)
    task_id = _bounded_string(contract.get("task_id"), field="contract.task_id", pattern=op_fleet._TASK_ID_RE)
    dispatch_id = _dispatch_id(contract_sha, task_id, node.name)
    envelope = {
        "schema": DISPATCH_SCHEMA,
        "version": FABRIC_VERSION,
        "dispatch_id": dispatch_id,
        "attempt_id": _attempt_id(dispatch_id),
        "contract_sha256": contract_sha,
        "task_id": task_id,
        "target_node": node.name,
        "coordinator_principal": node.coordinator_principal,
        "assigned_profile": _bounded_string(
            contract.get("assigned_profile"),
            field="contract.assigned_profile",
            pattern=_PROFILE_RE,
        ),
        "objective": _bounded_string(contract.get("objective"), field="contract.objective", maximum=8_000),
        "inputs": _bounded_strings(contract.get("inputs", []), field="contract.inputs"),
        "constraints": _bounded_strings(contract.get("constraints", []), field="contract.constraints"),
        "authorization": _auth_object(contract.get("authorization")),
        "logical_workspace": logical_workspace,
        "remote_backend": remote_backend,
        "remote_options": remote_options,
        "required_features": list(dict.fromkeys((*_DEFAULT_FEATURES, *node.required_features))),
        "capability_snapshot_sha256": capability_sha,
        "evidence_policy": {"run_state": list(evidence_policy["run_state"])},
        "created_at": _now(),
    }
    if len(canonical_json(envelope).encode("utf-8")) > 64_000:
        raise FabricError(
            "FABRIC_PAYLOAD_TOO_LARGE",
            "canonical Fabric dispatch envelope exceeds 64 KB",
        )
    return envelope


def _validate_envelope(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "version",
        "dispatch_id",
        "attempt_id",
        "contract_sha256",
        "task_id",
        "target_node",
        "coordinator_principal",
        "assigned_profile",
        "objective",
        "inputs",
        "constraints",
        "authorization",
        "logical_workspace",
        "remote_backend",
        "remote_options",
        "required_features",
        "capability_snapshot_sha256",
        "evidence_policy",
        "created_at",
    }
    envelope = _closed(value, required=required, name="Fabric dispatch envelope")
    if envelope["schema"] != DISPATCH_SCHEMA or envelope["version"] != FABRIC_VERSION:
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric dispatch schema/version is unsupported",
        )
    _bounded_string(envelope["dispatch_id"], field="dispatch_id", pattern=_ID_RE)
    _bounded_string(envelope["attempt_id"], field="attempt_id", pattern=_ID_RE)
    _bounded_string(envelope["contract_sha256"], field="contract_sha256", pattern=_SHA_RE)
    _bounded_string(envelope["task_id"], field="task_id", pattern=op_fleet._TASK_ID_RE)
    _bounded_string(envelope["target_node"], field="target_node", pattern=_NODE_RE)
    _bounded_string(
        envelope["coordinator_principal"],
        field="coordinator_principal",
        pattern=_PRINCIPAL_RE,
    )
    _bounded_string(envelope["assigned_profile"], field="assigned_profile", pattern=_PROFILE_RE)
    _bounded_string(envelope["objective"], field="objective", maximum=8_000)
    _bounded_strings(envelope["inputs"], field="inputs")
    _bounded_strings(envelope["constraints"], field="constraints")
    _auth_object(envelope["authorization"])
    _bounded_string(envelope["logical_workspace"], field="logical_workspace", maximum=128)
    backend = _bounded_string(envelope["remote_backend"], field="remote_backend", pattern=_BACKEND_RE)
    if backend in {"fabric", "fleet"}:
        raise FabricError("FABRIC_EXECUTION_INVALID", "nested Fabric/fleet delegation is not allowed")
    _bounded_json(envelope["remote_options"], field="remote_options")
    _bounded_strings(
        envelope["required_features"],
        field="required_features",
        maximum=32,
        item_max=128,
    )
    _bounded_string(
        envelope["capability_snapshot_sha256"],
        field="capability_snapshot_sha256",
        pattern=_SHA_RE,
    )
    evidence_policy = _closed(
        envelope["evidence_policy"],
        required={"run_state"},
        name="evidence_policy",
    )
    provenance = _bounded_strings(
        evidence_policy["run_state"],
        field="evidence_policy.run_state",
        maximum=8,
        item_max=64,
    )
    if not provenance or any(item not in _SAFE_EVIDENCE_PROVENANCE for item in provenance):
        raise FabricError(
            "FABRIC_EVIDENCE_POLICY_INVALID",
            "Fabric run-state evidence policy is invalid",
        )
    _bounded_string(envelope["created_at"], field="created_at", maximum=128)
    return envelope


def _request(
    operation: str,
    principal: str,
    *,
    data: dict[str, Any],
    dispatch_id: str = "",
    attempt_id: str = "",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "version": FABRIC_VERSION,
        "operation": operation,
        "coordinator_principal": principal,
        "request_id": "freq-"
        + hashlib.sha256(
            f"{operation}:{dispatch_id}:{attempt_id}:{time.time_ns()}".encode()
        ).hexdigest()[:24],
        "data": data,
    }
    if dispatch_id:
        request["dispatch_id"] = dispatch_id
    if attempt_id:
        request["attempt_id"] = attempt_id
    return request


def _validate_request(value: Any) -> dict[str, Any]:
    request = _closed(
        value,
        required={"schema", "version", "operation", "coordinator_principal", "request_id", "data"},
        optional={"dispatch_id", "attempt_id"},
        name="Fabric request",
    )
    if request["schema"] != REQUEST_SCHEMA or request["version"] != FABRIC_VERSION:
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric request schema/version is unsupported",
        )
    operation = _bounded_string(request["operation"], field="operation", maximum=32)
    if operation not in {"capabilities", "accept", "status", "reconcile", "cancel", "evidence"}:
        raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "Fabric operation is unsupported")
    _bounded_string(
        request["coordinator_principal"],
        field="coordinator_principal",
        pattern=_PRINCIPAL_RE,
    )
    _bounded_string(request["request_id"], field="request_id", pattern=_ID_RE)
    if "dispatch_id" in request:
        _bounded_string(request["dispatch_id"], field="dispatch_id", pattern=_ID_RE)
    if "attempt_id" in request:
        _bounded_string(request["attempt_id"], field="attempt_id", pattern=_ID_RE)
    if not isinstance(request["data"], dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric request data must be an object")
    return request


def _response(operation: str, *, ok: bool, code: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "version": FABRIC_VERSION,
        "operation": operation,
        "ok": ok,
        "code": code,
        "data": data,
    }


def _audit(
    tool: str,
    *,
    success: bool,
    changed: bool,
    summary: str,
    extra: dict[str, Any],
) -> None:
    try:
        policy = op.OperatorPolicy()
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=not changed,
            success=success,
            changed=changed,
            summary=summary[:500],
            extra=extra,
        )
    except _EXPECTED_ERRORS:
        return


def _latest_run(runs: Any) -> dict[str, Any] | None:
    if not isinstance(runs, list):
        return None
    candidates = [run for run in runs if isinstance(run, dict)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda run: (
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or run.get("state") or ""),
        ),
    )


def _validate_evidence(
    value: Any,
    *,
    attempt: Any,
    node: FabricNode,
    allowed_provenance: tuple[str, ...],
) -> dict[str, Any]:
    required = {
        "schema",
        "version",
        "dispatch_id",
        "attempt_id",
        "contract_sha256",
        "task_id",
        "node_name",
        "peer_identity",
        "coordinator_principal",
        "remote_backend",
        "terminal_state",
        "observations",
        "policy_sha256",
        "created_at",
    }
    evidence = _closed(value, required=required, name="Fabric evidence")
    if evidence["schema"] != EVIDENCE_SCHEMA or evidence["version"] != FABRIC_VERSION:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence schema/version is invalid")

    exact = {
        "dispatch_id": attempt["dispatch_id"],
        "attempt_id": attempt["attempt_id"],
        "node_name": node.name,
        "peer_identity": node.expected_identity,
        "coordinator_principal": node.coordinator_principal,
        "remote_backend": attempt["remote_backend"],
    }
    for key, expected in exact.items():
        if evidence.get(key) != expected:
            raise FabricError(
                "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
                f"evidence {key} does not match the admitted attempt",
            )

    coordinator_db = Path(attempt["_coordinator_db"])
    if not coordinator_db.is_file():
        raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
    with _connect_readonly(coordinator_db) as db:
        dispatch = db.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?",
            (attempt["dispatch_id"],),
        ).fetchone()
    if (
        dispatch is None
        or evidence.get("contract_sha256") != dispatch["contract_sha256"]
        or evidence.get("task_id") != dispatch["task_id"]
    ):
        raise FabricError(
            "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
            "evidence contract/task lineage does not match",
        )
    if attempt.get("peer_policy_sha256") and evidence.get("policy_sha256") != attempt.get(
        "peer_policy_sha256"
    ):
        raise FabricError(
            "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
            "evidence peer policy digest does not match accepted attempt",
        )
    if evidence["terminal_state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence is not terminal")

    observations = evidence["observations"]
    if not isinstance(observations, list) or len(observations) > _MAX_ITEMS:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence observations are invalid")
    clean: list[dict[str, Any]] = []
    for observation in observations:
        observation = _closed(
            observation,
            required={"kind", "provenance", "state", "outcome", "started_at", "ended_at", "error", "source"},
            name="evidence observation",
        )
        if observation["kind"] != "run_state":
            raise FabricError(
                "FABRIC_EVIDENCE_REJECTED",
                "G4-A accepts only run_state remote observations",
            )
        provenance = _bounded_string(
            observation["provenance"],
            field="observation.provenance",
            maximum=64,
        )
        if provenance not in allowed_provenance or provenance == "worker_statement":
            raise FabricError(
                "FABRIC_EVIDENCE_PROVENANCE_REJECTED",
                "evidence provenance is not allowed for the check",
            )
        clean.append(
            {
                "kind": "run_state",
                "provenance": provenance,
                "state": _bounded_string(observation["state"], field="observation.state", maximum=1_000, required=False),
                "outcome": _bounded_string(observation["outcome"], field="observation.outcome", maximum=1_000, required=False),
                "started_at": _bounded_string(observation["started_at"], field="observation.started_at", maximum=1_000, required=False),
                "ended_at": _bounded_string(observation["ended_at"], field="observation.ended_at", maximum=1_000, required=False),
                "error": _bounded_string(observation["error"], field="observation.error", maximum=1_000, required=False),
                "source": _bounded_string(observation["source"], field="observation.source", maximum=1_000, required=False),
            }
        )
    admitted = dict(evidence)
    admitted["observations"] = clean
    return admitted
