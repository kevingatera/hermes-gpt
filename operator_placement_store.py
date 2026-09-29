"""Mission database access and durable placement decision records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import operator_mission_budget as budget
import operator_mission_runtime as mission
import operator_policy as op
from operator_placement_common import MISSION_ID_RE, NODE_ID_RE, _clean_text, _now


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_placement_tables(db: sqlite3.Connection) -> None:
    """Additive placement table. IF NOT EXISTS, coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS placement_decisions (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            classification TEXT NOT NULL,
            top_candidate TEXT NOT NULL DEFAULT '',
            assigned_agent TEXT NOT NULL DEFAULT '',
            decision_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_placement_mission ON placement_decisions(mission_id, updated_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_placement_tables(db)
        return db
    return mission._connect(path, write=False)


def _begin_write(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
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
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id} node={node_id}",
            extra={"mission_id": mission_id, "node_id": node_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(
    exc: Exception,
    code: str,
    action: str,
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        op.error_from_exception(
            exc,
            layer="operator",
            code=code,
            suggested_action=action,
            extra=extra,
        )
    )


def _read_node_requirement(
    db: sqlite3.Connection, mission_id: str, node_id: str
) -> dict[str, Any]:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    if not NODE_ID_RE.fullmatch(node_id):
        raise ValueError("node_id is invalid")
    row = db.execute(
        "SELECT capability_req, budget, state FROM plan_nodes WHERE mission_id=? AND node_id=?",
        (mission_id, node_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"plan node {node_id!r} not found for mission {mission_id!r}")
    cap_req = json.loads(row["capability_req"] or "{}")
    budget_req = json.loads(row["budget"] or "{}")
    return {
        "profile": cap_req.get("profile", ""),
        "skills": cap_req.get("skills", []),
        "authorization_class": cap_req.get("authorization_class", "reversible_write"),
        "budget": budget_req,
    }


def _read_mission_priority(db: sqlite3.Connection, mission_id: str) -> int:
    try:
        row = db.execute(
            "SELECT spec_json FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        spec = json.loads(row["spec_json"])
    except (ValueError, TypeError):
        return 0
    priority = spec.get("priority", 0) if isinstance(spec, dict) else 0
    try:
        return max(0, min(int(priority), 9))
    except (TypeError, ValueError):
        return 0


def _read_budget_context(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    try:
        acct = budget._read_account(db, mission_id)
    except (LookupError, ValueError, sqlite3.Error):
        return {}
    env = acct.get("envelope", {})
    return {
        "remaining_tokens": float(env.get("remaining", 0.0) or 0.0),
        "quota_tokens": float(acct.get("quota", 0.0) or 0.0),
        "unit": acct.get("unit", ""),
        "crosses_envelope": bool(env.get("crosses_envelope")),
    }


def _placement_table_exists(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='placement_decisions'"
    ).fetchone()
    return row is not None


def _upsert_decision(db: sqlite3.Connection, decision: dict[str, Any]) -> None:
    now = _now()
    top = decision.get("top_candidate") or {}
    db.execute(
        """
        INSERT INTO placement_decisions
            (mission_id, node_id, decision_json, classification, top_candidate,
             assigned_agent, decision_sha256, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mission_id, node_id) DO UPDATE SET
            decision_json=excluded.decision_json,
            classification=excluded.classification,
            top_candidate=excluded.top_candidate,
            assigned_agent=excluded.assigned_agent,
            decision_sha256=excluded.decision_sha256,
            updated_at=excluded.updated_at
        """,
        (
            decision["mission_id"],
            decision["node_id"],
            json.dumps(decision, ensure_ascii=False, sort_keys=True),
            decision["classification"],
            top.get("entity_id", ""),
            decision["assigned_agent"],
            decision["decision_sha256"],
            now,
            now,
        ),
    )


def _decision_view(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["decision_json"])


def _node_def(path: Path, mission_id: str, node_id: str) -> dict[str, str]:
    """Derive a plan node's ``kind``/``owner`` from the stored plan JSON (read-only)."""
    default = {"kind": "single", "owner": ""}
    try:
        with _connect(path, write=False) as db:
            row = db.execute(
                "SELECT plan_json FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    try:
        plan_doc = json.loads(row["plan_json"])
    except (ValueError, TypeError):
        return default
    for node in plan_doc.get("nodes", []) or []:
        if node.get("node_id") == node_id:
            return {
                "kind": _clean_text(
                    node.get("kind", "single"), field="kind", maximum=16
                )
                or "single",
                "owner": _clean_text(node.get("owner", ""), field="owner", maximum=64),
            }
    return default
