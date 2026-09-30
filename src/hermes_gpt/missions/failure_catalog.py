"""Failure classes, recovery rows, and bounded vocabulary constants."""

from __future__ import annotations

import re
from typing import Any

from hermes_gpt.missions import runtime as mission

SCHEMA_VERSION = "0.9-failure-semantics.1"
DECISION_SCHEMA = "hermes.failure-decision/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# §11.1 taxonomy — the 8 semantic failure classes (authoritative, Ops model)
# ---------------------------------------------------------------------------

CLASS_TRANSIENT = "transient"
CLASS_WAITING = "waiting"
CLASS_SEMANTIC = "semantic_failure"
CLASS_AUTHORITY = "authority"
CLASS_CAPABILITY = "capability"
CLASS_ENVIRONMENT = "environment"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_TERMINAL = "cancelled"  # cancelled / operator-terminal: no action

FAILURE_CLASSES = (
    CLASS_TRANSIENT,
    CLASS_WAITING,
    CLASS_SEMANTIC,
    CLASS_AUTHORITY,
    CLASS_CAPABILITY,
    CLASS_ENVIRONMENT,
    CLASS_AMBIGUOUS,
    CLASS_TERMINAL,
)

# Non-failure classifications (recovery-matrix progress rows, §11.2).
CLASS_NONE_DISPATCHABLE = "none_dispatchable"  # ready child waiting for dispatch
CLASS_NONE_APPROVAL = "none_awaiting_approval"  # all terminal, approval required
CLASS_NONE_COMPLETION = "none_completion"  # all terminal, verified, no approval
CLASS_NONE = "none"  # healthy in-flight / steady state

CLASS_UNKNOWN = "unknown"  # fail-closed bucket (not one of the 8)

TAXONOMY: dict[str, dict[str, Any]] = {
    CLASS_TRANSIENT: {
        "meaning": "rate_limit, provider 5xx/429 throughput, transient fleet/network",
        "action": "backoff + retry (base 30s, cap 15min, jitter; breaker respected)",
        "auto_retry": True,
    },
    CLASS_WAITING: {
        "meaning": "parent not done, approval pending, child not terminal, provider replenishing",
        "action": "wait; no retry; recheck on next trigger",
        "auto_retry": False,
    },
    CLASS_SEMANTIC: {
        "meaning": "implementation/QA defect, test failure, forbidden action, artifact mismatch, protocol violation",
        "action": "escalate; bounded replan proposal (D8, <=1 default); route to Developer",
        "auto_retry": False,
    },
    CLASS_AUTHORITY: {
        "meaning": "policy deny, confirm/approval gate, auth/quota/billing credential blocker",
        "action": "park blocked, escalate to owner; never auto-retry",
        "auto_retry": False,
    },
    CLASS_CAPABILITY: {
        "meaning": "no capable node, missing skill, node not enrolled, backend unsupported",
        "action": "park blocked, escalate; no auto-retry",
        "auto_retry": False,
    },
    CLASS_ENVIRONMENT: {
        "meaning": "missing workspace, unreadable store, corrupt manifest, stale lease",
        "action": "fail-closed to reconciling/blocked; recover around or escalate",
        "auto_retry": False,
    },
    CLASS_AMBIGUOUS: {
        "meaning": "backend accepted but outcome unknown (submission_may_have_succeeded), reconciling",
        "action": "observe + report; never auto-redispatch; do not fabricate",
        "auto_retry": False,
    },
    CLASS_TERMINAL: {
        "meaning": "cancelled / operator-terminal state",
        "action": "terminal; no action",
        "auto_retry": False,
    },
}

# ---------------------------------------------------------------------------
# §11.2 recovery matrix — deterministic smallest-first lookup.
# Every action is a *proposed request*; nothing here executes (would_execute
# is always False). "smallest" ordering is embodied in the ladder + rows.
# ---------------------------------------------------------------------------

