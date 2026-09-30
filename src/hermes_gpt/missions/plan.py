"""Stable import facade for MissionPlan validation, storage, and MCP tools.

Implementation lives in focused modules so consumers can keep importing this
module while schema rules, persistence, and tool behavior evolve independently.
"""

from __future__ import annotations

from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op

# Re-export the historical names because controllers and integrations import
# constants and a small number of helpers from this module directly.
from hermes_gpt.missions.plan_schema import AUTH_CLASSES, KIND_APPROVAL, KIND_PARALLEL, KIND_SINGLE, KINDS, MAX_ARTIFACT_BASENAME, MAX_ARTIFACTS, MAX_BUDGET_MINUTES, MAX_BUDGET_TOKENS, MAX_DECOMP, MAX_DEPTH_DEPENDS, MAX_NODES, MAX_OBJECTIVE_BYTES, MAX_SKILL_NAME, MAX_SKILLS, MISSION_ID_RE, NODE_ID_RE, NODE_SCHEMA, NODE_STATES, NODE_TRANSITIONS, PLAN_SCHEMA, PLAN_STATUS_APPROVED, PLAN_STATUS_DRAFT, PLAN_STATUS_REJECTED, PLAN_STATUS_REVIEW, PLAN_STATUSES, REF_RE, SCHEMA_VERSION, SHA_RE, TERMINAL_NODE_STATES, _canonical_node, _canonical_plan, _clean_artifacts, _clean_budget, _clean_capability_req, _clean_profile, _clean_skills, _clean_text, _default_artifacts, _default_budget, _node_contract_signature, _objective_meta, _parse_plan, _plan_sha256, _validate_node_dag, decompose_mission, validate_node_transition
from hermes_gpt.missions.plan_store import _audit, _begin_write, _connect, _db_path, _error, _get_plan_row, _init_plan_tables, _nodes_rows, _now, _plan_view
from hermes_gpt.missions.plan_tools import _read_spec, _ready_node_ids, _topological_order, hermes_plan_create, hermes_plan_decompose, hermes_plan_get, hermes_plan_list, hermes_plan_node_transition, hermes_plan_review, hermes_plan_set_status, hermes_plan_validate

__all__ = (
    "AUTH_CLASSES",
    "KINDS",
    "KIND_APPROVAL",
    "KIND_PARALLEL",
    "KIND_SINGLE",
    "MAX_ARTIFACTS",
    "MAX_ARTIFACT_BASENAME",
    "MAX_BUDGET_MINUTES",
    "MAX_BUDGET_TOKENS",
    "MAX_DECOMP",
    "MAX_DEPTH_DEPENDS",
    "MAX_NODES",
    "MAX_OBJECTIVE_BYTES",
    "MAX_SKILLS",
    "MAX_SKILL_NAME",
    "MISSION_ID_RE",
    "NODE_ID_RE",
    "NODE_SCHEMA",
    "NODE_STATES",
    "NODE_TRANSITIONS",
    "PLAN_SCHEMA",
    "PLAN_STATUSES",
    "PLAN_STATUS_APPROVED",
    "PLAN_STATUS_DRAFT",
    "PLAN_STATUS_REJECTED",
    "PLAN_STATUS_REVIEW",
    "REF_RE",
    "SCHEMA_VERSION",
    "SHA_RE",
    "TERMINAL_NODE_STATES",
    "_audit",
    "_begin_write",
    "_canonical_node",
    "_canonical_plan",
    "_clean_artifacts",
    "_clean_budget",
    "_clean_capability_req",
    "_clean_profile",
    "_clean_skills",
    "_clean_text",
    "_connect",
    "_db_path",
    "_default_artifacts",
    "_default_budget",
    "_error",
    "_get_plan_row",
    "_init_plan_tables",
    "_node_contract_signature",
    "_nodes_rows",
    "_now",
    "_objective_meta",
    "_parse_plan",
    "_plan_sha256",
    "_plan_view",
    "_read_spec",
    "_ready_node_ids",
    "_topological_order",
    "_validate_node_dag",
    "decompose_mission",
    "hermes_plan_create",
    "hermes_plan_decompose",
    "hermes_plan_get",
    "hermes_plan_list",
    "hermes_plan_node_transition",
    "hermes_plan_review",
    "hermes_plan_set_status",
    "hermes_plan_validate",
    "mission",
    "op",
    "validate_node_transition",
)
