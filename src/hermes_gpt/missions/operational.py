"""Health, cron, fleet, audit, and profile Mission Control surfaces."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.workspace import cron as op_cron
from hermes_gpt.workspace import diagnostics as op_diag
from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.workspace import gateway as op_gateway
from hermes_gpt.policy import authorization as op
from hermes_gpt.missions.common import _MAX_AUDIT_RECORDS, _mission_envelope, _prompt_meta, _resolve_root, _sanitize_error, _truncate
from hermes_gpt.missions.sources import _codex_jobs_dir, _cron_dir, _cron_executions_db, _fleet_authority_manifest, _gateway_state_path, _iter_profiles, _open_ro, _profile_home, _read_json_file, _read_json_list_file, _state_db, _vault_db_path


def hermes_mission_health(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Derived health snapshot (design §6.4 health). Reuses doctor checks."""
    root = _resolve_root(hermes_root)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Reuse the operator doctor's structured checks for the default profile.
    try:
        doctor = json.loads(
            op_diag.hermes_operator_doctor(profile="default", hermes_root=root)
        )
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
    checks.append(
        {
            "name": "fleet_authority",
            "status": "PASS" if authority == "configured" else "WARN",
            "message": authority,
        }
    )

    # Vault health (metadata only).
    vault_db = _vault_db_path(root)
    checks.append(
        {
            "name": "vault_store",
            "status": "PASS" if vault_db.exists() else "WARN",
            "message": "present" if vault_db.exists() else "absent",
        }
    )

    # Codex store presence (G1 conditional).
    codex_dir = _codex_jobs_dir(root)
    checks.append(
        {
            "name": "codex_store",
            "status": "PASS" if codex_dir.exists() else "WARN",
            "message": "present" if codex_dir.exists() else "absent",
        }
    )

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


def hermes_mission_cron(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Cron jobs + executions + scheduler liveness (design §6.4 cron)."""
    root = _resolve_root(hermes_root)
    jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    by_profile: dict[str, int] = {}

    for profile in _iter_profiles(root):
        try:
            home = _profile_home(profile, root)
            profile_jobs = (
                op_cron._read_jobs(home)
                if hasattr(op_cron, "_read_jobs")
                else _read_json_list_file(_cron_dir(home) / "jobs.json")
            )
            by_profile[profile] = len(profile_jobs)
            for job in profile_jobs:
                if not isinstance(job, dict):
                    continue
                pm = _prompt_meta(job.get("prompt"))
                jobs.append(
                    {
                        "profile": profile,
                        "job_id": str(job.get("id") or "unknown"),
                        "name": _sanitize_error(
                            str(job.get("name") or "cron job")[:100]
                        ),
                        "schedule": str(
                            job.get("schedule_display") or job.get("schedule") or "?"
                        ),
                        "enabled": bool(job.get("enabled", True)),
                        "state": str(
                            job.get("state")
                            or ("scheduled" if job.get("enabled", True) else "paused")
                        ),
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
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS c FROM executions GROUP BY status"
                ):
                    status = str(row["status"] or "unknown")
                    exec_by_status[status] = exec_by_status.get(status, 0) + int(
                        row["c"]
                    )
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
        counts={
            "jobs": len(jobs),
            "enabled": enabled,
            "failed_recent": failed_recent,
            "profiles": len(by_profile),
        },
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_fleet(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
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


def hermes_mission_audit(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
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


def _profile_summary(
    profile: str, root: Path | None, warnings: list[str]
) -> dict[str, Any] | None:
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
    pj = (
        op_cron._read_jobs(home)
        if hasattr(op_cron, "_read_jobs")
        else _read_json_list_file(_cron_dir(home) / "jobs.json")
    )
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


def hermes_mission_profiles(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
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
