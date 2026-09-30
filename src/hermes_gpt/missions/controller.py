"""Supervised mission controller — shadow/observe reconciler loop.

Implements architecture proposal §17 item 6 / §7 / D2 / D10: a **long-running
controller process** in the hermes-gpt sidecar that reconciles missions against
their plan, driving the *existing* host Kanban dispatcher + ProviderProfile
routing (D2: via import_hermes/seams protocols; host kanban_watchers left
running as-is). This slice ships the controller at **L0/L1 by default**:

- **Observes** authoritative sidecar state (mission, plan_nodes, delegation,
  runner) and Classifies it via ``operator_failure_semantics`` (the Ops 8-class
  taxonomy + smallest-first §11.2 recovery matrix).
- **Emits** a plan + next-action + the commands it *would* run (``would_be_commands``)
  and the smallest recovery action **as decision output** — ``would_execute``
  is ``False`` and no dispatch/reclaim/redispatch/approval/completion path runs.
- **Writes ONLY** the controller's own surfaces: ``controller_plan`` (the recorded
  decision), ``controller_pass_lease`` (per-mission concurrency safety), and
  ``controller_telemetry`` (per-pass telemetry) — plus a heartbeat file. It never
  mutates mission/plan/delegation/attachment state, never completes or
  approves a Mission (D5/P3/§7.7 hard walls). Two v0.12 exceptions, each
  behind its own default-off gate set: (1) a crossing budget envelope whose
  full D3 gate set passes (machine gate + Operator enabled/direct +
  per-mission policy flags + per-call confirm) may pause the Mission +
  raise the ``budget_breaker`` signal via
  ``operator_mission_budget.enforce_budget_breaker`` (design
  ``docs/design/v0.12-budget-enforcement.md`` §2.2); (2) the L2 rung may
  execute one dispatch through the existing delegation authority surface —
  that surface owns the Mission linkage and its own CAS guards (design
  ``docs/design/v0.12-controller-l2.md``). With either gate off — the
  default — the pass output is byte-identical to the pre-v0.12 shadow pass.

Loop mechanics (§7.1–§7.6):
1. Trigger model T1–T5 (§7.2) enqueues a work request per mission.
2. **Conflate** — one in-flight pass per mission; a second trigger while a pass
   holds the lease marks "recheck needed" (§7.1).
3. **Per-mission pass lease** (§7.3) — a single guarded ``UPDATE`` acquired on
   ``(lease_expires IS NULL OR lease_expires < now)``; TTL =
   ``min(max_pass_duration, reconcile_interval*2)``; heartbeat-renewed;
   crash-reclaimed by TTL expiry (kill -9 of the owning process reclaims the pass
   on the next trigger).
4. **Classify** — the trigger is not the decision; classify the observation
   envelope (§11.1). Un-observable state fail-closes to ``blocked``/``reconciling``
   with ``need_attention`` (§7.5).
5. **Smallest action** from the recovery matrix as decision output (§11.2).
6. **Idempotency** (§7.4) — controller operation idempotency key =
   ``sha256(mission_id|node|stage_id|contract_sha|attempt_seq)``.
7. **Replan** (§7.6) — proposal-only for ``semantic_failure``, bounded (≤1), routed
   through the existing decompose/advance tools, never executed here.

Authority (D10): mutating autonomy is gated on BOTH the QA evidence model and the
Security authority model being present — both parents are ``done``. L0/L1 is the
default rung; the additive **L2 rung** (v0.12 slice-2, Pack B) executes at most
ONE action per pass and only when ALL of §2.1 hold: ``HERMES_GPT_CONTROLLER_EXECUTE=1``
(machine gate, default OFF), per-call ``confirm=True``, live enabled + ``direct``
apply mode, and a bound workspace. With the machine gate unset every surface here
is byte-identical to L0/L1. The rung keeps the whole §7.7 §2.3 prohibition matrix:
it never completes/approves/weakens evidence/auto-redispatch ``reconciling`` work/
rewrites a plan/retries unboundedly/places without authority/bypasses Mission or
delegation CAS/touches secrets, and every executed action is idempotency-keyed
with the pre-execution intent persisted before dispatch (crash → refused
``already_executed``, never a duplicate dispatch).

INV-9 data containment holds: no raw prompt / transcript / error body / objective
is persisted — only enums, hashes, matched token ids, counts, and bounded refs.

§12.2 telemetry/health + alerting (t_e8468723): every pass records its
``escalation_tier`` (GREEN/YELLOW/RED); ``controller_status()`` serves the
24h-windowed aggregate health + tier rollup; a RED pass spools a
fleet-attention INTERRUPT envelope (``missions/controller_attention_spool.jsonl``)
for the existing delivery-broker lane — the controller NEVER sends (no
self-send from inside the loop), and INV-10 keeps refused gates loud: a stale
heartbeat reads RED, a spool failure raises, and the status tool audits tier
+ spool counts.

Conventions mirror ``operator_failure_semantics`` / ``operator_placement``:
public functions return a bounded JSON envelope; read surfaces require
``read_only``; every call is audited (bounded summary + counts only).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.missions import controller_attention as attention
from hermes_gpt.missions import controller_decisions as decisions
from hermes_gpt.missions import controller_health as health
from hermes_gpt.missions import controller_l2 as _l2
from hermes_gpt.missions import controller_observation as _observation
from hermes_gpt.missions import plan
from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op

# Preserve the former module-level pass API while implementations live in
# dedicated controller modules.
from hermes_gpt.missions.controller_pass import PASS_SCHEMA, NullHostAdapter, _apply_l2_execution, _budget_would_pause, _l2_plan_execution, _record_telemetry, _stuck_seconds, controller_status, reconcile_pass, run_loop_tick
from hermes_gpt.missions.controller_preview import reconcile_preview

# Preserve the pre-split observation helper names for integrations.
MAX_NODES = _observation.MAX_NODES
_latest_delegation = _observation._latest_delegation
build_observation = _observation.build_observation

# Keep these names available from operator_controller for compatibility.
from hermes_gpt.missions.controller_store import MAX_MISSIONS_PER_PASS, MAX_TARGET_STRING, TRIGGER_DEPENDENCY, TRIGGER_HEALTH, TRIGGER_LIVE_EVENT, TRIGGER_MANUAL, TRIGGER_PERIODIC, TRIGGERS, _connect, _db_path, _init_controller_tables, _lease_expiry, _lease_info, _lease_seq, _now, _now_iso_ts, _now_ts, _root, _sanitize, acquire_lease, conflate, consume_trigger, mark_recheck, release_lease, renew_lease, trigger

# Keep the pre-split controller names available to tests and integrations.
CONTROLLER_EXECUTE_ENV = _l2.CONTROLLER_EXECUTE_ENV
EXECUTABLE_ROW_KEYS = _l2.EXECUTABLE_ROW_KEYS
ATTENTION_ROW_KEYS = _l2.ATTENTION_ROW_KEYS
REFUSED_CONFIRM_REQUIRED = _l2.REFUSED_CONFIRM_REQUIRED
REFUSED_DRY_RUN = _l2.REFUSED_DRY_RUN
REFUSED_POLICY = _l2.REFUSED_POLICY
REFUSED_WORKSPACE = _l2.REFUSED_WORKSPACE
REFUSED_UNSUPPORTED_ACTION = _l2.REFUSED_UNSUPPORTED_ACTION
REFUSED_ALREADY_EXECUTED = _l2.REFUSED_ALREADY_EXECUTED
REFUSED_ATTENTION = _l2.REFUSED_ATTENTION
REFUSED_NO_TARGET = _l2.REFUSED_NO_TARGET
REFUSED_NO_ACTION = _l2.REFUSED_NO_ACTION
REFUSED_APPROVAL_GATE = _l2.REFUSED_APPROVAL_GATE
REFUSED_AUTH_CLASS = _l2.REFUSED_AUTH_CLASS
REFUSED_SECRET_REQUIREMENT = _l2.REFUSED_SECRET_REQUIREMENT
EXECUTION_RESULTS = _l2.EXECUTION_RESULTS
EXECUTION_REFUSAL_CODES = _l2.EXECUTION_REFUSAL_CODES
EXECUTION_STATE_INTENT = _l2.EXECUTION_STATE_INTENT
EXECUTION_STATE_DISPATCHED = _l2.EXECUTION_STATE_DISPATCHED
EXECUTION_STATE_FAILED = _l2.EXECUTION_STATE_FAILED
EXECUTION_PRIOR_STATES = _l2.EXECUTION_PRIOR_STATES
L2_FORBIDDEN_AUTH_CLASSES = _l2.L2_FORBIDDEN_AUTH_CLASSES
_execute_enabled = _l2._execute_enabled
_execution_block = _l2._execution_block
_refusal = _l2._refusal
_live_policy_gate = _l2._live_policy_gate
_l2_dispatch = _l2._l2_dispatch
_attention_spool_path = attention._attention_spool_path
build_attention_envelope = attention.build_attention_envelope
spool_attention_envelope = attention.spool_attention_envelope
attention_envelopes = attention.attention_envelopes
_l2_escalate_no_target = attention._l2_escalate_no_target
SCHEMA_VERSION = health.SCHEMA_VERSION
CONTROLLER_MODE = health.CONTROLLER_MODE
DEFAULT_INTERVAL_SECONDS = health.DEFAULT_INTERVAL_SECONDS
MIN_INTERVAL_SECONDS = health.MIN_INTERVAL_SECONDS
MAX_PASS_DURATION_SECONDS = health.MAX_PASS_DURATION_SECONDS
HEARTBEAT_STALE_SECONDS = health.HEARTBEAT_STALE_SECONDS
AGGREGATE_WINDOW_SECONDS = health.AGGREGATE_WINDOW_SECONDS
_lease_ttl_seconds = health._lease_ttl_seconds
_heartbeat_path = health._heartbeat_path
_read_heartbeat = health._read_heartbeat
_rollup_tier = health._rollup_tier
_rollup_reasons = health._rollup_reasons
_count_uncertainty = health._count_uncertainty
heartbeat_pulse = health.heartbeat_pulse
PASS_NOOP = decisions.PASS_NOOP
PASS_DISPATCHED = decisions.PASS_DISPATCHED
PASS_RECOVERED = decisions.PASS_RECOVERED
PASS_ESCALATED = decisions.PASS_ESCALATED
PASS_BLOCKED = decisions.PASS_BLOCKED
PASS_STALE = decisions.PASS_STALE
PASS_RESULTS = decisions.PASS_RESULTS
TIER_GREEN = decisions.TIER_GREEN
TIER_YELLOW = decisions.TIER_YELLOW
TIER_RED = decisions.TIER_RED
TIERS = decisions.TIERS
STALE_RECONCILING_SECONDS = decisions.STALE_RECONCILING_SECONDS
MAX_WOULD_BE_COMMANDS = decisions.MAX_WOULD_BE_COMMANDS
RED_ROW_KEYS = decisions.RED_ROW_KEYS
YELLOW_CLASSES = decisions.YELLOW_CLASSES
_GREEN_CLASSES = decisions._GREEN_CLASSES
_idempotency_key = decisions._idempotency_key
_would_be_commands = decisions._would_be_commands
_pass_result = decisions._pass_result
derive_pass_tier = decisions.derive_pass_tier

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = plan.NODE_ID_RE
SHA_RE = mission.SHA_RE

# ---------------------------------------------------------------------------

MAX_PLAN_NODE_LIMIT = 200




# ---------------------------------------------------------------------------
# Small helpers (mirror sibling operator modules)
# ---------------------------------------------------------------------------

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )

def _audit(
    tool: str,
    policy: op.OperatorPolicy | None,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    mission_id: str = "",
    node_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level if policy else "read_only",
            apply_mode=policy.apply_mode if policy else "dry_run",
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id} node={node_id}",
            extra={"mission_id": mission_id, "node_id": node_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


# ---------------------------------------------------------------------------
# Observation adapter — §11.1 "authoritative observation only"
# ---------------------------------------------------------------------------


def hermes_controller_reconcile(
    mission_id: str,
    trigger_kind: str = TRIGGER_MANUAL,
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Run one supervised reconciliation pass.

    ``dry_run=True`` (default) returns the exact pass envelope a direct pass
    would record — via a non-persisting preview that writes no mission,
    plan, delegation, lease, controller_plan/controller_telemetry, heartbeat,
    or attention state (the only durable side effect is the repo-wide
    Operator audit trail that every tool call produces) — and requires only
    read authority.

    ``dry_run=False`` runs the persisting pass (controller plans, telemetry,
    leases, heartbeats, attention envelopes) and requires ``workspace`` level
    plus ``direct`` apply mode.

    The pass is decision-only unless the L2 rung is fully enabled: the
    ``HERMES_GPT_CONTROLLER_EXECUTE=1`` machine gate (default off) AND
    ``confirm=True`` here AND a live direct apply mode AND a bound workspace.
    Only then may the pass execute its single smallest computed action through
    the existing work-contract/delegation authority surface; otherwise the
    envelope stays decision-only with ``would_execute`` False and an additive
    ``execution`` block carrying the stable refusal reason.
    """
    policy = op.OperatorPolicy()
    try:
        if not dry_run:
            policy.require_level("workspace")
            # The persisting pass writes controller bookkeeping (plans,
            # telemetry, leases, heartbeats) and therefore requires direct
            # apply mode. ``require_mutation`` cannot express this: with a
            # non-direct apply mode it silently downgrades instead of
            # raising, which would leave the persistence ungated.
            if policy.apply_mode != "direct":
                raise PermissionError(
                    "Controller reconcile persists controller bookkeeping and requires "
                    f"{op.OPERATOR_APPLY_MODE_ENV}=direct."
                )
        else:
            policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        if trigger_kind not in TRIGGERS:
            raise ValueError(f"trigger_kind must be one of {TRIGGERS}")

        if dry_run:
            result = reconcile_preview(
                mission_id,
                trigger_kind,
                hermes_root=hermes_root,
            )
        else:
            result = reconcile_pass(
                mission_id,
                trigger_kind,
                host=NullHostAdapter(),
                hermes_root=hermes_root,
                interval=DEFAULT_INTERVAL_SECONDS,
                confirm=bool(confirm),
            )
        result["dry_run"] = bool(dry_run)
        result["changed"] = not dry_run
        _audit(
            "hermes_controller_reconcile",
            policy,
            dry_run=bool(dry_run),
            success=not any(k in result for k in ("error",)),
            changed=not dry_run,
            mission_id=mission_id,
            node_id=result.get("node_id", ""),
            extra={
                "classification": result.get("classification", ""),
                "row_key": result.get("row_key", ""),
                "pass_result": result.get("pass_result", ""),
                "lease_acquired": bool(result.get("lease_acquired")),
                # §4: audit carries the execution marker (bounded fields only).
                "execution_enabled": _execute_enabled(),
                "executed": bool(result.get("would_execute")),
                "execution_result": str(
                    (result.get("execution") or {}).get("result", "")
                ),
                "execution_refused_reason": str(
                    (result.get("execution") or {}).get("refused_reason") or ""
                ),
            },
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_controller_reconcile",
            policy,
            dry_run=bool(dry_run),
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "CONTROLLER_RECONCILE_REJECTED",
            "Check the mission id, trigger kind, and Operator mutation policy.",
        )


