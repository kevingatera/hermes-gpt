"""Controller heartbeat and aggregate health reporting."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_gpt.missions import failure_semantics as fs
from hermes_gpt.missions.controller_decisions import PASS_BLOCKED, PASS_ESCALATED, PASS_RECOVERED, TIER_GREEN, TIER_RED, TIER_YELLOW
from hermes_gpt.missions.controller_l2 import REFUSED_NO_TARGET, _execute_enabled
from hermes_gpt.missions.controller_store import MAX_MISSIONS_PER_PASS, _connect, _db_path, _lease_info, _now, _now_ts, _root

SCHEMA_VERSION = "0.9-controller.1"
CONTROLLER_MODE = "shadow/observe"
DEFAULT_INTERVAL_SECONDS = 90.0
MIN_INTERVAL_SECONDS = 15.0
MAX_PASS_DURATION_SECONDS = 300.0
HEARTBEAT_STALE_SECONDS = 180.0
AGGREGATE_WINDOW_SECONDS = 24 * 3600.0


def _lease_ttl_seconds(interval: float = DEFAULT_INTERVAL_SECONDS) -> float:
    """TTL = min(max_pass_duration, reconcile_interval*2) (§7.3)."""
    return max(MIN_INTERVAL_SECONDS, min(MAX_PASS_DURATION_SECONDS, interval * 2.0))



# ---------------------------------------------------------------------------
# §12.2 telemetry / health surface: heartbeat file + controller_status()
# ---------------------------------------------------------------------------


def _heartbeat_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "controller_heartbeat.json"


def heartbeat_pulse(
    hermes_root: Path | None = None, *, interval: float = DEFAULT_INTERVAL_SECONDS
) -> dict[str, Any]:
    """Write/refresh the watchdog liveness file (§12.2)."""
    path = _heartbeat_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "live": True,
        "mode": CONTROLLER_MODE,
        "pid": os.getpid(),
        "last_beat": _now(),
        "ts": _now_ts(),
        "interval": interval,
        "lease_ttl": _lease_ttl_seconds(interval),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _read_heartbeat(hermes_root: Path | None) -> dict[str, Any]:
    path = _heartbeat_path(hermes_root)
    if not path.is_file():
        return {"live": False, "last_beat": "", "age_s": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"live": False, "last_beat": "", "age_s": None}
    age = _now_ts() - float(payload.get("ts", 0))
    payload["age_s"] = age
    payload["live"] = bool(payload.get("live")) and age < HEARTBEAT_STALE_SECONDS
    return payload


def controller_status(
    hermes_root: Path | None = None,
    *,
    attention_count: Callable[[Path | None], int],
) -> dict[str, Any]:
    """Read-only aggregate health (§12.2) + GREEN/YELLOW/RED tier rollup."""
    path = _db_path(hermes_root)
    hb = _read_heartbeat(hermes_root)
    status: dict[str, Any] = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "mode": CONTROLLER_MODE,
        "would_execute": False,
        "controller_live": bool(hb.get("live")),
        "last_pass_age_s": hb.get("age_s"),
        "last_beat": hb.get("last_beat", ""),
    }
    empty = {
        "active_passes": 0,
        "missions_reconciled_24h": 0,
        "passes_24h": 0,
        "recoveries": 0,
        "escalations": 0,
        "retry_storms_prevented": 0,
        "stale_lease_reclaims": 0,
        "classification_uncertainty": 0,
        "per_class": {},
        "per_tier": {},
        "attention_spooled": 0,
        "execution_enabled": _execute_enabled(),
        "executions": {
            "executed": 0,
            "dispatched": 0,
            "failed": 0,
            "refused": 0,
            "refused_by_reason": {},
            "placement_escalations": 0,
        },
        "leases": [],
    }
    if not path.is_file():
        status.update(empty)
        status["tier"] = _rollup_tier(status, hb)
        status["tier_reasons"] = _rollup_reasons(status, hb)
        return status
    with _connect(path, write=False) as db:
        try:
            active = db.execute(
                "SELECT COUNT(*) AS c FROM controller_pass_lease"
            ).fetchone()
        except sqlite3.Error:
            active = {"c": 0}
        cutoff = datetime.fromtimestamp(
            _now_ts() - AGGREGATE_WINDOW_SECONDS, tz=timezone.utc
        ).isoformat()
        try:
            tele = db.execute(
                "SELECT pass_result,classification,need_attention,escalation_tier,"
                "lease_reclaimed,COUNT(*) AS c "
                "FROM controller_telemetry WHERE created_at>=? "
                "GROUP BY pass_result,classification,need_attention,escalation_tier,lease_reclaimed",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            tele = []
        try:
            tele_all = db.execute(
                "SELECT pass_result,COUNT(*) AS c FROM controller_telemetry "
                "GROUP BY pass_result"
            ).fetchall()
        except sqlite3.Error:
            tele_all = []
        try:
            exec_rows = db.execute(
                "SELECT executed_result,executed_refused_reason,COUNT(*) AS c "
                "FROM controller_telemetry WHERE created_at>=? "
                "GROUP BY executed_result,executed_refused_reason",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            exec_rows = []
        try:
            reconciled = db.execute(
                "SELECT COUNT(DISTINCT mission_id) AS c FROM controller_telemetry "
                "WHERE created_at>=?",
                (cutoff,),
            ).fetchone()
        except sqlite3.Error:
            reconciled = {"c": 0}
        try:
            leases = [
                _lease_info(db, r["mission_id"])
                for r in db.execute(
                    "SELECT mission_id FROM controller_pass_lease ORDER BY mission_id LIMIT ?",
                    (MAX_MISSIONS_PER_PASS,),
                ).fetchall()
            ]
        except sqlite3.Error:
            leases = []
        per_class: dict[str, int] = {}
        per_result: dict[str, int] = {}
        per_tier: dict[str, int] = {}
        reclaims = 0
        passes = 0
        uncertainty = _count_uncertainty(db, cutoff)
        # §4: bounded L2-rung execution counters (executed / dispatched / failed /
        # refused-by-reason / placement escalations) derived from the existing
        # telemetry surface — no new store.
        executed_count = 0
        dispatched_count = 0
        failed_count = 0
        refused_by_reason: dict[str, int] = {}
        placement_escalations = 0
        for row in exec_rows:
            n = int(row["c"])
            result = row["executed_result"] or ""
            reason = row["executed_refused_reason"] or ""
            if result == "dispatched":
                executed_count += n
                dispatched_count += n
            elif result == "failed":
                executed_count += n
                failed_count += n
            elif reason:
                refused_by_reason[reason] = refused_by_reason.get(reason, 0) + n
                if reason == REFUSED_NO_TARGET:
                    placement_escalations += n
        for row in tele:
            n = int(row["c"])
            passes += n
            per_result[row["pass_result"]] = per_result.get(row["pass_result"], 0) + n
            per_class[row["classification"]] = (
                per_class.get(row["classification"], 0) + n
            )
            tier = row["escalation_tier"] or ""
            if tier:
                per_tier[tier] = per_tier.get(tier, 0) + n
            reclaims += n if int(row["lease_reclaimed"] or 0) else 0
        status.update(
            {
                "active_passes": int(active["c"]),
                "missions_reconciled_24h": int(reconciled["c"]),
                "passes_24h": passes,
                "recoveries": per_result.get(PASS_RECOVERED, 0),
                "escalations": per_result.get(PASS_ESCALATED, 0)
                + per_result.get(PASS_BLOCKED, 0),
                "retry_storms_prevented": per_result.get(PASS_BLOCKED, 0),
                "stale_lease_reclaims": reclaims,
                "classification_uncertainty": uncertainty,
                "per_class": per_class,
                "per_tier": per_tier,
                "pass_results": per_result,
                "pass_results_all_time": {
                    r["pass_result"]: int(r["c"]) for r in tele_all
                },
                "leases": leases,
                "attention_spooled": attention_count(hermes_root),
                "execution_enabled": _execute_enabled(),
                "executions": {
                    "executed": executed_count,
                    "dispatched": dispatched_count,
                    "failed": failed_count,
                    "refused": sum(refused_by_reason.values()),
                    "refused_by_reason": refused_by_reason,
                    "placement_escalations": placement_escalations,
                },
            }
        )
        status["tier"] = _rollup_tier(status, hb)
        status["tier_reasons"] = _rollup_reasons(status, hb)
    return status


def _rollup_tier(status: dict[str, Any], hb: dict[str, Any]) -> str:
    """Controller-level tier: worst of liveness and pass-tier signals."""
    if not status.get("controller_live"):
        # No fresh heartbeat → the watchdog surface itself is dark. §12.2
        # treats an unobservable controller as RED (fail-closed liveness).
        return TIER_RED
    per_tier: dict[str, int] = status.get("per_tier", {})
    if per_tier.get(TIER_RED, 0) > 0:
        return TIER_RED
    if per_tier.get(TIER_YELLOW, 0) > 0:
        return TIER_YELLOW
    return TIER_GREEN


def _rollup_reasons(status: dict[str, Any], hb: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not status.get("controller_live"):
        reasons.append("heartbeat_stale_or_missing")
    per_tier: dict[str, int] = status.get("per_tier", {})
    if per_tier.get(TIER_RED, 0) > 0:
        reasons.append(f"red_passes_24h={per_tier[TIER_RED]}")
    if per_tier.get(TIER_YELLOW, 0) > 0:
        reasons.append(f"yellow_passes_24h={per_tier[TIER_YELLOW]}")
    if int(status.get("classification_uncertainty", 0) or 0) > 0:
        reasons.append(
            f"classification_uncertainty={status['classification_uncertainty']}"
        )
    if not reasons:
        reasons.append("steady_state")
    return reasons[:8]


def _count_uncertainty(db: sqlite3.Connection, cutoff: str | None = None) -> int:
    try:
        if cutoff is not None:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM controller_telemetry "
                "WHERE classification=? AND created_at>=?",
                (fs.CLASS_UNKNOWN, cutoff),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM controller_telemetry WHERE classification=?",
                (fs.CLASS_UNKNOWN,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0
