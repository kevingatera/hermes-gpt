"""Delegation, failure, and pending-approval Mission Control surfaces."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.missions.common import _LOG_TAIL_BYTES, _MAX_APPROVALS, _MAX_DELEGATIONS, _MAX_ERRORS, _MAX_KANBAN_RUNS, _mission_envelope, _prompt_meta, _resolve_root, _sanitize_error, _truncate
from hermes_gpt.missions.sources import _action_items_path, _cron_executions_db, _errors_log, _interrupted_turns_path, _iter_profiles, _kanban_boards_dir, _open_ro, _profile_home, _read_json_file, _state_db, _vault_access_requests


def _async_delegations_for(
    home: Path, warnings: list[str], scope: str
) -> list[dict[str, Any]]:
    """Read async_delegations from one state.db, redacted (never bodies)."""
    out: list[dict[str, Any]] = []
    try:
        conn = _open_ro(_state_db(home))
        try:
            rows = conn.execute(
                "SELECT delegation_id, origin_session, parent_session_id, state, dispatched_at, completed_at "
                "FROM async_delegations"
            ).fetchall()
            for row in rows:
                out.append(
                    {
                        "delegation_id": str(row["delegation_id"]),
                        "state": str(row["state"] or "unknown"),
                        "origin_session": row["origin_session"],
                        "parent_session": row["parent_session_id"],
                        "dispatched_at": row["dispatched_at"],
                        "completed_at": row["completed_at"],
                        "scope": scope,
                    }
                )
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error, OSError):
        pass
    return out


def _kanban_runs_for(hermes_root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Aggregate kanban task_runs across boards (G3), normalized with board slug."""
    out: list[dict[str, Any]] = []
    boards_dir = _kanban_boards_dir(hermes_root)
    if not boards_dir.is_dir():
        return out
    try:
        for board in sorted(p for p in boards_dir.iterdir() if p.is_dir()):
            db = board / "kanban.db"
            if not db.exists():
                continue
            slug = board.name
            try:
                conn = _open_ro(db)
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
                    rows = conn.execute(
                        "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?",
                        (_MAX_KANBAN_RUNS,),
                    ).fetchall()
                    for row in rows:
                        out.append(
                            {
                                "task_id": str(
                                    row["task_id"]
                                    if "task_id" in cols and row["task_id"] is not None
                                    else "unknown"
                                ),
                                "board": slug,
                                "assignee": row["assignee"]
                                if "assignee" in cols
                                else None,
                                "status": row["status"] if "status" in cols else None,
                                "outcome": row["outcome"]
                                if "outcome" in cols
                                else None,
                                "error": _sanitize_error(row["error"])
                                if "error" in cols
                                else None,
                                "started_at": row["started_at"]
                                if "started_at" in cols
                                else None,
                                "ended_at": row["ended_at"]
                                if "ended_at" in cols
                                else None,
                            }
                        )
                finally:
                    conn.close()
            except (FileNotFoundError, sqlite3.Error, OSError):
                continue
    except OSError:
        pass
    return out


