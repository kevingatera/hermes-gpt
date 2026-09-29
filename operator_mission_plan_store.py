"""SQLite persistence and bounded read views for MissionPlans."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op
from operator_mission_plan_schema import (
    MISSION_ID_RE,
    NODE_SCHEMA,
    PLAN_SCHEMA,
    SCHEMA_VERSION,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_plan_tables(db: sqlite3.Connection) -> None:
    """Additive plan tables. IF NOT EXISTS, so coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS mission_plans (
            mission_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            plan_sha256 TEXT NOT NULL DEFAULT '',
            decomposition TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS plan_nodes (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL DEFAULT '',
            capability_req TEXT NOT NULL DEFAULT '{}',
            budget TEXT NOT NULL DEFAULT '{}',
            deps TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL,
            lease_lock TEXT NOT NULL DEFAULT '',
            lease_expires TEXT NOT NULL DEFAULT '',
            epoch INTEGER NOT NULL DEFAULT 0,
            failure_kind TEXT NOT NULL DEFAULT '',
            retries INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_plan_nodes_mission_state ON plan_nodes(mission_id, state);
        CREATE INDEX IF NOT EXISTS idx_plans_status ON mission_plans(status, updated_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_plan_tables(db)
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


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


def _get_plan_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute(
        "SELECT * FROM mission_plans WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"mission plan {mission_id!r} not found")
    return row


def _nodes_rows(db: sqlite3.Connection, mission_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM plan_nodes WHERE mission_id=? ORDER BY node_id", (mission_id,)
    ).fetchall()


def _plan_view(
    db: sqlite3.Connection, row: sqlite3.Row, *, include_nodes: bool = True
) -> dict[str, Any]:
    plan = json.loads(row["plan_json"])
    value: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        **plan,
        "version": int(row["version"]),
        "status": row["status"],
        "plan_sha256": row["plan_sha256"],
        "decomposition": row["decomposition"] or plan.get("decomposition", ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_nodes:
        node_rows = {r["node_id"]: r for r in _nodes_rows(db, row["mission_id"])}
        nodes: list[dict[str, Any]] = []
        for defn in plan.get("nodes", []):
            nid = defn["node_id"]
            n = node_rows.get(nid)
            if n is None:
                continue
            nodes.append(
                {
                    "schema": NODE_SCHEMA,
                    "node_id": nid,
                    **defn,
                    "state": n["state"],
                    "lease_lock": n["lease_lock"],
                    "lease_expires": n["lease_expires"],
                    "epoch": int(n["epoch"]),
                    "failure_kind": n["failure_kind"],
                    "retries": int(n["retries"]),
                }
            )
        value["nodes"] = nodes
        value["node_count"] = len(nodes)
    return value
