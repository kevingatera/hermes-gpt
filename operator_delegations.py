"""Unified durable delegation lifecycle for Hermes GPT v0.9.

Delegations are orchestration records, not execution authorities. The canonical
Work Contract and its selected runner/Fabric backend remain authoritative for
scope, mutation gates, dispatch, cancellation, and completion evidence.

This store deliberately persists no objective/prompt/transcript. It records only
bounded lineage and normalized lifecycle metadata so Missions and clients can
observe Pi, OpenCode, Codex, Fleet/Fabric, and future runner backends uniformly.
"""

from __future__ import annotations

import operator_contract as _contract_mod
import operator_delegation_cancel as _cancel_module
import operator_delegation_dispatch as _dispatch_module
import operator_delegation_lifecycle as delegation_lifecycle
import operator_delegation_queries as _queries_module
import operator_delegation_reconcile as _reconcile_module
import operator_delegation_store as delegation_store
import operator_mission_runtime as _mission_runtime
import operator_policy as _policy
import operator_runners as _runners

# Retain established module attributes while keeping each write path separate.
contract_mod = _contract_mod
mission_runtime = _mission_runtime
op = _policy
runners = _runners
hermes_delegation_dispatch = _dispatch_module.hermes_delegation_dispatch
hermes_delegation_get = _queries_module.hermes_delegation_get
hermes_delegation_list = _queries_module.hermes_delegation_list
hermes_delegation_reconcile = _reconcile_module.hermes_delegation_reconcile
hermes_delegation_cancel = _cancel_module.hermes_delegation_cancel
SCHEMA_VERSION = delegation_store.SCHEMA_VERSION
DELEGATION_SCHEMA = delegation_store.DELEGATION_SCHEMA
DELEGATION_ID_RE = delegation_store.DELEGATION_ID_RE
STATES = delegation_store.STATES
DISPATCH_PHASES = delegation_store.DISPATCH_PHASES
TERMINAL_STATES = delegation_store.TERMINAL_STATES
MAX_LIST = delegation_store.MAX_LIST
_now = delegation_store._now
_root = delegation_store._root
_db_path = delegation_store._db_path
_connect = delegation_store._connect
_init = delegation_store._init
_bounded = delegation_store._bounded
_new_id = delegation_store._new_id
_normalize_state = delegation_store._normalize_state
_backend_ref = delegation_store._backend_ref
_surface = delegation_store._surface
_event = delegation_store._event
_live_event = delegation_store._live_event
_audit = delegation_store._audit
_error = delegation_store._error
_get_row = delegation_store._get_row
_manifest_row = delegation_store._manifest_row
_ensure_mission = delegation_lifecycle._ensure_mission
_mission_state = delegation_lifecycle._mission_state
_sync_mission_attachment = delegation_lifecycle._sync_mission_attachment
mission_completion_guard = delegation_lifecycle.mission_completion_guard
mission_cancellation_guard = delegation_lifecycle.mission_cancellation_guard
_mission_sync_failure = delegation_lifecycle._mission_sync_failure
_dispatch_in_progress = delegation_lifecycle._dispatch_in_progress
_dispatch_cancelled = delegation_lifecycle._dispatch_cancelled
_cancellation_in_progress = delegation_lifecycle._cancellation_in_progress
_dispatch_cas_lost = delegation_lifecycle._dispatch_cas_lost
_reserved_cancel_cas_lost = delegation_lifecycle._reserved_cancel_cas_lost
_dispatched_cancel_cas_lost = delegation_lifecycle._dispatched_cancel_cas_lost
_latest_observation = delegation_lifecycle._latest_observation
_observation_sha256 = delegation_lifecycle._observation_sha256
_observation_is_fresh_for_cancellation = (
    delegation_lifecycle._observation_is_fresh_for_cancellation
)
_mission_dispatch_guard = delegation_lifecycle._mission_dispatch_guard
