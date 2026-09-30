"""Operator health reports assembled from the diagnostic probes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_gpt import paths
from hermes_gpt.policy import authorization as op
from hermes_gpt.workspace.diagnostics_checks import STATUS_FAIL, STATUS_PASS, STATUS_UNSUPPORTED, STATUS_WARN, _check_config_readable, _check_connector_api_bridge, _check_cron_registry, _check_env_readable, _check_gateway_status, _check_last_audit_record, _check_operator_policy, _check_operator_runtime, _check_skills_registry, _count_skills_safe, _env_path, _gateway_pid_path, _is_process_alive, _profile_home, _read_cron_jobs_safe, _read_env_keys_safe, _read_last_audit_record, _ticker_heartbeat_path
from hermes_gpt.workspace.diagnostics_repo import _repo_status
from hermes_gpt.versioning import VERSION


def hermes_operator_doctor(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Run a read-only health check across operator surfaces."""
    trace_id = op.new_trace_id()
    try:
        try:
            op.validate_profile_name(profile)
        except ValueError as exc:
            result = op.make_error_envelope(
                layer="policy",
                code="INVALID_PROFILE",
                safe_message=str(exc),
                suggested_action="Provide a valid Hermes profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        if not op.profile_exists(profile, hermes_root):
            result = op.make_error_envelope(
                layer="policy",
                code="PROFILE_NOT_FOUND",
                safe_message=f"Profile {profile!r} does not exist.",
                suggested_action="Verify HERMES_HOME and the profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        profile_home = _profile_home(profile, hermes_root)

        checks: dict[str, Any] = {
            "operator_runtime": _check_operator_runtime(),
            "gateway_status": _check_gateway_status(profile_home),
            "config_readable": _check_config_readable(profile_home),
            "env_readable": _check_env_readable(profile_home),
            "cron_registry": _check_cron_registry(profile_home),
            "skills_registry": _check_skills_registry(profile_home),
            "operator_policy": _check_operator_policy(profile, hermes_root),
            "last_audit_record": _check_last_audit_record(),
            "connector_api_bridge": _check_connector_api_bridge(profile_home),
        }

        failed = [name for name, c in checks.items() if c["status"] == STATUS_FAIL]
        warnings = [name for name, c in checks.items() if c["status"] == STATUS_WARN]
        unsupported = [
            name for name, c in checks.items() if c["status"] == STATUS_UNSUPPORTED
        ]

        if failed:
            overall = STATUS_FAIL
            recommended = "Run hermes_operator_recover with apply=false to preview recovery steps."
        elif warnings:
            overall = STATUS_WARN
            recommended = "Review warnings, then run hermes_operator_recover if needed."
        elif unsupported:
            overall = STATUS_WARN
            recommended = "Unsupported checks are not failures; review connector notes."
        else:
            overall = STATUS_PASS
            recommended = "No action needed."

        return json.dumps(
            {
                "success": True,
                "ok": overall == STATUS_PASS,
                "profile": profile,
                "overall_status": overall,
                "checks": checks,
                "failed_checks": failed,
                "warnings": warnings,
                "unsupported": unsupported,
                "recommended_action": recommended,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="operator",
            code="DOCTOR_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_doctor again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)


def hermes_operator_snapshot(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Return a single current-state summary of the operator and its surfaces."""
    trace_id = op.new_trace_id()
    try:
        try:
            op.validate_profile_name(profile)
        except ValueError as exc:
            result = op.make_error_envelope(
                layer="policy",
                code="INVALID_PROFILE",
                safe_message=str(exc),
                suggested_action="Provide a valid Hermes profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        profile_exists = op.profile_exists(profile, hermes_root)
        profile_home = _profile_home(profile, hermes_root) if profile_exists else None
        known_issues: list[str] = []

        # Gateway
        gateway: dict[str, Any] = {"running": False}
        if profile_home is not None:
            try:
                pid_path = _gateway_pid_path(profile_home)
                pid = None
                if pid_path.exists():
                    try:
                        pid = int(pid_path.read_text(encoding="utf-8").strip())
                    except (OSError, ValueError):
                        pid = None
                gateway["pid"] = pid
                gateway["running"] = (
                    _is_process_alive(pid) if pid is not None else False
                )
                hb_path = _ticker_heartbeat_path(profile_home)
                if hb_path.exists():
                    try:
                        gateway["ticker_heartbeat_mtime"] = hb_path.stat().st_mtime
                    except OSError:
                        pass
            except Exception as exc:
                known_issues.append(f"gateway_probe_failed:{exc.__class__.__name__}")

        # Cron
        cron: dict[str, Any] = {
            "jobs_count": 0,
            "enabled_count": 0,
            "jobs_with_errors": 0,
        }
        if profile_home is not None:
            try:
                jobs = _read_cron_jobs_safe(profile_home)
                cron = {
                    "jobs_count": len(jobs),
                    "enabled_count": sum(1 for j in jobs if j.get("enabled", True)),
                    "jobs_with_errors": sum(1 for j in jobs if j.get("last_error")),
                }
            except Exception as exc:
                known_issues.append(f"cron_probe_failed:{exc.__class__.__name__}")

        # Env
        env: dict[str, Any] = {
            "env_exists": False,
            "key_count": 0,
            "secret_like_count": 0,
        }
        if profile_home is not None:
            try:
                env_path = _env_path(profile_home)
                if env_path.exists():
                    keys, secret_like = _read_env_keys_safe(env_path)
                    env = {
                        "env_exists": True,
                        "key_count": len(keys),
                        "secret_like_count": len(secret_like),
                    }
            except Exception as exc:
                known_issues.append(f"env_probe_failed:{exc.__class__.__name__}")

        # Skills
        skills: dict[str, Any] = {"count": 0}
        if profile_home is not None:
            try:
                skills["count"] = _count_skills_safe(profile_home)
            except Exception as exc:
                known_issues.append(f"skills_probe_failed:{exc.__class__.__name__}")

        # Last audit timestamp
        last_audit_timestamp: str | None = None
        try:
            record = _read_last_audit_record()
            if record:
                last_audit_timestamp = record.get("timestamp")
        except Exception:
            known_issues.append("audit_probe_failed")

        # Repo status
        repo_status = _repo_status(paths.project_root())

        recommended = (
            "Run hermes_operator_doctor for details."
            if known_issues
            else "No action needed."
        )

        return json.dumps(
            {
                "success": True,
                "version": VERSION,
                "profile": profile,
                "profile_exists": profile_exists,
                "gateway": gateway,
                "cron": cron,
                "env": env,
                "skills": skills,
                "last_audit_timestamp": last_audit_timestamp,
                "repo_status": repo_status,
                "known_issues": known_issues,
                "recommended_next_action": recommended,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="operator",
            code="SNAPSHOT_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_snapshot again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)
