"""Build a read-only preview of the mission-controller decision."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_gpt.missions import failure_semantics as fs
from hermes_gpt.missions import runtime as mission
from hermes_gpt.missions.controller_decisions import PASS_STALE, _pass_result, _would_be_commands, derive_pass_tier
from hermes_gpt.missions.controller_health import CONTROLLER_MODE, SCHEMA_VERSION
from hermes_gpt.missions.controller_l2 import REFUSED_DRY_RUN, _execute_enabled, _refusal
from hermes_gpt.missions.controller_observation import build_observation
from hermes_gpt.missions.controller_pass import PASS_SCHEMA, NullHostAdapter, _stuck_seconds
from hermes_gpt.missions.controller_store import _connect, _db_path, _now, _now_ts, _sanitize


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
