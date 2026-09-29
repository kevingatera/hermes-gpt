"""Run or pause one Hermes cron job through fixed CLI arguments."""

from __future__ import annotations

import json
from pathlib import Path

import operator_policy as op
from operator_cron_store import _find_job, _format_job_safe, _read_jobs


def _hermes_argv(profile: str, sub: list[str]) -> list[str]:
    """Build a fixed-argv Hermes CLI invocation for the given profile."""
    if profile == "default":
        return ["hermes", *sub]
    return ["hermes", "-p", profile, *sub]


def hermes_cron_run(
    profile: str = "default",
    job_id: str = "",
    dry_run: bool = True,
    timeout: int = 1800,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Run a cron job immediately. Requires level >= cron.

    ``timeout`` is the maximum time Hermes GPT will wait for the underlying
    synchronous ``hermes cron run`` process. Real agent cron jobs commonly run
    longer than two minutes, so the operator default is 30 minutes rather than
    the generic 120-second subprocess budget.
    """
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")
        try:
            timeout = int(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be an integer number of seconds.") from exc
        if timeout < 30 or timeout > 7200:
            raise ValueError("timeout must be between 30 and 7200 seconds.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        job = _find_job(jobs, job_id)
        if not job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in profile {profile!r}."
            )

        argv = _hermes_argv(profile, ["cron", "run", str(job.get("id"))])

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
                "job_id": str(job.get("id")),
                "job_name": str(job.get("name")),
                "timeout": timeout,
            }
            op.audit_record(
                tool="hermes_cron_run",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=str(job.get("id")),
                prompt=str(job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        if runner is None:
            rc, out, err = op.run_argv(
                argv,
                timeout=timeout,
                workdir=None,
                timeout_cap=7200,
            )
        else:
            rc, out, err = runner(argv, timeout=timeout, workdir=None)
        redacted_out = op.redact_output(out)
        redacted_err = op.redact_output(err)

        # Refresh job state.
        refreshed = _find_job(_read_jobs(profile_home), str(job.get("id")))
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": redacted_out,
            "stderr": redacted_err,
            "job": _format_job_safe(refreshed or job),
        }
        op.audit_record(
            tool="hermes_cron_run",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc}",
            profile=profile,
            job_id=str(job.get("id")),
            prompt=str(job.get("prompt") or ""),
            error=redacted_err if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        op.audit_record(
            tool="hermes_cron_run",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_RUN_ERROR",
                suggested_action="Check job_id, profile, Hermes CLI availability, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_pause(
    profile: str = "default",
    job_id: str = "",
    reason: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Pause a cron job. Requires level >= cron."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        job = _find_job(jobs, job_id)
        if not job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in profile {profile!r}."
            )

        argv = _hermes_argv(profile, ["cron", "pause", str(job.get("id"))])

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_pause": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
                "job_id": str(job.get("id")),
                "job_name": str(job.get("name")),
                "reason": (reason or "")[:200],
            }
            op.audit_record(
                tool="hermes_cron_pause",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=str(job.get("id")),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=60, workdir=None)
        redacted_out = op.redact_output(out)
        redacted_err = op.redact_output(err)
        refreshed = _find_job(_read_jobs(profile_home), str(job.get("id")))
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": redacted_out,
            "stderr": redacted_err,
            "job": _format_job_safe(refreshed or job),
        }
        op.audit_record(
            tool="hermes_cron_pause",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc} reason={(reason or '')[:80]}",
            profile=profile,
            job_id=str(job.get("id")),
            error=redacted_err if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        op.audit_record(
            tool="hermes_cron_pause",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_PAUSE_ERROR",
                suggested_action="Check job_id, profile, Hermes CLI availability, and operator level/apply mode.",
            ),
            indent=2,
        )
