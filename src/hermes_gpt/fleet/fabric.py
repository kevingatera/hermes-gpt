"""Public compatibility surface for Hermes GPT A2A Fabric."""

from __future__ import annotations

from hermes_gpt.execution import runners as op_runners

# Existing integrations import implementation helpers from this module.
# Keep these names available while their implementations live in focused modules.
# ruff: noqa: F401
from hermes_gpt.fleet import fabric_config, fabric_store, fabric_transport
from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.fleet.fabric_backend import FabricBackend, register_runner_backend
from hermes_gpt.fleet.fabric_coordinator import FabricCoordinator
from hermes_gpt.fleet.fabric_core import (
    _AUTH_RANK,
    _DEFAULT_FEATURES,
    _EXPECTED_ERRORS,
    _ID_RE,
    _MAX_BODY,
    _PEER_EXECUTION_UNIT_STATES,
    _PEER_WRITE_CLAIM_STATES,
    _PRINCIPAL_RE,
    _SAFE_EVIDENCE_PROVENANCE,
    _SHA_RE,
    _TERMINAL_COORD,
    _TERMINAL_PEER,
    CAPABILITY_SCHEMA,
    COORDINATOR_DB_ENV,
    DISPATCH_SCHEMA,
    EVIDENCE_SCHEMA,
    FABRIC_VERSION,
    NODE_REGISTRY_ENV,
    NODE_REGISTRY_SCHEMA,
    PEER_DB_ENV,
    PEER_POLICY_ENV,
    PEER_POLICY_SCHEMA,
    PEER_TOKENS_ENV,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    FabricError,
    FabricNode,
    FabricPeerPolicy,
    WorkspaceMapping,
    _attempt_id,
    _audit,
    _auth_object,
    _bounded_coordinator_peer_values,
    _bounded_json,
    _bounded_string,
    _bounded_strings,
    _build_envelope,
    _closed,
    _config_path,
    _connect,
    _connect_readonly,
    _contract_forbidden_actions_present,
    _contract_profile_scope,
    _contract_sha,
    _db_path,
    _dispatch_id,
    _evidence_policy,
    _extract_data_response,
    _fabric_card,
    _fabric_options,
    _http_json,
    _init_coordinator_db,
    _init_peer_db,
    _latest_run,
    _now,
    _peer_entry,
    _prepare_db_parent,
    _read_closed_json,
    _request,
    _require_secure_transport,
    _response,
    _root,
    _rpc_call,
    _validate_envelope,
    _validate_evidence,
    _validate_request,
    _validate_response,
    canonical_json,
    load_node_registry,
    load_peer_policy,
    load_peer_tokens,
    sha256_json,
    strict_json_loads,
)
from hermes_gpt.fleet.fabric_peer_service import FabricPeerService
from hermes_gpt.fleet.fabric_protocol import (
    _BACKEND_RE,
    _MAX_ITEMS,
    _NODE_RE,
    _PROFILE_RE,
)
from hermes_gpt.policy import authorization as op


def peer_main(argv: list[str] | None = None) -> None:
    from hermes_gpt.fleet.fabric_peer_http import peer_main as run_peer_http

    run_peer_http(argv)


__all__ = [
    "FabricBackend",
    "FabricCoordinator",
    "FabricError",
    "FabricNode",
    "FabricPeerPolicy",
    "FabricPeerService",
    "WorkspaceMapping",
    "canonical_json",
    "load_node_registry",
    "load_peer_policy",
    "load_peer_tokens",
    "peer_main",
    "register_runner_backend",
    "sha256_json",
    "strict_json_loads",
]

if __name__ == "__main__":
    peer_main()
