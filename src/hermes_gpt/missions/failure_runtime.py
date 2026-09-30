"""Policy-gated MCP surfaces and persistence for failure decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op
from hermes_gpt.missions.failure_catalog import CLASS_NONE, CLASS_NONE_APPROVAL, CLASS_NONE_COMPLETION, CLASS_NONE_DISPATCHABLE, CLASS_SEMANTIC, CLASS_UNKNOWN, MATRIX, MAX_OBSERVATION_JSON, MAX_REPLAN_ATTEMPTS_DEFAULT, MISSION_ID_RE, NODE_ID_RE, SCHEMA_VERSION, TAXONOMY
from hermes_gpt.missions.failure_classifier import ObservationError, _canonical, _now, _sanitize, _validate_envelope, classify, finalize


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(exc, layer="operator", code=code, suggested_action=action)
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
# Durable store: controller_plan rows (decision output only). Additive,
# IF NOT EXISTS, coexists with the Mission runtime store (§6.2).
# ---------------------------------------------------------------------------


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS controller_plan (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL,
            classification TEXT NOT NULL,
            failure_class TEXT NOT NULL DEFAULT '',
            row_key TEXT NOT NULL DEFAULT '',
            proposed_action TEXT NOT NULL DEFAULT '',
            would_execute INTEGER NOT NULL DEFAULT 0,
            need_attention INTEGER NOT NULL DEFAULT 0,
            decision_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id, decision_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_controller_plan_mission
            ON controller_plan(mission_id, created_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_tables(db)
        return db
    return mission._connect(path, write=False)


def _record_decision(db: sqlite3.Connection, decision: dict[str, Any]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO controller_plan("
        "mission_id,node_id,decision_json,classification,failure_class,row_key,"
        "proposed_action,would_execute,need_attention,decision_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision["mission_id"],
            decision["node_id"],
            _canonical(decision),
            decision["classification"],
            decision.get("failure_class", ""),
            decision.get("row_key", ""),
            _sanitize(decision.get("proposed_action", ""), 200),
            1 if decision.get("would_execute") else 0,
            1 if decision.get("need_attention") else 0,
            decision["decision_sha256"],
            decision.get("generated_at") or _now(),
        ),
    )


# ---------------------------------------------------------------------------
# Public tool surfaces (JSON envelopes; read_only / workspace+direct gated)
# ---------------------------------------------------------------------------


def hermes_failure_classify(
    mission_id: str,
    node_id: str,
    observation_json: str,
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Classify an observation envelope and propose the smallest recovery action.

    Decision output only: ``would_execute`` is always ``False`` and no action
    is taken. Dry-run (default) requires ``read_only`` and records nothing;
    recording the decision to ``controller_plan`` requires ``workspace`` +
    ``direct`` + ``confirm``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        if node_id and not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        if not isinstance(observation_json, str) or len(observation_json) > MAX_OBSERVATION_JSON:
            raise ValueError("observation_json is missing or exceeds the size bound")
        try:
            observation = json.loads(observation_json)
        except json.JSONDecodeError as exc:
            raise ObservationError(f"observation_json is not valid JSON: {exc}") from exc

        env = _validate_envelope(observation)
        decision = finalize(classify(mission_id, node_id or "", env))
        decision["dry_run"] = True
        decision["changed"] = False

        effective_dry = policy.effective_dry_run(dry_run)
        if effective_dry:
            _audit(
                "hermes_failure_classify",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                node_id=node_id,
                extra={
                    "classification": decision["classification"],
                    "row_key": decision["row_key"],
                    "decision_sha256": decision["decision_sha256"],
                },
            )
            return json.dumps(decision, ensure_ascii=False, indent=2)

        # Recording path: workspace + direct + confirm; writes ONLY a
        # controller_plan row. No mission/plan/delegation mutation exists here.
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not confirm:
            raise PermissionError("recording a failure decision requires confirm=true")
        decision["dry_run"] = False
        decision["changed"] = True
        decision["persisted"] = {"mission_id": mission_id, "node_id": node_id or ""}
        decision["decision_sha256"] = hashlib.sha256(
            _canonical({k: v for k, v in decision.items() if k != "generated_at"}).encode()
        ).hexdigest()
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            mission._get_row(db, mission_id)  # verify the mission exists
            _record_decision(db, decision)
            db.commit()

        _audit(
            "hermes_failure_classify",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            node_id=node_id,
            extra={
                "classification": decision["classification"],
                "row_key": decision["row_key"],
                "decision_sha256": decision["decision_sha256"],
            },
        )
        return json.dumps(decision, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
        ObservationError,
    ) as exc:
        _audit(
            "hermes_failure_classify",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
        )
        return _error(
            exc,
            "FAILURE_CLASSIFY_REJECTED",
            "Check mission/node ids, the observation envelope shape, and Operator policy.",
        )


def hermes_failure_taxonomy() -> str:
    """Read-only: the authoritative 8-class taxonomy (§11.1) + unknown bucket."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "classes": {
                name: {
                    "meaning": spec["meaning"],
                    "action": spec["action"],
                    "auto_retry": spec["auto_retry"],
                }
                for name, spec in TAXONOMY.items()
            },
            "non_failure_classifications": {
                CLASS_NONE_DISPATCHABLE: "ready child; dispatch is the smallest action",
                CLASS_NONE_APPROVAL: "all children terminal; approval gate",
                CLASS_NONE_COMPLETION: "all children terminal; verified; completion request",
                CLASS_NONE: "steady state; recheck next trigger",
            },
            "unknown_bucket": {
                "classification": CLASS_UNKNOWN,
                "policy": "fail-closed: blocked + need_attention + classification_uncertainty",
            },
            "replan_bound": {
                "max_attempts": MAX_REPLAN_ATTEMPTS_DEFAULT,
                "only_for_class": CLASS_SEMANTIC,
                "path": "proposal through existing decompose/advance tools (D8)",
            },
        }
        _audit(
            "hermes_failure_taxonomy",
            policy,
            dry_run=True,
            success=True,
            changed=False,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        return _error(exc, "FAILURE_TAXONOMY_REJECTED", "Operator policy must be enabled.")


def hermes_recovery_matrix(row_key: str = "") -> str:
    """Read-only: the deterministic smallest-first recovery matrix (§11.2)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if row_key:
            if row_key not in MATRIX:
                raise LookupError(f"unknown matrix row: {row_key}")
            rows = {row_key: MATRIX[row_key]}
        else:
            rows = dict(MATRIX)
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "smallest_first": True,
            "every_action_is_a_request": True,
            "would_execute": False,
            "rows": rows,
        }
        _audit(
            "hermes_recovery_matrix",
            policy,
            dry_run=True,
            success=True,
            changed=False,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, LookupError, OSError) as exc:
        return _error(exc, "RECOVERY_MATRIX_REJECTED", "Check the row key and Operator policy.")


def hermes_controller_plan_list(
    mission_id: str,
    limit: int = 50,
    *,
    hermes_root: Path | None = None,
) -> str:
    """Read-only: recorded failure decisions (controller_plan rows)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        rows: list[dict[str, Any]] = []
        if path.is_file():
            with _connect(path, write=False) as db:
                try:
                    cursor = db.execute(
                        "SELECT node_id,classification,failure_class,row_key,proposed_action,"
                        "would_execute,need_attention,decision_sha256,created_at "
                        "FROM controller_plan WHERE mission_id=? ORDER BY created_at DESC LIMIT ?",
                        (mission_id, limit),
                    )
                except sqlite3.Error:
                    cursor = []
                rows = [
                    {
                        "node_id": r["node_id"],
                        "classification": r["classification"],
                        "failure_class": r["failure_class"],
                        "row_key": r["row_key"],
                        "proposed_action": r["proposed_action"],
                        "would_execute": bool(r["would_execute"]),
                        "need_attention": bool(r["need_attention"]),
                        "decision_sha256": r["decision_sha256"],
                        "created_at": r["created_at"],
                    }
                    for r in cursor
                ]
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "mission_id": mission_id,
            "count": len(rows),
            "decisions": rows,
        }
        _audit(
            "hermes_controller_plan_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"count": len(rows)},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "CONTROLLER_PLAN_LIST_REJECTED", "Check the mission id and Operator policy.")
