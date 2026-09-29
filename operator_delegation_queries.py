"""Read-only delegation lookups and bounded listings."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_delegation_store import (
    DELEGATION_ID_RE,
    MAX_LIST,
    SCHEMA_VERSION,
    STATES,
    _audit,
    _connect,
    _db_path,
    _error,
    _get_row,
    _surface,
)


def hermes_delegation_get(delegation_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not DELEGATION_ID_RE.fullmatch(delegation_id or ""):
            raise ValueError("delegation_id is invalid")
        path = _db_path(hermes_root)
        if not path.is_file():
            raise LookupError(f"delegation {delegation_id!r} was not found")
        with _connect(path, write=False) as db:
            row = _get_row(db, delegation_id)
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT event_type,from_state,to_state,backend_state,observed_sha256,created_at FROM delegation_events WHERE delegation_id=? ORDER BY seq DESC LIMIT 100",
                    (delegation_id,),
                ).fetchall()
            ]
        _audit(
            tool="hermes_delegation_get",
            policy=policy,
            dry_run=True,
            success=True,
            changed=False,
            delegation_id=delegation_id,
            task_id=row["task_id"],
            backend=row["backend"],
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "delegation": _surface(row, events=events),
            },
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "DELEGATION_GET_FAILED",
            "Check delegation id and Operator read access.",
        )


def hermes_delegation_list(
    mission_id: str = "",
    state: str = "",
    limit: int = 50,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if state and state not in STATES:
            raise ValueError("state filter is invalid")
        limit = max(1, min(int(limit), MAX_LIST))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "delegations": [],
                    "count": 0,
                }
            )
        clauses: list[str] = []
        params: list[Any] = []
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(path, write=False) as db:
            rows = db.execute(
                f"SELECT * FROM delegations{where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "delegations": [_surface(r) for r in rows],
                "count": len(rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "DELEGATION_LIST_FAILED", "Check filters and Operator read access."
        )
