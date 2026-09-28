"""Mission Control aggregation layer for hermes-gpt v0.6 (M0).

This module implements the read-only ``hermes_mission_*`` surface: a structured,
bounded, authenticated view of the entire Hermes ecosystem (profiles, fleet
agents, Codex jobs, cron activity, delegated work, failures, pending approvals,
health, vault, usage, and the operator audit trail) exposed to trusted clients
(ChatGPT first).

Design contract: v0.6 Mission Control design (M0). The hard
constraints from the v0.6 data-risk review (P0-1..P0-3) and the
v0.6 plan §4.1 are enforced here:

- Read-only by construction: every SQLite store is opened ``file:...?mode=ro``;
  no write / dry-run / apply arguments; no mutating shell calls.
- No raw bodies: no message, memory, transcript, request-dump, or profile-secret
  content ever crosses the surface — summaries / aggregates only.
- No secrets / tokens: auth.json, .env, a2a/MCP tokens, ``hermes status`` API
  keys, and Vault secrets never appear in the view model.
- Every mission call is audited (design D10).
- Conditional surfaces report ``available:false`` + ``reason`` rather than
  failing (design D9).
- Output is bounded (§8.4 caps) with ``truncated``/``count_total``.

Conventions match the existing ``operator_*`` modules:
- imports ``operator_policy as op`` for policy/audit/redaction/error helpers;
- every public function returns a JSON ``str`` like the other ``hermes_*``
  wrappers;
- ``_default_hermes_root()`` is passed through; each public function takes an
  optional ``hermes_root``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import operator_cron as op_cron
import operator_diagnostics as op_diag
import operator_fleet as op_fleet
import operator_gateway as op_gateway
import operator_mission_common as mission_common
import operator_policy as op
from operator_mission_common import (
    _LOG_TAIL_BYTES,
    _MAX_APPROVALS,
    _MAX_AUDIT_RECORDS,
    _MAX_DELEGATIONS,
    _MAX_ERRORS,
    _MAX_JOBS,
    _MAX_KANBAN_RUNS,
    _OVERVIEW_LIST_KEEP,
    OVERVIEW_CAP_BYTES,
    SCHEMA_VERSION,
    _audit_call,
    _audited,
    _bounded_json,
    _cache_get,
    _cache_set,
    _mission_denied,
    _mission_envelope,
    _prompt_meta,
    _resolve_root,
    _sanitize_error,
    _surface_allowed,
    _truncate,
)

# Preserve constants and compatibility helpers used by the UI and tests.
MISSION_ALLOWED_SURFACES_ENV = mission_common.MISSION_ALLOWED_SURFACES_ENV
MISSION_SURFACES = mission_common.MISSION_SURFACES
OVERVIEW_HARD_CAP_BYTES = mission_common.OVERVIEW_HARD_CAP_BYTES
SURFACE_CAP_BYTES = mission_common.SURFACE_CAP_BYTES
SURFACE_HARD_CAP_BYTES = mission_common.SURFACE_HARD_CAP_BYTES
SURFACE_TTL = mission_common.SURFACE_TTL
_allowed_surfaces = mission_common._allowed_surfaces
_cache_clear = mission_common._cache_clear
_default_hermes_root = mission_common._default_hermes_root
_mission_error = mission_common._mission_error

# ---------------------------------------------------------------------------
# SQLite read-only helpers
# ---------------------------------------------------------------------------


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite database read-only (``file:...?mode=ro``). Never creates."""
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path.name}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _state_db(profile_home: Path) -> Path:
    return profile_home / "state.db"


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _cron_executions_db(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "executions.db"


def _profile_home(profile: str, hermes_root: Path | None) -> Path:
    return op.resolve_profile_home(profile, hermes_root)


def _iter_profiles(hermes_root: Path | None) -> Iterable[str]:
    return op.list_existing_profiles(hermes_root)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, returning None if missing/unreadable. Never denied paths."""
    if not path.exists() or op.is_denied_path(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_json_list_file(path: Path) -> list[Any]:
    if not path.exists() or op.is_denied_path(path):
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    return []


# ---------------------------------------------------------------------------
# Path helpers for mission sources (bounded, read-only)
# ---------------------------------------------------------------------------


def _logs_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "logs"


def _kanban_boards_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "kanban" / "boards"


def _codex_jobs_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "codex-jobs"


def _fleet_authority_manifest(root: Path) -> Path:
    """Resolve the fleet-authority manifest under ``hermes_root`` (hermetic).

    Mirrors ``operator_fleet._manifest_path`` but roots the lookup at the
    resolved hermes_root rather than the ambient ``HERMES_HOME`` env / home
    dir, so mission reads are hermetic and honor the passed root (design D5).
    """
    return root / "config" / "fleet-authority.json"


def _vault_db_path(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "hermes-vault-data" / "vault.db"


def _vault_access_requests(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "hermes-vault-data" / "access_requests.json"


def _interrupted_turns_path(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "desktop" / "interrupted_turns.json"


def _action_items_path() -> Path:
    return Path.home() / "nexus-wiki" / "ops" / "state" / "action-items.json"


def _errors_log(hermes_root: Path | None) -> Path:
    return _logs_dir(hermes_root) / "errors.log"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"


# ---------------------------------------------------------------------------
# Cheap surfaces
# ---------------------------------------------------------------------------


def hermes_mission_health(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Derived health snapshot (design §6.4 health). Reuses doctor checks."""
    root = _resolve_root(hermes_root)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Reuse the operator doctor's structured checks for the default profile.
    try:
        doctor = json.loads(op_diag.hermes_operator_doctor(profile="default", hermes_root=root))
        if doctor.get("success") is True and isinstance(doctor.get("checks"), dict):
            for name, check in doctor["checks"].items():
                if isinstance(check, dict):
                    checks.append(
                        {
                            "name": name,
                            "status": check.get("status", "UNKNOWN"),
                            "message": _truncate(check.get("message"), 200),
                        }
                    )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"doctor_failed:{exc.__class__.__name__}")

    # Fleet authority state (G2): not an error, a first-class health item.
    try:
        manifest = _fleet_authority_manifest(root)
        authority = "configured" if manifest.exists() else "not_configured"
    except OSError:
        authority = "not_configured"
    checks.append({"name": "fleet_authority", "status": "PASS" if authority == "configured" else "WARN", "message": authority})

    # Vault health (metadata only).
    vault_db = _vault_db_path(root)
    checks.append({"name": "vault_store", "status": "PASS" if vault_db.exists() else "WARN", "message": "present" if vault_db.exists() else "absent"})

    # Codex store presence (G1 conditional).
    codex_dir = _codex_jobs_dir(root)
    checks.append({"name": "codex_store", "status": "PASS" if codex_dir.exists() else "WARN", "message": "present" if codex_dir.exists() else "absent"})

    statuses = {c["status"] for c in checks}
    if "FAIL" in statuses:
        overall = "fail"
    elif "WARN" in statuses:
        overall = "warn"
    else:
        overall = "pass"

    counts = {"checks": len(checks)}
    return _mission_envelope(
        tool="hermes_mission_health",
        surface="health",
        data={"overall": overall, "checks": checks},
        counts=counts,
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_cron(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Cron jobs + executions + scheduler liveness (design §6.4 cron)."""
    root = _resolve_root(hermes_root)
    jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    by_profile: dict[str, int] = {}

    for profile in _iter_profiles(root):
        try:
            home = _profile_home(profile, root)
            profile_jobs = op_cron._read_jobs(home) if hasattr(op_cron, "_read_jobs") else _read_json_list_file(_cron_dir(home) / "jobs.json")
            by_profile[profile] = len(profile_jobs)
            for job in profile_jobs:
                if not isinstance(job, dict):
                    continue
                pm = _prompt_meta(job.get("prompt"))
                jobs.append(
                    {
                        "profile": profile,
                        "job_id": str(job.get("id") or "unknown"),
                        "name": _sanitize_error(str(job.get("name") or "cron job")[:100]),
                        "schedule": str(job.get("schedule_display") or job.get("schedule") or "?"),
                        "enabled": bool(job.get("enabled", True)),
                        "state": str(job.get("state") or ("scheduled" if job.get("enabled", True) else "paused")),
                        "next_run_at": job.get("next_run_at"),
                        "last_run_at": job.get("last_run_at"),
                        "last_status": job.get("last_status"),
                        "last_error": _sanitize_error(job.get("last_error")),
                        "deliver": str(job.get("deliver") or "local"),
                        **pm,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"cron:{profile}:{exc.__class__.__name__}")

    # Execution counts (mode=ro) across profiles.
    exec_by_status: dict[str, int] = {}
    for profile in _iter_profiles(root):
        try:
            db = _cron_executions_db(_profile_home(profile, root))
            conn = _open_ro(db)
            try:
                for row in conn.execute("SELECT status, COUNT(*) AS c FROM executions GROUP BY status"):
                    status = str(row["status"] or "unknown")
                    exec_by_status[status] = exec_by_status.get(status, 0) + int(row["c"])
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError):
            pass

    # Scheduler liveness: any profile ticker_heartbeat within stale window.
    scheduler_live = False
    stale_seconds = 300
    for profile in _iter_profiles(root):
        try:
            hb = _cron_dir(_profile_home(profile, root)) / "ticker_heartbeat"
            if hb.exists() and (time.time() - hb.stat().st_mtime) <= stale_seconds:
                scheduler_live = True
                break
        except OSError:
            continue

    enabled = sum(1 for j in jobs if j.get("enabled", True))
    failed_recent = exec_by_status.get("failed", 0)

    return _mission_envelope(
        tool="hermes_mission_cron",
        surface="cron",
        data={
            "jobs": jobs,
            "executions_by_status": exec_by_status,
            "scheduler_live": scheduler_live,
            "by_profile": by_profile,
        },
        counts={"jobs": len(jobs), "enabled": enabled, "failed_recent": failed_recent, "profiles": len(by_profile)},
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_fleet(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Fleet agents + A2A registry + authority state (design §6.4 fleet)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []

    # Reuse the existing bounded fleet list (never returns URLs or tokens).
    agents: list[dict[str, Any]] = []
    try:
        parsed = json.loads(op_fleet.hermes_fleet_list())
        if parsed.get("success") is True and isinstance(parsed.get("agents"), list):
            for agent in parsed["agents"]:
                if isinstance(agent, dict):
                    agents.append(
                        {
                            "name": str(agent.get("name") or "unknown"),
                            "has_token": bool(agent.get("has_token", False)),
                        }
                    )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"fleet_registry:{exc.__class__.__name__}")

    # Authority manifest (G2): "not_configured" is a state, not an error.
    authority = "not_configured"
    try:
        manifest = _fleet_authority_manifest(root)
        if manifest.exists():
            authority = "configured"
    except OSError:
        pass

    # Gateway served profiles (metadata only).
    served_profiles: list[str] = []
    try:
        gs = _read_json_file(_gateway_state_path(_profile_home("default", root)))
        if gs and isinstance(gs.get("served_profiles"), list):
            served_profiles = [str(p) for p in gs["served_profiles"]]
    except (OSError, ValueError, UnicodeDecodeError):
        pass

    return _mission_envelope(
        tool="hermes_mission_fleet",
        surface="fleet",
        data={
            "peers": agents,
            "authority": authority,
            "served_profiles": served_profiles,
        },
        counts={"peers": len(agents), "served_profiles": len(served_profiles)},
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_audit(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Recent operator audit records (redacted summaries)."""
    records = op.audit_tail(limit=_MAX_AUDIT_RECORDS)
    clean: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        clean.append(
            {
                "timestamp": rec.get("timestamp"),
                "tool": str(rec.get("tool") or ""),
                "level": str(rec.get("level") or ""),
                "apply_mode": str(rec.get("apply_mode") or ""),
                "dry_run": bool(rec.get("dry_run", False)),
                "success": bool(rec.get("success", True)),
                "changed": bool(rec.get("changed", False)),
                "summary": _sanitize_error(_truncate(rec.get("summary"), 300)),
                "error": _sanitize_error(rec.get("error")),
                "profile": rec.get("profile"),
                "prompt_len": rec.get("prompt_len"),
                "prompt_sha256": rec.get("prompt_sha256"),
            }
        )
    return _mission_envelope(
        tool="hermes_mission_audit",
        surface="audit",
        data={"records": clean},
        counts={"records": len(clean)},
        warnings=[],
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Sensitive surfaces
# ---------------------------------------------------------------------------


def _profile_summary(profile: str, root: Path | None, warnings: list[str]) -> dict[str, Any] | None:
    """Per-profile operational summary. Never returns auth/SOUL/memory bodies."""
    try:
        home = _profile_home(profile, root)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"profiles:{profile}:{exc.__class__.__name__}")
        return None

    # Model/provider from config (redacted read).
    model: str | None = None
    provider: str | None = None
    try:
        cfg = op_diag._read_config_safe(home)
        if isinstance(cfg, dict):
            model = cfg.get("model")
            provider = cfg.get("provider")
            model = str(model) if isinstance(model, str) and model else None
            provider = str(provider) if isinstance(provider, str) and provider else None
    except (OSError, RuntimeError, ValueError):
        pass

    # Gateway running (pid probe).
    gateway_running = False
    try:
        pid_path = home / "gateway.pid"
        # Fix 2026-09-08 (see MEMORY.md / HANDOFF.md, hermes-gpt v0.8.0 @ fc1f68c):
        # Hermes versions may store a numeric PID or structured gateway state.
        # Use the shared parser so mission status matches gateway diagnostics.
        pid: int | None = op_gateway._read_gateway_pid_from_pid_file(pid_path)
        if pid is None:
            _state = op_gateway._read_gateway_state(home / "gateway_state.json")
            pid = op_gateway._read_gateway_pid_from_state(_state)
        gateway_running = op_diag._is_process_alive(pid) if pid is not None else False
    except OSError:
        gateway_running = False

    # Cron jobs count.
    cron_jobs = 0
    cron_enabled = 0
    jobs_with_errors = 0
    pj = op_cron._read_jobs(home) if hasattr(op_cron, "_read_jobs") else _read_json_list_file(_cron_dir(home) / "jobs.json")
    cron_jobs = len(pj)
    cron_enabled = sum(1 for j in pj if isinstance(j, dict) and j.get("enabled", True))
    jobs_with_errors = sum(1 for j in pj if isinstance(j, dict) and j.get("last_error"))

    # Sessions 7d + last activity from state.db (mode=ro), count/timestamp only.
    sessions_7d = 0
    last_activity: str | None = None
    try:
        conn = _open_ro(_state_db(home))
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) AS c, MAX(last_activity_at) AS last FROM sessions WHERE last_activity_at >= ?",
                (cutoff,),
            ).fetchone()
            if row:
                sessions_7d = int(row["c"] or 0)
                last_activity = row["last"]
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error, OSError):
        pass

    # Ticker heartbeat liveness.
    ticker_heartbeat: float | None = None
    try:
        hb = _cron_dir(home) / "ticker_heartbeat"
        if hb.exists():
            ticker_heartbeat = hb.stat().st_mtime
    except OSError:
        pass

    return {
        "profile": profile,
        "model": model,
        "provider": provider,
        "gateway_running": gateway_running,
        "cron_jobs": cron_jobs,
        "cron_enabled": cron_enabled,
        "jobs_with_errors": jobs_with_errors,
        "sessions_7d": sessions_7d,
        "last_activity": last_activity,
        "ticker_heartbeat": ticker_heartbeat,
    }


