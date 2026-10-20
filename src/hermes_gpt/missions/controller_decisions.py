"""Pure row-key to command, pass-result, and escalation decisions."""

from __future__ import annotations

import hashlib
from typing import Any

from hermes_gpt.missions import failure_semantics as fs
from hermes_gpt.missions.controller_store import _sanitize

# §12.2 pass-result vocabulary
# ---------------------------------------------------------------------------

# pass_result (telemetry §12.2): noop | dispatched | recovered | escalated |
# blocked | stale
PASS_NOOP = "noop"
PASS_DISPATCHED = "dispatched"
PASS_RECOVERED = "recovered"
PASS_ESCALATED = "escalated"
PASS_BLOCKED = "blocked"
PASS_STALE = "stale"
PASS_RESULTS = (
    PASS_NOOP,
    PASS_DISPATCHED,
    PASS_RECOVERED,
    PASS_ESCALATED,
    PASS_BLOCKED,
    PASS_STALE,
)

# §12.2 alerting tiers. GREEN = healthy/silent; YELLOW = degraded but
# self-healing (count-only in status output); RED = a human gate is genuinely
# required -> the pass spools a fleet-attention INTERRUPT envelope for the
# delivery broker (never self-sent from inside the loop).
TIER_GREEN = "GREEN"
TIER_YELLOW = "YELLOW"
TIER_RED = "RED"
TIERS = (TIER_GREEN, TIER_YELLOW, TIER_RED)

# A blocked mission with unresolved attention past this age is RED.
STALE_RECONCILING_SECONDS = 3600.0
MAX_WOULD_BE_COMMANDS = 8


def _idempotency_key(
    mission_id: str,
    node_id: str,
    stage_id: str,
    contract_sha: str,
    attempt_seq: int,
) -> str:
    """§7.4 controller operation idempotency key."""
    return hashlib.sha256(
        f"{mission_id}|{node_id}|{stage_id}|{contract_sha}|{int(attempt_seq)}".encode()
    ).hexdigest()



