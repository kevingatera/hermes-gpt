"""Fabric peer, node, and credential configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.fleet.fabric_protocol import (
    _BACKEND_RE,
    _MAX_BODY,
    _NODE_RE,
    _PRINCIPAL_RE,
    _PROFILE_RE,
    FabricError,
    _bounded_string,
    _bounded_strings,
    _closed,
    sha256_json,
    strict_json_loads,
)
from hermes_gpt.policy import authorization as op

NODE_REGISTRY_SCHEMA = "hermes.fabric-node-registry/v1"
PEER_POLICY_SCHEMA = "hermes.fabric-peer-policy/v1"
NODE_REGISTRY_ENV = "HERMES_GPT_FABRIC_NODE_REGISTRY"
PEER_POLICY_ENV = "HERMES_GPT_FABRIC_PEER_POLICY"
PEER_TOKENS_ENV = "HERMES_GPT_FABRIC_PEER_TOKENS"

_AUTH_RANK = {"none": 0, "read_only": 1, "reversible_write": 2, "high_impact": 3}


@dataclass(frozen=True)
class FabricNode:
    name: str
    a2a_peer_name: str
    expected_identity: str
    coordinator_principal: str
    enabled: bool
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allowed_remote_backends: tuple[str, ...]
    logical_workspaces: tuple[str, ...]
    required_features: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceMapping:
    logical_id: str
    local_path: Path
    revision: str
    conflict_domain: str


@dataclass(frozen=True)
class FabricPeerPolicy:
    node_name: str
    identity: str
    allowed_coordinator_principals: tuple[str, ...]
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allowed_backends: tuple[str, ...]
    required_features: tuple[str, ...]
    workspace_mappings: dict[str, WorkspaceMapping]
    digest: str


def _root(hermes_root: Path | None = None) -> Path:
    configured = hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    normalized = op.normalize_hermes_data_root(configured)
    return Path(normalized or Path.home() / ".hermes")


def _config_path(env_name: str, default_name: str, hermes_root: Path | None = None) -> Path:
    configured = os.environ.get(env_name, "").strip()
    path = Path(configured).expanduser() if configured else _root(hermes_root) / "config" / default_name
    if not path.is_absolute():
        raise FabricError("FABRIC_CONFIG_INVALID", f"{env_name} must resolve to an absolute path")
    if op.is_denied_path(path) or path.is_symlink():
        raise FabricError("FABRIC_CONFIG_INVALID", "Fabric configuration path is not allowed")
    return path


def _read_closed_json(path: Path, *, maximum: int = _MAX_BODY) -> dict[str, Any]:
    if op.is_denied_path(path) or path.is_symlink():
        raise FabricError("FABRIC_CONFIG_INVALID", "Fabric configuration path is not allowed")
    try:
        # Read one byte beyond the parser limit to detect oversized files
        # without first loading the entire file into memory.
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
    except FileNotFoundError as exc:
        raise FabricError("FABRIC_CONFIG_MISSING", f"Fabric configuration is missing: {path.name}") from exc
    value = strict_json_loads(raw, maximum=maximum)
    if not isinstance(value, dict):
        raise FabricError("FABRIC_CONFIG_INVALID", "Fabric configuration must be an object")
    return value


def load_node_registry(
    path: Path | None = None,
    *,
    hermes_root: Path | None = None,
) -> dict[str, FabricNode]:
    path = path or _config_path(NODE_REGISTRY_ENV, "fabric-nodes.json", hermes_root)
    raw = _read_closed_json(path)
    _closed(raw, required={"schema", "version", "nodes"}, name="node registry")
    if raw["schema"] != NODE_REGISTRY_SCHEMA or raw["version"] != 1 or not isinstance(raw["nodes"], list):
        raise FabricError("FABRIC_CONFIG_INVALID", "node registry schema/version is invalid")
    if len(raw["nodes"]) > 128:
        raise FabricError("FABRIC_CONFIG_INVALID", "node registry contains too many nodes")

    result: dict[str, FabricNode] = {}
    for item in raw["nodes"]:
        item = _closed(
            item,
            required={
                "name",
                "a2a_peer_name",
                "expected_identity",
                "coordinator_principal",
                "enabled",
                "allowed_profiles",
                "max_authorization",
                "allowed_remote_backends",
                "logical_workspaces",
                "required_features",
            },
            name="node",
        )
        name = _bounded_string(item["name"], field="node.name", pattern=_NODE_RE)
        if name in result:
            raise FabricError("FABRIC_CONFIG_INVALID", "node names must be unique")
        peer_name = _bounded_string(
            item["a2a_peer_name"],
            field="node.a2a_peer_name",
            pattern=op_fleet._AGENT_RE,
        )
        identity = _bounded_string(item["expected_identity"], field="node.expected_identity", maximum=128)
        principal = _bounded_string(
            item["coordinator_principal"],
            field="node.coordinator_principal",
            pattern=_PRINCIPAL_RE,
        )
        if not isinstance(item["enabled"], bool):
            raise FabricError("FABRIC_CONFIG_INVALID", "node.enabled must be boolean")
        profiles = tuple(
            _bounded_strings(item["allowed_profiles"], field="node.allowed_profiles", maximum=32, item_max=64)
        )
        if any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
            raise FabricError("FABRIC_CONFIG_INVALID", "node allowed profile is invalid")
        max_auth = _bounded_string(item["max_authorization"], field="node.max_authorization", maximum=32)
        if max_auth not in _AUTH_RANK:
            raise FabricError("FABRIC_CONFIG_INVALID", "node max_authorization is invalid")
        backends = tuple(
            _bounded_strings(
                item["allowed_remote_backends"],
                field="node.allowed_remote_backends",
                maximum=32,
                item_max=64,
            )
        )
        if any(not _BACKEND_RE.fullmatch(backend) or backend in {"fabric", "fleet"} for backend in backends):
            raise FabricError("FABRIC_CONFIG_INVALID", "node remote backend allowlist is invalid")
        workspaces = tuple(
            _bounded_strings(
                item["logical_workspaces"],
                field="node.logical_workspaces",
                maximum=64,
                item_max=128,
            )
        )
        features = tuple(
            _bounded_strings(
                item["required_features"],
                field="node.required_features",
                maximum=32,
                item_max=128,
            )
        )
        result[name] = FabricNode(
            name,
            peer_name,
            identity,
            principal,
            item["enabled"],
            profiles,
            max_auth,
            backends,
            workspaces,
            features,
        )
    return result


def load_peer_policy(path: Path | None = None, *, hermes_root: Path | None = None) -> FabricPeerPolicy:
    path = path or _config_path(PEER_POLICY_ENV, "fabric-peer-policy.json", hermes_root)
    raw = _read_closed_json(path)
    _closed(
        raw,
        required={
            "schema",
            "version",
            "node_name",
            "identity",
            "allowed_coordinator_principals",
            "allowed_profiles",
            "max_authorization",
            "allowed_backends",
            "required_features",
            "workspace_mappings",
        },
        name="peer policy",
    )
    if raw["schema"] != PEER_POLICY_SCHEMA or raw["version"] != 1:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer policy schema/version is invalid")

    node_name = _bounded_string(raw["node_name"], field="peer.node_name", pattern=_NODE_RE)
    identity = _bounded_string(raw["identity"], field="peer.identity", maximum=128)
    principals = tuple(
        _bounded_strings(
            raw["allowed_coordinator_principals"],
            field="peer.allowed_coordinator_principals",
            maximum=32,
            item_max=128,
        )
    )
    if not principals or any(not _PRINCIPAL_RE.fullmatch(principal) for principal in principals):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer principals are invalid")
    profiles = tuple(
        _bounded_strings(raw["allowed_profiles"], field="peer.allowed_profiles", maximum=32, item_max=64)
    )
    if not profiles or any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer profiles are invalid")
    max_auth = _bounded_string(raw["max_authorization"], field="peer.max_authorization", maximum=32)
    if max_auth not in _AUTH_RANK:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer max_authorization is invalid")
    backends = tuple(
        _bounded_strings(raw["allowed_backends"], field="peer.allowed_backends", maximum=32, item_max=64)
    )
    if not backends or any(
        not _BACKEND_RE.fullmatch(backend) or backend in {"fabric", "fleet"}
        for backend in backends
    ):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer backend allowlist is invalid")
    features = tuple(
        _bounded_strings(raw["required_features"], field="peer.required_features", maximum=32, item_max=128)
    )

    mappings_raw = raw["workspace_mappings"]
    if not isinstance(mappings_raw, dict) or not mappings_raw or len(mappings_raw) > 64:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer workspace mappings are invalid")
    mappings: dict[str, WorkspaceMapping] = {}
    for logical_id, mapping in mappings_raw.items():
        logical_id = _bounded_string(logical_id, field="workspace logical id", maximum=128)
        mapping = _closed(
            mapping,
            required={"local_path", "revision", "conflict_domain"},
            name="workspace mapping",
        )
        local_path_text = _bounded_string(mapping["local_path"], field="workspace.local_path", maximum=1_000)
        local_path = Path(local_path_text).expanduser()
        if not local_path.is_absolute() or op.is_denied_path(local_path):
            raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping path is not allowed")
        try:
            if local_path.is_symlink():
                raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping root may not be a symlink")
        except OSError as exc:
            raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping cannot be inspected") from exc
        mappings[logical_id] = WorkspaceMapping(
            logical_id,
            local_path.resolve(),
            _bounded_string(mapping["revision"], field="workspace.revision", maximum=128),
            _bounded_string(mapping["conflict_domain"], field="workspace.conflict_domain", maximum=128),
        )

    return FabricPeerPolicy(
        node_name,
        identity,
        principals,
        profiles,
        max_auth,
        backends,
        features,
        mappings,
        sha256_json(raw),
    )


def load_peer_tokens(value: str | None = None) -> dict[str, str]:
    raw_value = value if value is not None else os.environ.get(PEER_TOKENS_ENV, "")
    if not raw_value:
        raise FabricError(
            "FABRIC_PRINCIPAL_CONFIG_MISSING",
            "verified Fabric requires configured coordinator-principal tokens",
        )
    raw = strict_json_loads(raw_value, maximum=32_000)
    if not isinstance(raw, dict) or not raw or len(raw) > 32:
        raise FabricError("FABRIC_PRINCIPAL_CONFIG_INVALID", "peer token map is invalid")

    out: dict[str, str] = {}
    seen_tokens: set[str] = set()
    for principal, token in raw.items():
        principal = _bounded_string(principal, field="coordinator principal", pattern=_PRINCIPAL_RE)
        if not isinstance(token, str) or len(token) < 16 or len(token.encode("utf-8")) > 1_024:
            raise FabricError("FABRIC_PRINCIPAL_CONFIG_INVALID", "peer bearer token is invalid")
        if token in seen_tokens:
            raise FabricError(
                "FABRIC_PRINCIPAL_CONFIG_INVALID",
                "each coordinator principal must have a unique bearer token",
            )
        out[principal] = token
        seen_tokens.add(token)
    return out


__all__ = [
    "NODE_REGISTRY_ENV",
    "NODE_REGISTRY_SCHEMA",
    "PEER_POLICY_ENV",
    "PEER_POLICY_SCHEMA",
    "PEER_TOKENS_ENV",
    "FabricNode",
    "FabricPeerPolicy",
    "WorkspaceMapping",
    "load_node_registry",
    "load_peer_policy",
    "load_peer_tokens",
]