MATRIX: dict[str, dict[str, Any]] = {
    "dispatch_ready_child": {
        "observed": "mission running, child pending, parent ready",
        "smallest_action": "dispatch ready child",
        "proposed_tool": "hermes_swarm_stage_dispatch | hermes_contract_dispatch",
        "verify": "deployment durable (delegation dispatched, task running)",
        "auto_retry": False,
    },
    "reclaim_dead_worker": {
        "observed": "child running, worker dead",
        "smallest_action": "reclaim + bounded retry",
        "proposed_tool": "host kanban reclaim_task",
        "verify": "task back in ready/blocked; no duplicate spawn",
        "auto_retry": True,
    },
    "observe_reconciling": {
        "observed": "child reconciling / ambiguous backend outcome",
        "smallest_action": "observe + report; do NOT redispatch",
        "proposed_tool": "hermes_delegation_reconcile",
        "verify": "resolves or escalates; never auto-redispatched",
        "auto_retry": False,
    },
    "retry_transient_backoff": {
        "observed": "child failed (transient)",
        "smallest_action": "backoff + retry",
        "proposed_tool": "dispatcher retry (breaker respected)",
        "verify": "run reaches terminal",
        "auto_retry": True,
    },
    "breaker_exhausted": {
        "observed": "retry ceiling reached (consecutive_failures/failure_limit, gave_up)",
        "smallest_action": "blocked + gave_up + human signal; no further retry",
        "proposed_tool": "controller signal (broker) + operator",
        "verify": "human action; breaker state recorded",
        "auto_retry": False,
    },
    "escalate_semantic": {
        "observed": "child failed (semantic)",
        "smallest_action": "escalate + bounded replan proposal (D8)",
        "proposed_tool": "hermes_swarm_stage_advance rework | hermes_plan_decompose | Developer card",
        "verify": "plan revised only through decompose/advance; evidence attached",
        "auto_retry": False,
    },
    "signal_awaiting_approval": {
        "observed": "all children terminal, approval required",
        "smallest_action": "signal awaiting_approval and stop",
        "proposed_tool": "hermes_mission_reconcile",
        "verify": "status awaiting_approval; controller stops; owner approves",
        "auto_retry": False,
    },
    "request_completion": {
        "observed": "all children terminal, no approval required, verified evidence",
        "smallest_action": "request completion via verified lifecycle",
        "proposed_tool": "hermes_mission_reconcile",
        "verify": "completed; evidence ref recorded",
        "auto_retry": False,
    },
    "fail_closed_evidence": {
        "observed": "missing / corrupt / unverified evidence",
        "smallest_action": "fail-closed to blocked/reconciling; need_attention",
        "proposed_tool": "reconcile tools (hermes_mission_reconcile)",
        "verify": "not success; need_attention raised",
        "auto_retry": False,
    },
    "park_authority": {
        "observed": "authority / policy gate",
        "smallest_action": "park blocked, escalate",
        "proposed_tool": "controller signal + operator/owner",
        "verify": "human action; no auto-retry",
        "auto_retry": False,
    },
    "park_capability": {
        "observed": "no capable target",
        "smallest_action": "park blocked, escalate capability",
        "proposed_tool": "controller signal + Orchestrator",
        "verify": "placement reviewed; not auto-resolved",
        "auto_retry": False,
    },
    "recover_environment": {
        "observed": "environment fault (workspace/store/manifest/lease)",
        "smallest_action": "fail-closed to reconciling/blocked; recover around or escalate",
        "proposed_tool": "controller signal + operator",
        "verify": "environment recovered or escalated; never silently retried",
        "auto_retry": False,
    },
    "terminal_no_action": {
        "observed": "cancelled / operator terminal",
        "smallest_action": "none (terminal)",
        "proposed_tool": "",
        "verify": "terminal state stands; no action",
        "auto_retry": False,
    },
    "wait_recheck": {
        "observed": "waiting on dependency / approval / replenish",
        "smallest_action": "no action; recheck next trigger",
        "proposed_tool": "",
        "verify": "next trigger re-evaluates",
        "auto_retry": False,
    },
    "unknown_fail_closed": {
        "observed": "unclassifiable / missing observation",
        "smallest_action": "fail-closed: blocked + need_attention; record classification_uncertainty",
        "proposed_tool": "controller signal (broker)",
        "verify": "human review; no guessed class ever acted on",
        "auto_retry": False,
    },
}

MATRIX_ROW_KEYS = tuple(sorted(MATRIX))

