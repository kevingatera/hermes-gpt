"""Run and preview one supervised mission-controller pass."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.missions import controller_health as health
from hermes_gpt.missions import controller_l2 as _l2
from hermes_gpt.missions import failure_semantics as fs
from hermes_gpt.missions import budget as op_mission_budget
from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op
from hermes_gpt.missions.controller_attention import _l2_escalate_no_target, attention_envelopes, build_attention_envelope, spool_attention_envelope
from hermes_gpt.missions.controller_decisions import PASS_BLOCKED, PASS_NOOP, PASS_STALE, TIER_GREEN, TIER_RED, TIER_YELLOW, _pass_result, _would_be_commands, derive_pass_tier
from hermes_gpt.missions.controller_health import AGGREGATE_WINDOW_SECONDS, CONTROLLER_MODE, DEFAULT_INTERVAL_SECONDS, SCHEMA_VERSION, _lease_ttl_seconds, heartbeat_pulse
from hermes_gpt.missions.controller_l2 import EXECUTION_STATE_DISPATCHED, EXECUTION_STATE_FAILED, EXECUTION_STATE_INTENT, _execute_enabled, _execution_block, _l2_dispatch
from hermes_gpt.missions.controller_observation import HostObservationAdapter, build_observation
from hermes_gpt.missions.controller_store import MAX_MISSIONS_PER_PASS, _connect, _db_path, _now, _now_ts, _sanitize, acquire_lease, conflate, consume_trigger, mark_recheck, release_lease

PASS_SCHEMA = "hermes.controller-pass/v1"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


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