def hermes_mission_delegations(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Delegated work + kanban runs (G6/G7), never bodies (design §6.4)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    delegations: list[dict[str, Any]] = []

    for profile in _iter_profiles(root):
        try:
            delegations.extend(
                _async_delegations_for(_profile_home(profile, root), warnings, profile)
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"delegations:{profile}:{exc.__class__.__name__}")

    kanban_runs = _kanban_runs_for(root, warnings)

    by_state: dict[str, int] = {}
    for d in delegations:
        st = d["state"]
        by_state[st] = by_state.get(st, 0) + 1

    return _mission_envelope(
        tool="hermes_mission_delegations",
        surface="delegations",
        data={
            "delegations": delegations[:_MAX_DELEGATIONS],
            "kanban_runs": kanban_runs,
            "by_state": by_state,
        },
        counts={
            "total": len(delegations),
            "in_flight": by_state.get("running", 0) + by_state.get("in_progress", 0),
            "completed": by_state.get("completed", 0),
            "failed": by_state.get("error", 0) + by_state.get("failed", 0),
            "unknown": by_state.get("unknown", 0),
            "kanban_runs": len(kanban_runs),
        },
        warnings=warnings,
        trace_id=trace_id,
    )


def _recent_errors(hermes_root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Tail of errors.log, redacted + bounded."""
    out: list[dict[str, Any]] = []
    log = _errors_log(hermes_root)
    if not log.exists():
        return out
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            lines = fh.read().splitlines()
        for line in lines[-_MAX_ERRORS:]:
            if not line.strip():
                continue
            out.append(
                {
                    "source": "errors.log",
                    "message": _sanitize_error(line),
                }
            )
    except OSError:
        pass
    return out


def hermes_mission_failures(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Failures across sources, bounded + recent (design §6.4 failures)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []

    # errors.log tail.
    errors.extend(_recent_errors(root, warnings))

    # Cron executions failed.
    for profile in _iter_profiles(root):
        try:
            conn = _open_ro(_cron_executions_db(_profile_home(profile, root)))
            try:
                for row in conn.execute(
                    "SELECT job_id, error, started_at FROM executions WHERE status='failed' "
                    "ORDER BY started_at DESC LIMIT ?",
                    (_MAX_ERRORS,),
                ):
                    errors.append(
                        {
                            "source": f"cron:{profile}",
                            "job_id": str(row["job_id"] or ""),
                            "message": _sanitize_error(row["error"]),
                            "timestamp": row["started_at"],
                        }
                    )
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError):
            continue

    # Kanban run errors.
    for run in _kanban_runs_for(root, warnings):
        if run.get("error"):
            errors.append(
                {
                    "source": f"kanban:{run['board']}",
                    "job_id": run["task_id"],
                    "message": run["error"],
                }
            )

    by_source: dict[str, int] = {}
    for e in errors:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1

    return _mission_envelope(
        tool="hermes_mission_failures",
        surface="failures",
        data={"recent_errors": errors[:_MAX_ERRORS], "by_source": by_source},
        counts={"recent_error_count": len(errors)},
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_approvals(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Pending approvals via approval-inbox aggregation contract (G4), view-only."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    approvals: list[dict[str, Any]] = []

    # interrupted_turns.json (HIGH): status + prompt sha only.
    it_path = _interrupted_turns_path(root)
    data = _read_json_file(it_path)
    if data:
        for key, entry in data.items():
            if isinstance(entry, dict):
                pm = _prompt_meta(entry.get("prompt"))
                approvals.append(
                    {
                        "kind": "interrupted_turn",
                        "source": "interrupted_turns.json",
                        "id": str(key),
                        "status": "pending",
                        "created_at": entry.get("started_at"),
                        **pm,
                    }
                )

    # Vault access_requests (metadata only).
    ar_data = _read_json_file(_vault_access_requests(root))
    if ar_data:
        reqs = ar_data.get("requests", []) if isinstance(ar_data, dict) else []
        for req in reqs if isinstance(reqs, list) else []:
            if isinstance(req, dict) and req.get("status", "pending") == "pending":
                secret = str(req.get("secret") or req.get("secret_sha256") or "")
                approvals.append(
                    {
                        "kind": "vault_access_request",
                        "source": "vault",
                        "id": str(req.get("id") or ""),
                        "status": "pending",
                        "created_at": req.get("created_at"),
                        "prompt_len": 0,
                        "prompt_sha256": hashlib.sha256(
                            secret.encode("utf-8", "replace")
                        ).hexdigest()
                        if secret
                        else "",
                    }
                )

    # action-items.json (MED): status + label only, no blocker bodies.
    ai_data = _read_json_file(_action_items_path())
    if ai_data:
        items = ai_data.get("items", []) if isinstance(ai_data, dict) else ai_data
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("status", "open") == "open":
                approvals.append(
                    {
                        "kind": "action_item",
                        "source": "action-items.json",
                        "id": str(item.get("id") or ""),
                        "status": "open",
                        "title": _truncate(str(item.get("title") or ""), 160),
                        "created_at": item.get("created_at"),
                    }
                )

    by_source: dict[str, int] = {}
    for a in approvals:
        by_source[a["source"]] = by_source.get(a["source"], 0) + 1

    return _mission_envelope(
        tool="hermes_mission_approvals",
        surface="approvals",
        data={"approvals": approvals[:_MAX_APPROVALS], "by_source": by_source},
        counts={"count": len(approvals)},
        warnings=warnings,
        trace_id=trace_id,
    )