# Which classes are *failure* classes vs non-failure progress classes.
_CLASS_TO_ROW: dict[str, str] = {
    CLASS_TRANSIENT: "retry_transient_backoff",
    CLASS_WAITING: "wait_recheck",
    CLASS_SEMANTIC: "escalate_semantic",
    CLASS_AUTHORITY: "park_authority",
    CLASS_CAPABILITY: "park_capability",
    CLASS_ENVIRONMENT: "recover_environment",
    CLASS_AMBIGUOUS: "observe_reconciling",
    CLASS_TERMINAL: "terminal_no_action",
    CLASS_UNKNOWN: "unknown_fail_closed",
    CLASS_NONE_DISPATCHABLE: "dispatch_ready_child",
    CLASS_NONE_APPROVAL: "signal_awaiting_approval",
    CLASS_NONE_COMPLETION: "request_completion",
    CLASS_NONE: "wait_recheck",
}

# ---------------------------------------------------------------------------
# Authoritative-vocabulary enums (inputs are validated against these; an
# unknown enum value is fail-closed to `unknown`, never guessed).
# ---------------------------------------------------------------------------

DELEGATION_STATES = frozenset(
    {"reserved", "queued", "running", "reconciling", "succeeded", "failed", "cancelled"}
)
DELEGATION_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MISSION_STATES = frozenset(
    {"draft", "running", "awaiting_approval", "completed", "paused", "blocked"}
)
NODE_STATES = frozenset(
    {
        "pending",
        "blockable",
        "dispatched",
        "running",
        "awaiting_review",
        "validated",
        "awaiting_approval",
        "completed",
        "failed",
        "paused",
    }
)
NODE_TERMINAL = frozenset({"completed", "failed"})
WORKER_EXIT_KINDS = frozenset(
    {"clean_exit", "rate_limited", "nonzero_exit", "signaled", "unknown"}
)
VERDICTS = frozenset(
    {"", "SATISFIED", "NOT_SATISFIED", "INCONCLUSIVE", "INVALID_CONTRACT"}
)
INFLIGHT_DELEGATION_STATES = frozenset({"queued", "running", "reconciling"})

# ---------------------------------------------------------------------------
# Fixed token vocabularies (INV-9: only matched token ids are ever recorded).
#
# These refine the host `_RESPAWN_BLOCKER_RE` (kanban_db_dispatch), which lumps
# throughput (rate limit / 429) together with account/credential walls
# (quota / auth / billing). §11.1 splits them: throughput → `transient`
# (backoff + retry), account/credential/policy → `authority` (park + escalate,
# never auto-retry). Both refinements are subsets of the host pattern set.
# ---------------------------------------------------------------------------

AUTHORITY_TOKENS: tuple[str, ...] = (
    "quota",
    "auth",
    "unauthorized",
    "forbidden",
    "billing",
    "subscription",
    "access_denied",
    "permission_denied",
    "invalid_api_key",
    "invalid_key",
    "403",
    "credentials",
    "confirm_required",
    "approval_required",
    "policy_denied",
)
TRANSIENT_TOKENS: tuple[str, ...] = (
    "rate_limit",
    "ratelimit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "bad_gateway",
    "service_unavailable",
    "overloaded",
    "temporarily_unavailable",
    "connection_error",
    "connection_reset",
    "connection_refused",
    "network_error",
    "timeout",
)
ENVIRONMENT_TOKENS: tuple[str, ...] = (
    "workspace_missing",
    "workspace_unavailable",
    "store_unreadable",
    "db_locked",
    "database_locked",
    "manifest_corrupt",
    "corrupt_manifest",
    "lease_stale",
    "stale_lease",
    "disk_full",
)
SEMANTIC_TOKENS: tuple[str, ...] = (
    "test_failure",
    "tests_failed",
    "assertion",
    "assertionerror",
    "forbidden_action",
    "artifact_mismatch",
    "missing_artifact",
    "protocol_violation",
    "compile_error",
    "syntax_error",
    "import_error",
    "regression",
)
CAPABILITY_TOKENS: tuple[str, ...] = (
    "no_capable_target",
    "missing_skill",
    "skill_not_found",
    "node_not_enrolled",
    "backend_unsupported",
    "no_backend",
    "no_peer",
    "capability_denied",
)

MAX_REPLAN_ATTEMPTS_DEFAULT = 1  # D8: replan bounded (<=1 by default)

# Envelope bounds.
MAX_ERROR_TEXT = 512  # matched, never stored raw
MAX_REASONS = 16
MAX_OBSERVATION_JSON = 8192
MAX_STRING = 128

# PII-ish tokens stripped from any echoed summary (mirrors operator_placement).
_PII_STRIP = re.compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)