def hermes_mission_profiles(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Profile list + per-profile operational summary (design §6.4 profiles)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    profiles: list[dict[str, Any]] = []
    for profile in _iter_profiles(root):
        summary = _profile_summary(profile, root, warnings)
        if summary is not None:
            profiles.append(summary)

    active = [p["profile"] for p in profiles if p.get("gateway_running")]
    return _mission_envelope(
        tool="hermes_mission_profiles",
        surface="profiles",
        data={"profiles": profiles, "active": active},
        counts={"profiles": len(profiles), "gateway_served": len(active)},
        warnings=warnings,
        trace_id=trace_id,
    )


def _async_delegations_for(home: Path, warnings: list[str], scope: str) -> list[dict[str, Any]]:
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
                        "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?", (_MAX_KANBAN_RUNS,)
                    ).fetchall()
                    for row in rows:
                        out.append(
                            {
                                "task_id": str(row["task_id"] if "task_id" in cols and row["task_id"] is not None else "unknown"),
                                "board": slug,
                                "assignee": row["assignee"] if "assignee" in cols else None,
                                "status": row["status"] if "status" in cols else None,
                                "outcome": row["outcome"] if "outcome" in cols else None,
                                "error": _sanitize_error(row["error"]) if "error" in cols else None,
                                "started_at": row["started_at"] if "started_at" in cols else None,
                                "ended_at": row["ended_at"] if "ended_at" in cols else None,
                            }
                        )
                finally:
                    conn.close()
            except (FileNotFoundError, sqlite3.Error, OSError):
                continue
    except OSError:
        pass
    return out


