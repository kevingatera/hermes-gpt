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
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_controller_decisions as decisions
import operator_controller_health as health
import operator_controller_l2 as _l2
import operator_delegations as deleg
import operator_failure_semantics as fs
import operator_mission_budget as op_mission_budget
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_policy as op
from operator_controller_observation import (
    HostObservationAdapter,
    MAX_NODES as MAX_NODES,
    _latest_delegation as _latest_delegation,
    build_observation,
)

# Keep these names available from operator_controller for compatibility.
from operator_controller_store import (
    MAX_MISSIONS_PER_PASS,
    MAX_TARGET_STRING,  # noqa: F401
    TRIGGER_DEPENDENCY,  # noqa: F401
    TRIGGER_HEALTH,  # noqa: F401
    TRIGGER_LIVE_EVENT,  # noqa: F401
    TRIGGER_MANUAL,
    TRIGGER_PERIODIC,  # noqa: F401
    TRIGGERS,
    _connect,
    _db_path,
    _init_controller_tables,  # noqa: F401
    _lease_expiry,  # noqa: F401
    _lease_info,
    _lease_seq,  # noqa: F401
    _now,
    _now_iso_ts,  # noqa: F401
    _now_ts,
    _root,
    _sanitize,
    acquire_lease,
    conflate,
    consume_trigger,
    mark_recheck,
    release_lease,
    renew_lease,  # noqa: F401
    trigger,
)

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

PASS_SCHEMA = "hermes.controller-pass/v1"

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


class NullHostAdapter:
    """Default for shadow mode: no host signals, so observation is fail-closed."""

    def worker_exit(self, mission_id: str, node_id: str) -> dict[str, Any] | None:
        return None

    def breaker(self, mission_id: str) -> dict[str, Any]:
        return {"consecutive_failures": 0, "limit": 3, "gave_up": False}

    def last_failure_error(self, mission_id: str, node_id: str) -> str:
        return ""

    def capability(self, mission_id: str, node_id: str) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# pass_result mapping (§12.2 telemetry vocabulary)
# ---------------------------------------------------------------------------






def _attention_spool_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "controller_attention_spool.jsonl"


def build_attention_envelope(
    *,
    mission_id: str,
    node_id: str,
    classification: str,
    row_key: str,
    proposed_action: str,
    reasons: list[str],
    uncertainty: str,
    tier_reasons: list[str],
    pass_seq: int,
) -> dict[str, Any]:
    """Build one fleet-attention INTERRUPT envelope for a RED pass (§12.2).

    Schema: fleet-attention-envelope/v1 (ops delivery contract). INV-9: only
    bounded enums, ids, and fixed matrix strings — never raw objective/error/
    secret text. Delivery is NOT performed here: the envelope is spooled for
    the existing delivery-broker lane (no self-send from inside the loop).
    """
    approval_wall = row_key == "park_authority"
    summary = (
        f"Supervised mission controller (shadow/observe) classified a pass as "
        f"RED — a human gate is genuinely required. mission={mission_id} "
        f"node={node_id or '-'} classification={classification} "
        f"row_key={row_key} proposed_action={_sanitize(proposed_action, 200)} "
        f"reasons={','.join(tier_reasons[:4])}"
    )
    if uncertainty:
        summary += f" uncertainty={_sanitize(uncertainty, 64)}"
    return {
        "schema_version": 1,
        "event_id": f"ctl-{uuid.uuid4().hex[:24]}",
        "source": {
            "kind": "system",
            "profile": "ops",
            "job_id": "hermes-gpt-controller",
            "job_name": "supervised-mission-controller (shadow)",
            "run_id": f"pass:{pass_seq}",
        },
        "domain": "operations",
        "severity": "P1",
        "attention_class": "INTERRUPT",
        "state": "open",
        "action_required": True,
        "approval_required": approval_wall,
        "title": f"Mission controller RED: {row_key} ({mission_id})"[:200],
        "summary": summary[:4000],
        "dedupe_key": f"controller:red:{mission_id}:{row_key}"[:300],
        "occurred_at": _now(),
        "evidence": [
            f"missions/missions.db#controller_telemetry(mission_id={mission_id})",
            f"missions/missions.db#controller_plan(mission_id={mission_id})",
            "missions/controller_heartbeat.json",
        ][:32],
        "metadata": {
            "tier": TIER_RED,
            "mode": CONTROLLER_MODE,
            "would_execute": False,
            "classification": classification,
            "row_key": row_key,
            "classification_uncertainty": _sanitize(uncertainty, 64),
            "escalation_reasons": [
                _sanitize(r, 64) for r in (*tier_reasons, *reasons[:2])
            ][:8],
            "proposed_action": _sanitize(proposed_action, 200),
        },
    }


