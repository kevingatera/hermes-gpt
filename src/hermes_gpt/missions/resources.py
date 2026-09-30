"""Codex, Vault, and usage Mission Control surfaces."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.missions.common import _MAX_JOBS, _mission_envelope, _resolve_root
from hermes_gpt.missions.sources import _codex_jobs_dir, _iter_profiles, _open_ro, _profile_home, _read_json_file, _state_db, _vault_db_path


def hermes_mission_codex(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """Codex jobs + native sessions (summaries only), conditional (G1/G8)."""
    root = _resolve_root(hermes_root)
    warnings: list[str] = []
    codex_dir = _codex_jobs_dir(root)

    operator_store_present = codex_dir.is_dir()
    operator_jobs: list[dict[str, Any]] = []
    if operator_store_present:
        try:
            for path in sorted(
                codex_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:_MAX_JOBS]:
                meta = _read_json_file(path)
                if not meta:
                    continue
                operator_jobs.append(
                    {
                        "job_id": str(meta.get("job_id") or ""),
                        "status": str(meta.get("status") or "unknown"),
                        "model": str(meta.get("model") or "")
                        if meta.get("model")
                        else None,
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
                    latest_session_dir = str(
                        rollout_dirs[0].parent.relative_to(sessions_root)
                    )
                except ValueError:
                    latest_session_dir = None
        except OSError:
            pass

    # Health: codex binary present (read-only probe, no mutation).
    health: dict[str, Any] = {
        "codex_available": False,
        "operator_store_present": operator_store_present,
    }
    health["codex_available"] = bool(shutil.which("codex"))

    fallback_source = (
        "operator_store"
        if operator_store_present
        else ("codex_cli" if native_sessions else "audit")
    )

    available = operator_store_present or native_sessions > 0
    reason = (
        None
        if available
        else "no Codex store or native sessions found on this host (G1)"
    )

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
        counts={
            "operator_jobs": len(operator_jobs),
            "native_sessions": native_sessions,
        },
        available=available,
        unavailable_reason=reason,
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_vault(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
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
                    for row in conn.execute(
                        "SELECT status, COUNT(*) AS c FROM credentials GROUP BY status"
                    ):
                        st = str(row["status"] or "unknown")
                        credential_state[st] = int(row["c"])
                except sqlite3.Error:
                    pass
                try:
                    lease_count = int(
                        conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
                    )
                    expired_leases = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM leases WHERE expires_at < ?",
                            (datetime.now(timezone.utc).isoformat(),),
                        ).fetchone()[0]
                    )
                except sqlite3.Error:
                    pass
                try:
                    pending_requests = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM access_requests WHERE status='pending'"
                        ).fetchone()[0]
                    )
                except sqlite3.Error:
                    pass
                try:
                    for row in conn.execute(
                        "SELECT decision, COUNT(*) AS c FROM access_logs GROUP BY decision"
                    ):
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
                    for row in conn.execute(
                        "SELECT service, alias FROM credentials ORDER BY service"
                    ):
                        credential_names.append(
                            str(row["service"] or "")
                            + (f"/{row['alias']}" if row["alias"] else "")
                        )
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
        counts={
            "credentials": sum(credential_state.values()),
            "leases": lease_count,
            "pending_requests": pending_requests,
        },
        available=available,
        unavailable_reason=reason,
        warnings=warnings,
        trace_id=trace_id,
    )


def hermes_mission_usage(
    hermes_root: Path | None = None, trace_id: str | None = None
) -> dict[str, Any]:
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
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")
                }
                has_cost = "estimated_cost_usd" in cols and "cost_status" in cols

                # Sessions in last 24h.
                s_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM sessions WHERE started_at >= ?",
                    (cutoff_24h,),
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