def hermes_mission_delegations(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Delegated work + kanban runs (G6/G7), never bodies (design §6.4)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    delegations: list[dict[str, Any]] = []

    for profile in _iter_profiles(root):
        try:
            delegations.extend(_async_delegations_for(_profile_home(profile, root), warnings, profile))
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
            "delegations": delegations[: _MAX_DELEGATIONS],
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


def hermes_mission_failures(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
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
            errors.append({"source": f"kanban:{run['board']}", "job_id": run["task_id"], "message": run["error"]})

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


def hermes_mission_approvals(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
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
                        "prompt_sha256": hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest() if secret else "",
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


# ---------------------------------------------------------------------------
# Conditional surfaces
# ---------------------------------------------------------------------------


def hermes_mission_codex(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Codex jobs + native sessions (summaries only), conditional (G1/G8)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    codex_dir = _codex_jobs_dir(root)

    operator_store_present = codex_dir.is_dir()
    operator_jobs: list[dict[str, Any]] = []
    if operator_store_present:
        try:
            for path in sorted(codex_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:_MAX_JOBS]:
                meta = _read_json_file(path)
                if not meta:
                    continue
                operator_jobs.append(
                    {
                        "job_id": str(meta.get("job_id") or ""),
                        "status": str(meta.get("status") or "unknown"),
                        "model": str(meta.get("model") or "") if meta.get("model") else None,
                        "created_at": meta.get("created_at"),
                        "started_at": meta.get("started_at"),
                        "ended_at": meta.get("ended_at"),
                        "return_code": meta.get("return_code"),
                    }
                )
        except OSError:
            pass

    # Native ~/.codex sessions: count + latest dir only (never transcripts).
    native_sessions = 0
    latest_session_dir: str | None = None
    codex_home = Path.home() / ".codex"
    sessions_root = codex_home / "sessions"
    if sessions_root.is_dir():
        try:
            rollout_dirs = sorted(
                (p for p in sessions_root.rglob("rollout-*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            native_sessions = len(rollout_dirs)
            if rollout_dirs:
                # Return only the session date path relative to the codex
                # sessions root (never an absolute home path).
                try:
                    latest_session_dir = str(rollout_dirs[0].parent.relative_to(sessions_root))
                except ValueError:
                    latest_session_dir = None
        except OSError:
            pass

    # Health: codex binary present (read-only probe, no mutation).
    health: dict[str, Any] = {"codex_available": False, "operator_store_present": operator_store_present}
    health["codex_available"] = bool(shutil.which("codex"))

    fallback_source = "operator_store" if operator_store_present else ("codex_cli" if native_sessions else "audit")

    available = operator_store_present or native_sessions > 0
    reason = None if available else "no Codex store or native sessions found on this host (G1)"

    return _mission_envelope(
        tool="hermes_mission_codex",
        surface="codex",
        data={
            "operator_store_present": operator_store_present,
            "operator_jobs": operator_jobs,
            "native_sessions": native_sessions,
            "latest_session_dir": latest_session_dir,
            "health": health,
            "fallback_source": fallback_source,
        },
        counts={"operator_jobs": len(operator_jobs), "native_sessions": native_sessions},
        available=available,
        unavailable_reason=reason,
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_vault(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Vault health + access-request queue, metadata only (design §6.4 vault)."""
    root = _resolve_root(hermes_root)
    vault_db = _vault_db_path(root)
    warnings: list[str] = []

    credential_state: dict[str, int] = {}
    lease_count = 0
    expired_leases = 0
    pending_requests = 0
    access_summary: dict[str, int] = {}

    if vault_db.exists():
        try:
            conn = _open_ro(vault_db)
            try:
                try:
                    for row in conn.execute("SELECT status, COUNT(*) AS c FROM credentials GROUP BY status"):
                        st = str(row["status"] or "unknown")
                        credential_state[st] = int(row["c"])
                except sqlite3.Error:
                    pass
                try:
                    lease_count = int(conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0])
                    expired_leases = int(
                        conn.execute("SELECT COUNT(*) FROM leases WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
                    )
                except sqlite3.Error:
                    pass
                try:
                    pending_requests = int(
                        conn.execute("SELECT COUNT(*) FROM access_requests WHERE status='pending'").fetchone()[0]
                    )
                except sqlite3.Error:
                    pass
                try:
                    for row in conn.execute("SELECT decision, COUNT(*) AS c FROM access_logs GROUP BY decision"):
                        d = str(row["decision"] or "unknown")
                        access_summary[d] = int(row["c"])
                except sqlite3.Error:
                    pass
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError) as exc:
            warnings.append(f"vault:{exc.__class__.__name__}")

    available = vault_db.exists()
    reason = None if available else "Vault store not present on this host"

    # Names only (never payloads/keys).
    credential_names: list[str] = []
    if available:
        try:
            conn = _open_ro(vault_db)
            try:
                try:
                    for row in conn.execute("SELECT service, alias FROM credentials ORDER BY service"):
                        credential_names.append(str(row["service"] or "") + (f"/{row['alias']}" if row["alias"] else ""))
                except sqlite3.Error:
                    pass
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError):
            pass

    return _mission_envelope(
        tool="hermes_mission_vault",
        surface="vault",
        data={
            "credential_state": credential_state,
            "credential_names": credential_names,
            "leases": {"count": lease_count, "expired": expired_leases},
            "pending_requests": pending_requests,
            "access_summary": access_summary,
        },
        counts={"credentials": sum(credential_state.values()), "leases": lease_count, "pending_requests": pending_requests},
        available=available,
        unavailable_reason=reason,
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_usage(hermes_root: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Session usage/cost aggregates (design §6.4 usage). No message content."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []

    sessions_24h = 0
    tokens_in_24h = 0
    tokens_out_24h = 0
    cost_24h = 0.0
    cost_known = 0.0
    by_profile: dict[str, dict[str, Any]] = {}

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    for profile in _iter_profiles(root):
        try:
            conn = _open_ro(_state_db(_profile_home(profile, root)))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")}
                has_cost = "estimated_cost_usd" in cols and "cost_status" in cols

                # Sessions in last 24h.
                s_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM sessions WHERE started_at >= ?", (cutoff_24h,)
                ).fetchone()
                prof_sessions = int(s_row["c"] or 0)
                sessions_24h += prof_sessions

                # Tokens + cost from session_model_usage where a started_at exists.
                if cols:
                    q = "SELECT * FROM session_model_usage"
                    try:
                        rows = conn.execute(q).fetchall()
                    except sqlite3.Error:
                        rows = []
                    prof_in = prof_out = 0
                    prof_cost = 0.0
                    prof_cost_known = 0.0
                    for row in rows:
                        if "input_tokens" in cols and row["input_tokens"] is not None:
                            prof_in += int(row["input_tokens"] or 0)
                        if "output_tokens" in cols and row["output_tokens"] is not None:
                            prof_out += int(row["output_tokens"] or 0)
                        if has_cost:
                            if row["cost_status"] == "known":
                                try:
                                    cost = float(row["estimated_cost_usd"] or 0.0)
                                except (TypeError, ValueError):
                                    cost = 0.0
                                prof_cost_known += cost
                            try:
                                prof_cost += float(row["estimated_cost_usd"] or 0.0)
                            except (TypeError, ValueError):
                                pass
                    tokens_in_24h += prof_in
                    tokens_out_24h += prof_out
                    cost_24h += prof_cost
                    cost_known += prof_cost_known
                    by_profile[profile] = {
                        "sessions_24h": prof_sessions,
                        "tokens_in": prof_in,
                        "tokens_out": prof_out,
                        "estimated_cost_usd": round(prof_cost, 4),
                    }
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError) as exc:
            warnings.append(f"usage:{profile}:{exc.__class__.__name__}")

    return _mission_envelope(
        tool="hermes_mission_usage",
        surface="usage",
        data={
            "sessions_24h": sessions_24h,
            "tokens_24h": {"input": tokens_in_24h, "output": tokens_out_24h},
            "estimated_cost_24h_usd": round(cost_24h, 4),
            "estimated_cost_known_24h_usd": round(cost_known, 4),
            "by_profile": by_profile,
        },
        counts={"sessions_24h": sessions_24h, "profiles": len(by_profile)},
        warnings=warnings,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Overview composition + TTL cache
# ---------------------------------------------------------------------------


def hermes_mission_overview(hermes_root: Path | None = None, force_refresh: bool = False, trace_id: str | None = None) -> str:
    """Composite bounded overview: all surfaces summarized (S1)."""
    if not _surface_allowed("overview"):
        return _bounded_json(_mission_denied(tool="hermes_mission_overview", surface="overview"))

    tid = trace_id or op.new_trace_id()

    cached = None if force_refresh else _cache_get("overview", hermes_root)
    if cached is not None:
        payload, age_ms = cached
        payload = dict(payload)
        payload["served_from_cache"] = True
        payload["age_ms"] = age_ms
        payload["trace_id"] = tid
        _audit_call(tool="hermes_mission_overview", success=True, summary="overview (cache)", extra={"trace_id": tid})
        return _bounded_json(payload, cap_bytes=OVERVIEW_CAP_BYTES, hard_cap_bytes=OVERVIEW_HARD_CAP_BYTES)

    # Build the summary view from each surface (respecting allowlist).
    root = _resolve_root(hermes_root)

    def _section(builder, tool: str, surface: str) -> dict[str, Any]:
        if not _surface_allowed(surface):
            return {"available": False, "unavailable_reason": "surface not allowed"}
        try:
            env = builder(hermes_root=root, trace_id=tid)
            if not env.get("success", True):
                return {"available": False, "unavailable_reason": env.get("unavailable_reason") or env.get("safe_message")}
            # Summary form: keep it small for the overview.
            return {"available": env.get("available", True), "counts": env.get("counts", {}), "data": env.get("data", {})}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "unavailable_reason": op.redact_output(str(exc))[:200]}

    # Cheap surfaces.
    health = _section(hermes_mission_health, "hermes_mission_health", "health")
    cron = _section(hermes_mission_cron, "hermes_mission_cron", "cron")
    fleet = _section(hermes_mission_fleet, "hermes_mission_fleet", "fleet")
    audit = _section(hermes_mission_audit, "hermes_mission_audit", "audit")

    # Sensitive surfaces.
    profiles = _section(hermes_mission_profiles, "hermes_mission_profiles", "profiles")
    delegations = _section(hermes_mission_delegations, "hermes_mission_delegations", "delegations")
    failures = _section(hermes_mission_failures, "hermes_mission_failures", "failures")
    approvals = _section(hermes_mission_approvals, "hermes_mission_approvals", "approvals")

    # Conditional surfaces.
    codex = _section(hermes_mission_codex, "hermes_mission_codex", "codex")
    vault = _section(hermes_mission_vault, "hermes_mission_vault", "vault")
    usage = _section(hermes_mission_usage, "hermes_mission_usage", "usage")

    # Compact per-section summary (trim to small lists for the overview).
    def _compact(section: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"available": section.get("available", True)}
        if not section.get("available", True):
            out["unavailable_reason"] = section.get("unavailable_reason")
            return out
        data = section.get("data", {})
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and len(value) > _OVERVIEW_LIST_KEEP:
                    out[key] = value[:_OVERVIEW_LIST_KEEP]
                    out["truncated"] = True
                    out["count_total"] = len(value)
                else:
                    out[key] = value
        out["counts"] = section.get("counts", {})
        return out

    unavailable = [
        s for s, section in (("health", health), ("profiles", profiles), ("fleet", fleet), ("codex", codex),
                             ("cron", cron), ("delegations", delegations), ("failures", failures),
                             ("approvals", approvals), ("vault", vault), ("usage", usage), ("audit", audit))
        if not section.get("available", True)
    ]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_mission_overview",
        "surface": "overview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fleet_health": {"overall": (health.get("data") or {}).get("overall", "unknown")},
        "profiles": _compact(profiles),
        "fleet_agents": _compact(fleet),
        "codex": _compact(codex),
        "cron": _compact(cron),
        "delegations": _compact(delegations),
        "failures": _compact(failures),
        "pending_approvals": _compact(approvals),
        "vault": _compact(vault),
        "usage": _compact(usage),
        "audit": _compact(audit),
        "surfaces_unavailable": unavailable,
        "trace_id": tid,
    }

    _audit_call(tool="hermes_mission_overview", success=True, summary="overview", extra={"trace_id": tid})
    _cache_set("overview", hermes_root, dict(payload))

    return _bounded_json(payload, cap_bytes=OVERVIEW_CAP_BYTES, hard_cap_bytes=OVERVIEW_HARD_CAP_BYTES)


# ---------------------------------------------------------------------------
# Public tool entrypoints (return JSON str, like the other operator tools)
# ---------------------------------------------------------------------------


def hermes_mission_overview_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return hermes_mission_overview(hermes_root=hermes_root, force_refresh=force_refresh)


def hermes_mission_health_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_health",
        surface="health",
        fn=hermes_mission_health,
        hermes_root=hermes_root,
        summary="health",
        force_refresh=force_refresh,
    )


def hermes_mission_cron_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_cron",
        surface="cron",
        fn=hermes_mission_cron,
        hermes_root=hermes_root,
        summary="cron",
        force_refresh=force_refresh,
    )


def hermes_mission_fleet_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_fleet",
        surface="fleet",
        fn=hermes_mission_fleet,
        hermes_root=hermes_root,
        summary="fleet",
        force_refresh=force_refresh,
    )


def hermes_mission_audit_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_audit",
        surface="audit",
        fn=hermes_mission_audit,
        hermes_root=hermes_root,
        summary="audit",
        force_refresh=force_refresh,
    )


def hermes_mission_profiles_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_profiles",
        surface="profiles",
        fn=hermes_mission_profiles,
        hermes_root=hermes_root,
        summary="profiles",
        force_refresh=force_refresh,
    )


def hermes_mission_delegations_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_delegations",
        surface="delegations",
        fn=hermes_mission_delegations,
        hermes_root=hermes_root,
        summary="delegations",
        force_refresh=force_refresh,
    )


def hermes_mission_failures_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_failures",
        surface="failures",
        fn=hermes_mission_failures,
        hermes_root=hermes_root,
        summary="failures",
        force_refresh=force_refresh,
    )


def hermes_mission_approvals_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_approvals",
        surface="approvals",
        fn=hermes_mission_approvals,
        hermes_root=hermes_root,
        summary="approvals",
        force_refresh=force_refresh,
    )


def hermes_mission_codex_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_codex",
        surface="codex",
        fn=hermes_mission_codex,
        hermes_root=hermes_root,
        summary="codex",
        force_refresh=force_refresh,
    )


def hermes_mission_vault_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_vault",
        surface="vault",
        fn=hermes_mission_vault,
        hermes_root=hermes_root,
        summary="vault",
        force_refresh=force_refresh,
    )


def hermes_mission_usage_tool(hermes_root: Path | None = None, force_refresh: bool = False) -> str:
    return _audited(
        tool="hermes_mission_usage",
        surface="usage",
        fn=hermes_mission_usage,
        hermes_root=hermes_root,
        summary="usage",
        force_refresh=force_refresh,
    )