def spool_attention_envelope(
    envelope: dict[str, Any], hermes_root: Path | None = None
) -> Path:
    """Append one RED envelope to the controller attention spool.

    INV-10 (loud as permitted): a spool failure raises — the pass fails loudly
    rather than silently dropping a human-gate signal. This never sends; the
    existing fleet-attention delivery lane owns transport.
    """
    path = _attention_spool_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(envelope, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def attention_envelopes(hermes_root: Path | None = None) -> list[dict[str, Any]]:
    """Read-only: spooled RED envelopes awaiting broker flush (oldest first)."""
    path = _attention_spool_path(hermes_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _stuck_seconds(db: sqlite3.Connection, mission_id: str) -> float | None:
    """Seconds since the oldest still-relevant need_attention pass (approx.).

    Shadow-slice approximation: age of the oldest need_attention telemetry
    row inside the aggregate window. ``None`` when the mission has never
    raised attention (nothing is stuck).
    """
    cutoff = datetime.fromtimestamp(
        _now_ts() - AGGREGATE_WINDOW_SECONDS, tz=timezone.utc
    ).isoformat()
    try:
        row = db.execute(
            "SELECT MIN(created_at) AS c FROM controller_telemetry "
            "WHERE mission_id=? AND need_attention=1 AND created_at>=?",
            (mission_id, cutoff),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["c"]:
        return None
    try:
        started = datetime.fromisoformat(row["c"])
    except ValueError:
        return None
    return max(0.0, _now_ts() - started.timestamp())


def _attention_count(hermes_root: Path | None) -> int:
    return len(attention_envelopes(hermes_root))


def controller_status(hermes_root: Path | None = None) -> dict[str, Any]:
    """Read-only aggregate health with the controller's attention spool count."""
    return health.controller_status(hermes_root, attention_count=_attention_count)


# ---------------------------------------------------------------------------
# Telemetry (per-pass §12.2) + aggregate health §12.2
# ---------------------------------------------------------------------------


def _record_telemetry(db: sqlite3.Connection, entry: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO controller_telemetry("
        "mission_id,trigger_kind,node_id,started_at,duration_ms,pass_result,"
        "classification,row_key,would_execute,lease_acquired,actions_taken_json,need_attention,created_at,"
        "lease_reclaimed,escalation_tier,executed_idempotency_key,executed_result,"
        "executed_target,executed_refused_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry["mission_id"],
            entry.get("trigger_kind", ""),
            entry.get("node_id", ""),
            entry["started_at"],
            int(entry.get("duration_ms", 0)),
            entry.get("pass_result", PASS_NOOP),
            entry.get("classification", ""),
            entry.get("row_key", ""),
            1 if entry.get("would_execute") else 0,
            1 if entry.get("lease_acquired") else 0,
            _canonical(entry.get("actions_taken", [])),
            1 if entry.get("need_attention") else 0,
            entry["started_at"],
            1 if entry.get("lease_reclaimed") else 0,
            entry.get("escalation_tier", ""),
            _sanitize(entry.get("executed_idempotency_key", ""), 64),
            _sanitize(entry.get("executed_result", ""), 32),
            _sanitize(entry.get("executed_target", ""), 64),
            _sanitize(entry.get("executed_refused_reason", ""), 64),
        ),
    )


def _l2_escalate_no_target(
    *,
    mission_id: str,
    node_id: str,
    pass_seq: int,
    hermes_root: Path | None,
) -> None:
    """§3: no_capable_target escalates (spool INTERRUPT); never auto-resolve.

    Uses the existing RED attention helper; a spool failure raises (INV-10).
    """
    envelope = build_attention_envelope(
        mission_id=mission_id,
        node_id=node_id,
        classification=fs.CLASS_CAPABILITY,
        row_key="park_capability",
        proposed_action="escalate capability (placement no_capable_target)",
        reasons=["placement:no_capable_target"],
        uncertainty="",
        tier_reasons=["placement:no_capable_target"],
        pass_seq=pass_seq,
    )
    envelope["metadata"]["proposed_action"] = "escalate capability (placement no_capable_target)"
    envelope["dedupe_key"] = f"controller:l2-no-target:{mission_id}:{node_id}"[:300]
    spool_attention_envelope(envelope, hermes_root=hermes_root)



def _l2_plan_execution(
    db: sqlite3.Connection,
    path: Path,
    hermes_root: Path | None,
    *,
    mission_id: str,
    node_id: str,
    row_key: str,
    cmds: list[dict[str, Any]],
    confirm: bool,
    attempt_seq: int,
    pass_seq: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Keep the controller helper signature while delegating the L2 plan."""
    return _l2.plan_execution(
        db,
        path,
        hermes_root,
        mission_id=mission_id,
        node_id=node_id,
        row_key=row_key,
        cmds=cmds,
        confirm=confirm,
        attempt_seq=attempt_seq,
        pass_seq=pass_seq,
        escalate_no_target=_l2_escalate_no_target,
    )



def _apply_l2_execution(
    db: sqlite3.Connection,
    path: Path,
    hermes_root: Path | None,
    *,
    mission_id: str,
    node_id: str,
    row_key: str,
    cmds: list[dict[str, Any]],
    confirm: bool,
    attempt_seq: int,
    pass_seq: int,
    record: dict[str, Any],
    pass_env: dict[str, Any],
    tier: str,
    tier_reasons: list[str],
) -> tuple[str, list[str]]:
    """Apply dispatch state to the pending plan and pass envelope.

    The execution intent is persisted before dispatch. This helper updates
    ``record`` and ``pass_env`` in place and returns any tier change.
    """
    if _execute_enabled():
        execution_block, pending, carry = _l2_plan_execution(
            db,
            path,
            hermes_root,
            mission_id=mission_id,
            node_id=node_id,
            row_key=row_key,
            cmds=cmds,
            confirm=confirm,
            attempt_seq=attempt_seq,
            pass_seq=pass_seq,
        )
        if carry is not None:
            # Fail-closed replay protection (§2.2/§5): the plan row is
            # REPLACE-keyed by (mission_id, node_id, decision_sha256), so
            # carry the prior execution evidence forward verbatim — the key
            # must stay refused, not be forgotten.
            record["execution"] = carry
        if pending is not None:
            # Persist the pre-execution intent BEFORE the dispatch call so
            # a crash mid-execution is detectable and reconciles fail-closed
            # (no duplicate dispatch on recovery).
            record["execution"] = {
                "state": EXECUTION_STATE_INTENT,
                "idempotency_key": pending["key"],
                "target": pending["target"],
                "profile": pending["profile"],
                "result": "",
            }
            fs._record_decision(db, record)
            db.commit()
            executed, result, refused_reason, linkage = _l2_dispatch(
                pending["contract"], mission_id, hermes_root
            )
            execution_block = _execution_block(
                executed=executed,
                action_kind=row_key,
                idempotency_key=pending["key"],
                result=result,
                refused_reason=refused_reason or None,
                placement_view=pending["placement"],
            )
            record["execution"] = {
                "state": (
                    EXECUTION_STATE_DISPATCHED
                    if executed
                    else EXECUTION_STATE_FAILED
                ),
                "idempotency_key": pending["key"],
                "target": pending["target"],
                "profile": pending["profile"],
                "result": result,
                "linkage": linkage,
            }
            record["would_execute"] = bool(executed)
            pass_env["would_execute"] = bool(executed)
            pass_env["executed_idempotency_key"] = pending["key"]
            pass_env["executed_result"] = result
            pass_env["executed_target"] = pending["target"]
            pass_env["executed_refused_reason"] = _sanitize(refused_reason, 64)
            if not executed:
                # §4/§5: an execution failure surfaces on the pass state
                # (fail closed), never as silent success, and never as a
                # retry loop — bounded rework only, on a new attempt_seq.
                record["pass_result"] = PASS_BLOCKED
                pass_env["pass_result"] = PASS_BLOCKED
                if tier == TIER_GREEN:
                    tier, tier_reasons = TIER_YELLOW, ["execution_failed"]
                    pass_env["escalation_tier"] = tier
                    pass_env["escalation_reasons"] = tier_reasons
                pass_env["actions_taken"].append(
                    {
                        "action": "execute_failed",
                        "detail": _sanitize(
                            f"{result}:{refused_reason or 'dispatch_failed'}", 120
                        ),
                    }
                )
            else:
                pass_env["actions_taken"].append(
                    {
                        "action": "execute",
                        "detail": (
                            "dispatched via delegation authority surface "
                            f"(target={pending['target']})"
                        ),
                    }
                )
        elif execution_block is not None:
            pass_env["executed_refused_reason"] = _sanitize(
                str(execution_block.get("refused_reason") or ""), 64
            )
            pass_env["actions_taken"].append(
                {
                    "action": "execute_refused",
                    "detail": str(execution_block.get("refused_reason") or ""),
                }
            )
        if execution_block is not None:
            pass_env["execution"] = execution_block
    return tier, tier_reasons


# ---------------------------------------------------------------------------
# The single-pass reconciler (shadow/observe): observe → classify → smallest
# action as decision output; writes ONLY controller_plan + controller_telemetry
# + controller_pass_lease.
# ---------------------------------------------------------------------------


def _budget_would_pause(db: sqlite3.Connection, mission_id: str) -> bool:
    """Read-only: would the mission's budget envelope trip the D3 breaker?

    v0.12 Pack A (design §2.2): the reconcile pass calls this to decide
    whether the mission in scope is on a crossing edge with the per-mission
    hard-block policy armed. Pure evaluation on the pass's own connection —
    no writes. Missions without a budget account (or stores without the
    budget tables) simply evaluate False.
    """
    try:
        if not op_mission_budget._account_table_exists(db):
            return False
        account = op_mission_budget._get_account_row(db, mission_id)
    except (LookupError, ValueError, sqlite3.Error):
        return False
    try:
        policy_obj = json.loads(account["policy_json"])
        env = op_mission_budget._envelope_status(
            float(account["spend"]), float(account["quota"]), str(account["unit"])
        )
        return bool(
            op_mission_budget._would_block(env, policy_obj)["would_pause"]
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def reconcile_pass(
    mission_id: str,
    trigger_kind: str,
    *,
    host: HostObservationAdapter | None = None,
    hermes_root: Path | None = None,
    lease_lock: str = "",
    interval: float = DEFAULT_INTERVAL_SECONDS,
    confirm: bool = False,
) -> dict[str, Any]:
    """Run one shadow pass over a mission.

    This is the heart of the loop (§7.1): acquire the pass lease → observe →
    classify → smallest action from the recovery matrix → record to
    ``controller_plan`` + ``controller_telemetry`` → release the lease. In
    shadow mode (the default) nothing dispatches, reclaims, redispatch,
    completes, or approves; ``would_execute`` is ``False`` and the returned
    envelope carries the ``would_be_commands`` for a higher-autonomy rung.

    ``confirm=True`` (per-call) plus the ``HERMES_GPT_CONTROLLER_EXECUTE``
    machine gate, live direct apply mode, and a bound workspace enable the L2
    rung (§2 of docs/design/v0.12-controller-l2.md): the pass then EXECUTES at
    most one action — the smallest computed one — through the existing
    work-contract/delegation authority surface. Every §7.7 prohibition still
    binds: the rung cannot complete, approve, weaken evidence, auto-redispatch
    ``reconciling`` work, rewrite a plan, retry unboundedly, place without
    authority, bypass a Mission/delegation CAS, or touch secrets.
    """
    started = _now()
    started_ts = _now_ts()
    host = host or NullHostAdapter()
    lease_lock = lease_lock or f"shadow:{os.getpid()}"
    ttl = _lease_ttl_seconds(interval)
    path = _db_path(hermes_root)

    pass_env: dict[str, Any] = {
        "schema": PASS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id,
        "node_id": "",
        "trigger_kind": trigger_kind,
        "mode": CONTROLLER_MODE,
        "would_execute": False,
        "lease_acquired": False,
        "lease_reclaimed": False,
        "pass_result": PASS_STALE,
        "classification": "",
        "row_key": "",
        "proposed_action": "",
        "would_be_commands": [],
        "need_attention": False,
        "actions_taken": [],
        "started_at": started,
    }

    with _connect(path, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        mission._get_row(db, mission_id)  # verify the mission exists
        acq = acquire_lease(
            db, mission_id, trigger_kind, ttl=ttl, lease_lock=lease_lock
        )
        pass_env["lease_acquired"] = bool(acq.get("acquired"))
        pass_env["lease_reclaimed"] = bool(acq.get("reclaimed")) and bool(
            acq.get("acquired")
        )
        pass_env["lease"] = acq
        if not acq.get("acquired"):
            # §7.1 conflation: a live pass holds the lease; mark recheck needed.
            mark_recheck(db, mission_id)
            pass_env["pass_result"] = PASS_STALE
            # Conflation is normal loop behavior, not an anomaly: GREEN tier
            # with the conflated pass visible in telemetry (escalation_tier
            # '' on early rows predating the tier column reads as GREEN here).
            pass_env["actions_taken"].append(
                {"action": "conflate", "detail": "pass in-flight; recheck needed"}
            )
            _record_telemetry(db, pass_env)
            db.commit()
            pass_env["duration_ms"] = int((_now_ts() - started_ts) * 1000)
            return pass_env

        # Observe (authoritative sidecar channels only) + classify.
        env, frontier = build_observation(db, hermes_root, mission_id, host)
        node_id = frontier.get("node_id", "") if frontier else ""
        pass_env["node_id"] = node_id
        try:
            decision = fs.finalize(fs.classify(mission_id, node_id, env))
        except fs.ObservationError as exc:
            decision = fs.finalize(
                fs.classify(
                    mission_id,
                    node_id,
                    {
                        "mission": env["mission"],
                        "plan": env["plan"],
                        "delegation": env.get("delegation"),
                        "runner": env.get("runner"),
                        "worker_exit": {"kind": "unknown", "code": 0},
                        "last_failure_error": "",
                        "capability": env.get("capability"),
                        "breaker": env.get("breaker"),
                    },
                )
            )
            decision["classification_uncertainty"] = "invalid_observation:" + _sanitize(
                str(exc), 64
            )

        row_key = decision["row_key"]
        contract_sha = frontier.get("contract_sha256", "") if frontier else ""
        attempt_seq = (int(frontier.get("retries", 0) or 0) + 1) if frontier else 1
        cmds = _would_be_commands(
            row_key,
            mission_id,
            node_id,
            contract_sha,
            attempt_seq,
            env.get("delegation"),
        )
        pass_result = _pass_result(row_key)
        need_attention = bool(decision.get("need_attention"))

        # §12.2 alerting tier (GREEN/YELLOW/RED) for this pass.
        tier, tier_reasons = derive_pass_tier(
            mission_status=str(env["mission"].get("status", "")),
            classification=str(decision["classification"]),
            row_key=row_key,
            need_attention=need_attention,
            stuck_s=_stuck_seconds(db, mission_id),
        )
        uncertainty = str(decision.get("classification_uncertainty", ""))

        # v0.12 Pack A (design §2.2): budget D3 enforcement seam. When the
        # mission's evaluated would_pause is true AND every enforcement gate
        # passes (machine gate + Operator enabled/direct + per-mission policy
        # + confirm), execute the breaker action set (D3: pause + signal +
        # break row) and record the outcome under budget_enforcement. When
        # enforcement is disabled — the default — budget_enforcement stays
        # null so L0/L1 outputs remain byte-identical. No controller write
        # transaction is open here (acquire_lease commits internally), so the
        # executor's own write connections cannot deadlock the pass.
        budget_enforcement: dict[str, Any] | None = None
        if (
            op.env_truthy(op_mission_budget.BUDGET_HARD_BLOCK_ENV)
            and op.OperatorPolicy().enabled
            and _budget_would_pause(db, mission_id)
        ):
            enforcement_raw = op_mission_budget.enforce_budget_breaker(
                mission_id,
                hermes_root=hermes_root,
                confirm=True,
            )
            budget_enforcement = json.loads(enforcement_raw)

        # Build the durable decision envelope (decision output only).
        pass_env.update(
            {
                "node_id": node_id,
                "classification": decision["classification"],
                "failure_class": decision.get("failure_class", ""),
                "row_key": row_key,
                "proposed_action": decision["proposed_action"],
                "proposed_tool": decision["proposed_tool"],
                "verify": decision["verify"],
                "auto_retry": bool(decision.get("auto_retry")),
                "would_execute": False,
                "need_attention": need_attention,
                "pass_result": pass_result,
                "escalation_tier": tier,
                "escalation_reasons": tier_reasons,
                "would_be_commands": cmds,
                "actions_taken": [
                    {"action": "observe", "detail": "shadow observe only"}
                ],
                "classification_uncertainty": decision.get(
                    "classification_uncertainty", ""
                ),
                "decision_sha256": decision["decision_sha256"],
                "replan_proposal": decision.get("replan_proposal"),
                "observation": {
                    "mission": env["mission"],
                    "plan": env["plan"],
                    "delegation": env.get("delegation"),
                    "runner": env.get("runner"),
                },
            }
        )
        if budget_enforcement is not None:
            # §2.2: record the enforcement outcome in the pass envelope (and
            # via actions_taken, in telemetry). Key is ABSENT when enforcement
            # is disabled so L0/L1 outputs stay byte-identical to pre-v0.12.
            pass_env["budget_enforcement"] = budget_enforcement
            pass_env["actions_taken"].append(
                {
                    "action": "budget_enforce",
                    "detail": str(budget_enforcement.get("reason", ""))[:128],
                    "enforced": bool(budget_enforcement.get("enforced")),
                    "need_attention": bool(budget_enforcement.get("need_attention")),
                }
            )

        # --- the ONLY durable writes: controller_plan + controller_telemetry ---
        record = {
            "schema": fs.DECISION_SCHEMA,
            "schema_version": fs.SCHEMA_VERSION,
            "mission_id": mission_id,
            "node_id": node_id,
            "classification": decision["classification"],
            "failure_class": decision.get("failure_class", ""),
            "row_key": row_key,
            "proposed_action": decision["proposed_action"],
            "would_execute": False,
            "need_attention": need_attention,
            "decision_sha256": decision["decision_sha256"],
            "generated_at": started,
            "trigger_kind": trigger_kind,
            "mode": CONTROLLER_MODE,
            "pass_result": pass_result,
            "would_be_commands": cmds,
        }

        # ------------------------------------------------------------------
        # v0.12 slice-2 (Pack B): L2-rung execution (§2). Off unless the
        # machine gate is set; at most ONE action per pass, executed through
        # the EXISTING authority surfaces. A refusal records no execution
        # state and leaves the L0/L1 output untouched (the envelope only gains
        # the additive execution block).
        # ------------------------------------------------------------------
        tier, tier_reasons = _apply_l2_execution(
            db,
            path,
            hermes_root,
            mission_id=mission_id,
            node_id=node_id,
            row_key=row_key,
            cmds=cmds,
            confirm=confirm,
            attempt_seq=attempt_seq,
            pass_seq=int(acq.get("pass_seq", 0) or 0),
            record=record,
            pass_env=pass_env,
            tier=tier,
            tier_reasons=tier_reasons,
        )

        fs._record_decision(db, record)
        _record_telemetry(db, pass_env)
        heartbeat_pulse(hermes_root)  # liveness signal (writer side)

        # §12.2 RED routing: spool a fleet-attention INTERRUPT envelope for
        # the delivery-broker lane. Never self-sent from inside the loop; a
        # spool failure raises (INV-10: refused gates stay as loud as
        # permitted — the pass fails loudly rather than dropping the signal).
        if tier == TIER_RED:
            envelope = build_attention_envelope(
                mission_id=mission_id,
                node_id=node_id,
                classification=str(decision["classification"]),
                row_key=row_key,
                proposed_action=str(decision["proposed_action"]),
                reasons=decision.get("matched_tokens", [])[:2],
                uncertainty=uncertainty,
                tier_reasons=tier_reasons,
                pass_seq=int(acq.get("pass_seq", 0) or 0),
            )
            spool_attention_envelope(envelope, hermes_root)
            pass_env["attention_spooled"] = True

        # Renew + release the lease now that the pass is complete.
        db.commit()
        release_lease(db, mission_id, lease_lock)
        db.commit()

    pass_env["duration_ms"] = int((_now_ts() - started_ts) * 1000)
    return pass_env


# ---------------------------------------------------------------------------
# Loop driver (long-running §7.1; NOT started by this slice — HARD RULES)
# ---------------------------------------------------------------------------


def run_loop_tick(
    *,
    host: HostObservationAdapter | None = None,
    hermes_root: Path | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    lease_lock: str = "shadow-loop",
    limit: int = MAX_MISSIONS_PER_PASS,
) -> dict[str, Any]:
    """One tick of the reconciler loop: conflate → pass per mission.

    The long-running process calls this on interval (T1) and on event wake
    (T2/T3/T4). This slice does NOT start the process (no deploy / no process
    mutation), but the loop body is the real, testable unit.
    """
    path = _db_path(hermes_root)
    results: list[dict[str, Any]] = []
    with _connect(path, write=True) as db:
        work = conflate(db, limit=limit)
        for w in work:
            mid = w["mission_id"]
            try:
                result = reconcile_pass(
                    mid,
                    w["trigger_kind"],
                    host=host,
                    hermes_root=hermes_root,
                    lease_lock=lease_lock,
                    interval=interval,
                )
                results.append(result)
                if result.get("lease_acquired"):
                    consume_trigger(db, mid)
            except (ValueError, LookupError, sqlite3.Error, KeyError) as exc:
                results.append(
                    {
                        "mission_id": mid,
                        "error": _sanitize(str(exc), 160),
                        "pass_result": PASS_BLOCKED,
                        "classification": fs.CLASS_UNKNOWN,
                        "need_attention": True,
                        # INV-10: a failed pass is as loud as permitted —
                        # surfaced in-band as RED even though the pass could
                        # not record telemetry.
                        "escalation_tier": TIER_RED,
                        "escalation_reasons": ["pass_error"],
                        "would_execute": False,
                    }
                )
    return {"tick": _now(), "missions": len(results), "results": results}


# ---------------------------------------------------------------------------
# Public MCP surfaces
# ---------------------------------------------------------------------------


def reconcile_preview(
    mission_id: str,
    trigger_kind: str,
    *,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Build one shadow pass envelope without persisting controller state.

    Same observation + classification as :func:`reconcile_pass`, but no lease
    is taken, no controller_plan/controller_telemetry rows are written, no
    heartbeat is pulsed, and no attention envelope is spooled. This is the
    truthful dry-run surface: the returned envelope is what a direct pass
    WOULD decide and record. The only durable side effect is the repo-wide
    Operator audit trail (every tool call is audited; see AGENTS.md) — no
    mission, plan, delegation, or controller state is touched.

    With the L2 machine gate set, the envelope additively reports the execution
    as refused with reason ``dry_run``: a preview stays a truthful zero-write
    preview even when every other gate is satisfied (§2.1 gate 5), and it never
    dispatches.
    """
    started = _now()
    started_ts = _now_ts()
    path = _db_path(hermes_root)

    pass_env: dict[str, Any] = {
        "schema": PASS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id,
        "node_id": "",
        "trigger_kind": trigger_kind,
        "mode": CONTROLLER_MODE,
        "would_execute": False,
        "lease_acquired": False,
        "lease_reclaimed": False,
        "preview": True,
        "pass_result": PASS_STALE,
        "classification": "",
        "row_key": "",
        "proposed_action": "",
        "would_be_commands": [],
        "need_attention": False,
        "actions_taken": [{"action": "preview", "detail": "no durable writes"}],
        "started_at": started,
    }

    with _connect(path, write=False) as db:
        mission._get_row(db, mission_id)  # verify the mission exists
        env, frontier = build_observation(db, hermes_root, mission_id, NullHostAdapter())
        node_id = frontier.get("node_id", "") if frontier else ""
        pass_env["node_id"] = node_id
        try:
            decision = fs.finalize(fs.classify(mission_id, node_id, env))
        except fs.ObservationError as exc:
            decision = fs.finalize(
                fs.classify(
                    mission_id,
                    node_id,
                    {
                        "mission": env["mission"],
                        "plan": env["plan"],
                        "delegation": env.get("delegation"),
                        "runner": env.get("runner"),
                        "worker_exit": {"kind": "unknown", "code": 0},
                        "last_failure_error": "",
                        "capability": env.get("capability"),
                        "breaker": env.get("breaker"),
                    },
                )
            )
            decision["classification_uncertainty"] = "invalid_observation:" + _sanitize(
                str(exc), 64
            )

        row_key = decision["row_key"]
        contract_sha = frontier.get("contract_sha256", "") if frontier else ""
        attempt_seq = (int(frontier.get("retries", 0) or 0) + 1) if frontier else 1
        cmds = _would_be_commands(
            row_key,
            mission_id,
            node_id,
            contract_sha,
            attempt_seq,
            env.get("delegation"),
        )
        need_attention = bool(decision.get("need_attention"))
        tier, tier_reasons = derive_pass_tier(
            mission_status=str(env["mission"].get("status", "")),
            classification=str(decision["classification"]),
            row_key=row_key,
            need_attention=need_attention,
            stuck_s=_stuck_seconds(db, mission_id),
        )

        pass_env.update(
            {
                "node_id": node_id,
                "classification": decision["classification"],
                "failure_class": decision.get("failure_class", ""),
                "row_key": row_key,
                "proposed_action": decision["proposed_action"],
                "proposed_tool": decision["proposed_tool"],
                "verify": decision["verify"],
                "auto_retry": bool(decision.get("auto_retry")),
                "would_execute": False,
                "need_attention": need_attention,
                "pass_result": _pass_result(row_key),
                "escalation_tier": tier,
                "escalation_reasons": tier_reasons,
                "would_be_commands": cmds,
                "classification_uncertainty": decision.get(
                    "classification_uncertainty", ""
                ),
                "decision_sha256": decision["decision_sha256"],
                "replan_proposal": decision.get("replan_proposal"),
                "observation": {
                    "mission": env["mission"],
                    "plan": env["plan"],
                    "delegation": env.get("delegation"),
                    "runner": env.get("runner"),
                },
            }
        )

        if _execute_enabled():
            # §2.1 gate 5: a preview is a truthful zero-write preview even with
            # every other gate satisfied — it reports the refusal, never a write.
            key = _sanitize(cmds[0].get("idempotency_key", ""), 64) if cmds else ""
            pass_env["execution"] = _refusal(row_key, key, REFUSED_DRY_RUN)

    pass_env["duration_ms"] = int((_now_ts() - started_ts) * 1000)
    return pass_env


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