def hermes_controller_status(hermes_root: Path | None = None) -> str:
    """Read-only controller health surface (§12.2 + GREEN/YELLOW/RED tier)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        status = controller_status(hermes_root)
        _audit(
            "hermes_controller_status",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={
                "controller_live": bool(status.get("controller_live")),
                "active_passes": status.get("active_passes", 0),
                "tier": status.get("tier", ""),
                "attention_spooled": status.get("attention_spooled", 0),
            },
        )
        return json.dumps(status, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        return _error(
            exc, "CONTROLLER_STATUS_REJECTED", "Operator policy must be enabled."
        )


def hermes_controller_lease_list(
    mission_id: str = "", hermes_root: Path | None = None
) -> str:
    """Read-only: current per-mission pass leases + trigger queue conflation state."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        leases: dict[str, Any] = {}
        triggers: list[dict[str, Any]] = []
        if path.is_file():
            with _connect(path, write=False) as db:
                try:
                    rows = db.execute(
                        "SELECT mission_id FROM controller_pass_lease ORDER BY mission_id"
                    ).fetchall()
                    for r in rows:
                        mid = r["mission_id"]
                        if mission_id and mid != mission_id:
                            continue
                        leases[mid] = _lease_info(db, mid)
                except sqlite3.Error:
                    pass
                if not mission_id:
                    try:
                        for r in db.execute(
                            "SELECT mission_id,trigger_kind,ref,seq FROM controller_trigger_queue "
                            "ORDER BY seq LIMIT ?",
                            (MAX_MISSIONS_PER_PASS,),
                        ).fetchall():
                            triggers.append(
                                {
                                    "mission_id": r["mission_id"],
                                    "trigger_kind": r["trigger_kind"],
                                    "ref": r["ref"],
                                    "seq": int(r["seq"]),
                                }
                            )
                    except sqlite3.Error:
                        pass
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "mode": CONTROLLER_MODE,
            "leases": leases,
            "trigger_queue": triggers,
        }
        _audit(
            "hermes_controller_lease_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={"lease_count": len(leases)},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "CONTROLLER_LEASE_LIST_REJECTED",
            "Check the mission id and Operator policy.",
        )


def hermes_controller_trigger(
    mission_id: str,
    trigger_kind: str,
    ref: str = "",
    hermes_root: Path | None = None,
) -> str:
    """Enqueue a T1–T5 request (persistent controller mutation)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        # Enqueueing persists trigger-queue + recheck state unconditionally:
        # require direct apply mode. ``require_mutation`` cannot express this
        # (a non-direct apply mode silently downgrades instead of raising).
        if policy.apply_mode != "direct":
            raise PermissionError(
                "Controller trigger persists queue/recheck state and requires "
                f"{op.OPERATOR_APPLY_MODE_ENV}=direct."
            )
        result = trigger(mission_id, trigger_kind, ref, hermes_root=hermes_root)
        _audit(
            "hermes_controller_trigger",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={"trigger_kind": trigger_kind, "seq": result.get("seq", 0)},
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_controller_trigger",
            policy,
            dry_run=False,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc, "CONTROLLER_TRIGGER_REJECTED", "Check the mission id, trigger kind, and Operator mutation policy."
        )
