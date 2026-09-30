"""Cron read tools with compatibility exports for the former combined module."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_gpt.policy import authorization as op

# Keep the former module-level helper and tool names for callers.
from hermes_gpt.workspace.cron_execution import _hermes_argv, hermes_cron_pause, hermes_cron_run
from hermes_gpt.workspace.cron_runtime import _build_copy_job, _new_job_id, hermes_cron_copy, hermes_cron_create, hermes_cron_move
from hermes_gpt.workspace.cron_store import _PRESERVED_COPY_FIELDS, _RESET_FIELDS, _RESET_VALUES, _cron_dir, _find_job, _format_job_safe, _hash_prompt, _is_duplicate, _jobs_file, _jobs_shape_cache, _jobs_shape_key, _read_jobs, _write_jobs


def hermes_cron_list(
    profile: str = "default",
    include_disabled: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """List cron jobs for a profile. Read-only."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        if not include_disabled:
            jobs = [j for j in jobs if j.get("enabled", True)]
        formatted = [_format_job_safe(j) for j in jobs]
        result = {
            "success": True,
            "profile": profile,
            "count": len(formatted),
            "jobs": formatted,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_LIST_ERROR",
                suggested_action="Check cron/jobs.json and profile name.",
            ),
            indent=2,
        )


def hermes_cron_status(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Aggregate cron status for a profile. Read-only."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        enabled = [j for j in jobs if j.get("enabled", True)]
        disabled = [j for j in jobs if not j.get("enabled", True)]
        with_errors = [j for j in jobs if j.get("last_error")]
        with_delivery_errors = [j for j in jobs if j.get("last_delivery_error")]

        # Gateway / ticker state is best-effort: check ticker_heartbeat file.
        cron_dir = _cron_dir(profile_home)
        ticker_heartbeat = None
        hb_path = cron_dir / "ticker_heartbeat"
        if hb_path.exists():
            try:
                ticker_heartbeat = hb_path.stat().st_mtime
            except OSError:
                ticker_heartbeat = None

        result = {
            "success": True,
            "profile": profile,
            "jobs_count": len(jobs),
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
            "jobs_with_errors": len(with_errors),
            "jobs_with_delivery_errors": len(with_delivery_errors),
            "ticker_heartbeat_mtime": ticker_heartbeat,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_STATUS_ERROR",
                suggested_action="Check cron/jobs.json and profile name.",
            ),
            indent=2,
        )
