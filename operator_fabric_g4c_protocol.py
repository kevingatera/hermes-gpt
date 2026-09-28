"""Validation helpers for the G4-C Fabric request and peer observation shapes."""

from __future__ import annotations

from typing import Any

import operator_fabric as base

FabricError = base.FabricError

_WRITE_CLAIM_STATES = frozenset({"NONE", "ACTIVE", "RELEASED", "SUPERSEDED", "UNKNOWN"})
_EXECUTION_UNIT_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "reloading",
        "inactive",
        "failed",
        "dead",
        "not-found",
        "terminal",
        "unknown",
    }
)


def _validate_request(value: Any) -> dict[str, Any]:
    request = base._closed(
        value,
        required={
            "schema",
            "version",
            "operation",
            "coordinator_principal",
            "request_id",
            "data",
        },
        optional={"dispatch_id", "attempt_id"},
        name="Fabric request",
    )
    if (
        request["schema"] != base.REQUEST_SCHEMA
        or request["version"] != base.FABRIC_VERSION
    ):
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric request schema/version is unsupported",
        )
    operation = base._bounded_string(
        request["operation"], field="operation", maximum=32
    )
    if operation not in {
        "capabilities",
        "accept",
        "status",
        "reconcile",
        "cancel",
        "evidence",
        "artifact_manifest",
        "artifact_chunk",
    }:
        raise FabricError(
            "FABRIC_OPERATION_UNSUPPORTED", "Fabric operation is unsupported"
        )
    base._bounded_string(
        request["coordinator_principal"],
        field="coordinator_principal",
        pattern=base._PRINCIPAL_RE,
    )
    base._bounded_string(request["request_id"], field="request_id", pattern=base._ID_RE)
    if "dispatch_id" in request:
        base._bounded_string(
            request["dispatch_id"], field="dispatch_id", pattern=base._ID_RE
        )
    if "attempt_id" in request:
        base._bounded_string(
            request["attempt_id"], field="attempt_id", pattern=base._ID_RE
        )
    if not isinstance(request["data"], dict):
        raise FabricError(
            "FABRIC_SCHEMA_INVALID", "Fabric request data must be an object"
        )
    return request


def _validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FabricError(
            "FABRIC_SCHEMA_INVALID", "Fabric dispatch envelope must be an object"
        )
    normalized = dict(value)
    retry_parent = normalized.pop("retry_parent_attempt_id", None)
    envelope = base._validate_envelope(normalized)
    if retry_parent is not None:
        envelope["retry_parent_attempt_id"] = base._bounded_string(
            retry_parent,
            field="retry_parent_attempt_id",
            pattern=base._ID_RE,
        )
    return envelope


def _run_state(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    return str(run.get("state") or run.get("status") or "").lower()


def _terminal_state(run: dict[str, Any] | None) -> str:
    state = _run_state(run)
    if state in {"completed", "succeeded", "success"}:
        return "SUCCEEDED"
    if state in {"failed", "error"}:
        return "FAILED"
    if state in {"cancelled", "canceled"}:
        return "CANCELLED"
    return ""


def _bounded_peer_observation(key: str, value: Any) -> str:
    allowed = (
        _WRITE_CLAIM_STATES if key == "write_claim_state" else _EXECUTION_UNIT_STATES
    )
    text = str(value or "")
    return (
        text
        if text in allowed
        else "UNKNOWN"
        if key == "write_claim_state"
        else "unknown"
    )
