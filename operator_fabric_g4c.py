"""Public compatibility surface for the G4-C Fabric runtime."""

from __future__ import annotations

# Preserve established names while keeping each runtime responsibility separate.
# ruff: noqa: F401
import argparse
import hashlib
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import fabric_artifacts as artifacts
import fabric_write_guard as write_guard
import operator_fabric as base
import operator_fabric_g4c_peer_write as peer_write
import operator_fabric_g4c_protocol as protocol
import operator_fabric_peer_http as peer_http
import operator_fabric_router as router
import operator_runners as runners
from operator_fabric_g4c_coordinator import FabricCoordinator
from operator_fabric_g4c_peer_server import _peer_handler_class, peer_main
from operator_fabric_g4c_peer_service import FEATURE_RECONCILE, FabricPeerService
from operator_fabric_g4c_router import AutoRouter, register_runtime

ARTIFACT_MANIFEST_SCHEMA = artifacts.ARTIFACT_MANIFEST_SCHEMA
ARTIFACT_CHUNK_SCHEMA = artifacts.ARTIFACT_CHUNK_SCHEMA
FEATURE_ARTIFACT = artifacts.FEATURE_ARTIFACT
FEATURE_ARTIFACT_SNAPSHOT = artifacts.FEATURE_ARTIFACT_SNAPSHOT
FEATURE_WRITE_OWNERSHIP = write_guard.FEATURE_WRITE_OWNERSHIP
FEATURE_EXECUTION_UNIT = write_guard.FEATURE_EXECUTION_UNIT
FEATURE_WRITE_EPOCH = write_guard.FEATURE_WRITE_EPOCH
FabricError = base.FabricError
FabricNode = base.FabricNode
FabricPeerPolicy = base.FabricPeerPolicy
SystemdUserUnitManager = write_guard.SystemdUserUnitManager
WorkspaceMapping = base.WorkspaceMapping
canonical_json = base.canonical_json
sha256_json = base.sha256_json
strict_json_loads = base.strict_json_loads
load_node_registry = base.load_node_registry
load_peer_policy = base.load_peer_policy
load_peer_tokens = base.load_peer_tokens
_logical_artifact_name = artifacts.logical_name
_validate_request = protocol._validate_request
_validate_envelope = protocol._validate_envelope
_run_state = protocol._run_state
_terminal_state = protocol._terminal_state
_bounded_peer_observation = protocol._bounded_peer_observation


__all__ = [
    "ARTIFACT_CHUNK_SCHEMA",
    "ARTIFACT_MANIFEST_SCHEMA",
    "FEATURE_ARTIFACT",
    "FEATURE_ARTIFACT_SNAPSHOT",
    "FEATURE_EXECUTION_UNIT",
    "FEATURE_RECONCILE",
    "FEATURE_WRITE_EPOCH",
    "FEATURE_WRITE_OWNERSHIP",
    "AutoRouter",
    "FabricCoordinator",
    "FabricError",
    "FabricNode",
    "FabricPeerPolicy",
    "FabricPeerService",
    "SystemdUserUnitManager",
    "WorkspaceMapping",
    "canonical_json",
    "load_node_registry",
    "load_peer_policy",
    "load_peer_tokens",
    "peer_main",
    "register_runtime",
    "sha256_json",
    "strict_json_loads",
]


if __name__ == "__main__":
    peer_main()
