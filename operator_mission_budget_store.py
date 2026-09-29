"""SQLite budget accounts, events, and audit storage helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op
from operator_mission_budget_policy import (
    ACCOUNT_SCHEMA,
    MISSION_ID_RE,
    SCHEMA_VERSION,
    _envelope_status,
    _would_block,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_budget_tables(db: sqlite3.Connection) -> None:
    """Addive budget tables. IF NOT EXISTS, so coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS budget_accounts (
            mission_id TEXT PRIMARY KEY,
            quota REAL NOT NULL,
            spend REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'tokens',
            policy_json TEXT NOT NULL DEFAULT '{}',
            account_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS budget_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            amount REAL NOT NULL,
            spend_after REAL NOT NULL,
            quota REAL NOT NULL,
            status TEXT NOT NULL,
            hard_block INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL DEFAULT 'spend_record',
            ref TEXT NOT NULL DEFAULT '',
            reason_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_budget_accounts_mission ON budget_accounts(mission_id);
        CREATE INDEX IF NOT EXISTS idx_budget_events_mission ON budget_events(mission_id, seq);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_budget_tables(db)
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
            summary=f"{tool} mission={mission_id}",
            extra={"mission_id": mission_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


def _account_view(row: sqlite3.Row) -> dict[str, Any]:
    policy = json.loads(row["policy_json"])
    spend = float(row["spend"])
    quota = float(row["quota"])
    unit = row["unit"]
    return {
        "schema": ACCOUNT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission_id": row["mission_id"],
        "spend": spend,
        "quota": quota,
        "unit": unit,
        "policy": policy,
        "account_sha256": row["account_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "envelope": _envelope_status(spend, quota, unit),
        "block": _would_block(_envelope_status(spend, quota, unit), policy),
    }


def _get_account_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute(
        "SELECT * FROM budget_accounts WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"budget account {mission_id!r} not found")
    return row


def _account_table_exists(db: sqlite3.Connection) -> bool:
    """Return whether the budget_accounts table exists (pre-first-set reads)."""
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_accounts'"
    ).fetchone()
    return row is not None


def _read_account(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    row = _get_account_row(db, mission_id)
    return _account_view(row)