def _would_be_commands(
    row_key: str,
    mission_id: str,
    node_id: str,
    contract_sha: str,
    attempt_seq: int,
    delegation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The commands a higher-autonomy rung *would* run (D10: L0/L1 only)."""
    if not row_key:
        return []
    commands: list[dict[str, Any]] = []
    tgt = node_id or (delegation or {}).get("delegation_id", "")
    idem = _idempotency_key(
        mission_id, node_id or "", node_id or "", contract_sha, attempt_seq
    )
    spec_by_row = {
        "dispatch_ready_child": (
            "hermes_swarm_stage_dispatch",
            "dispatch ready child to the assigned profile",
        ),
        "reclaim_dead_worker": (
            "host:kanban reclaim_task",
            "reclaim a dead worker + bounded retry",
        ),
        "observe_reconciling": (
            "hermes_delegation_reconcile",
            "observe + report; never auto-redispatch",
        ),
        "retry_transient_backoff": (
            "dispatcher retry (breaker respected)",
            "backoff + retry on a transient failure",
        ),
        "breaker_exhausted": (
            "controller signal (broker) + operator",
            "blocked + gave_up + human signal",
        ),
        "escalate_semantic": (
            "hermes_swarm_stage_advance rework | hermes_plan_decompose",
            "escalate + bounded replan proposal",
        ),
        "signal_awaiting_approval": (
            "hermes_mission_reconcile",
            "signal awaiting_approval, then stop",
        ),
        "request_completion": (
            "hermes_mission_reconcile",
            "request completion via verified lifecycle",
        ),
        "fail_closed_evidence": (
            "reconcile tools (hermes_mission_reconcile)",
            "fail-closed on missing evidence; need_attention",
        ),
        "park_authority": (
            "controller signal + operator/owner",
            "park blocked; human action; no auto-retry",
        ),
        "park_capability": (
            "controller signal + Orchestrator",
            "park blocked; placement reviewed",
        ),
        "recover_environment": (
            "controller signal + operator",
            "recover around or escalate; never silently retried",
        ),
        "terminal_no_action": ("", "terminal; no action"),
        "wait_recheck": ("", "no action; recheck next trigger"),
        "unknown_fail_closed": (
            "controller signal (broker)",
            "fail-closed blocked + need_attention",
        ),
    }
    tool, rationale = spec_by_row.get(row_key, ("", ""))
    if tool:
        commands.append(
            {
                "action": row_key,
                "tool": tool,
                "target": _sanitize(tgt, 64),
                "rationale": rationale,
                "idempotency_key": idem,
                "executed": False,
            }
        )
    return commands[:MAX_WOULD_BE_COMMANDS]



def _pass_result(row_key: str) -> str:
    if row_key in ("dispatch_ready_child",):
        return PASS_DISPATCHED
    if row_key in (
        "reclaim_dead_worker",
        "retry_transient_backoff",
        "recover_environment",
    ):
        return PASS_RECOVERED
    if row_key in ("escalate_semantic",):
        return PASS_ESCALATED
    if row_key in (
        "park_authority",
        "park_capability",
        "fail_closed_evidence",
        "breaker_exhausted",
        "unknown_fail_closed",
    ):
        return PASS_BLOCKED
    return PASS_NOOP



# ---------------------------------------------------------------------------
# §12.2 alerting tiers: GREEN silent / YELLOW count-only / RED -> broker
# Hard-wall rows: the controller can propose NO safe next action and a human
# gate is genuinely required (RED by definition in §12.2).
RED_ROW_KEYS = frozenset(
    {
        "park_authority",
        "park_capability",
        "breaker_exhausted",
        "unknown_fail_closed",
        "fail_closed_evidence",
    }
)
# YELLOW classes: abnormal, but self-healing or routable through normal lanes
# without a human gate (counted in status, never interrupted on).
YELLOW_CLASSES = frozenset(
    {
        fs.CLASS_TRANSIENT,
        fs.CLASS_SEMANTIC,
        fs.CLASS_ENVIRONMENT,
        fs.CLASS_AMBIGUOUS,
    }
)
_GREEN_CLASSES = frozenset(
    {
        fs.CLASS_NONE,
        fs.CLASS_NONE_DISPATCHABLE,
        fs.CLASS_NONE_APPROVAL,
        fs.CLASS_NONE_COMPLETION,
        fs.CLASS_WAITING,
        fs.CLASS_TERMINAL,
    }
)


def derive_pass_tier(
    *,
    mission_status: str,
    classification: str,
    row_key: str,
    need_attention: bool,
    stuck_s: float | None = None,
) -> tuple[str, list[str]]:
    """Deterministic §12.2 tier for one pass.

    RED (§12.2): mission blocked/reconciling past the stale threshold with
    ``need_attention``, breaker ``gave_up``, or a hard capability/authority
    wall (including fail-closed unknown/evidence walls — INV-10: a refused
    gate is as loud as permitted). YELLOW: abnormal but self-healing or
    routable without a human gate. GREEN: steady state (silent).
    """
    if row_key in RED_ROW_KEYS:
        return TIER_RED, [f"hard_wall:{row_key}"]
    if (
        need_attention
        and mission_status in ("blocked",)
        and stuck_s is not None
        and stuck_s > STALE_RECONCILING_SECONDS
    ):
        return TIER_RED, ["stale_blocked_reconciling"]
    if classification in YELLOW_CLASSES:
        return TIER_YELLOW, [f"class:{classification}"]
    if classification == fs.CLASS_UNKNOWN:
        # Unknown without a hard-wall row key still fail-closes loudly.
        return TIER_RED, ["fail_closed:unknown"]
    if classification in _GREEN_CLASSES:
        return TIER_GREEN, ["steady_state"]
    return TIER_YELLOW, [f"unmapped_class:{classification or 'none'}"]
