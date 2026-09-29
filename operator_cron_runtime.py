"""Policy-gated cron run and job mutation tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_cron_execution import _hermes_argv
from operator_cron_store import (
    _PRESERVED_COPY_FIELDS,
    _RESET_FIELDS,
    _RESET_VALUES,
    _find_job,
    _format_job_safe,
    _is_duplicate,
    _read_jobs,
    _write_jobs,
)


def _build_copy_job(source_job: dict[str, Any], new_id: str) -> dict[str, Any]:
    """Build a new job dict from source, preserving config and resetting state."""
    new_job: dict[str, Any] = {}
    for field in _PRESERVED_COPY_FIELDS:
        if field in source_job:
            new_job[field] = source_job[field]
    # repeat: preserve times, reset completed.
    if "repeat" in new_job and isinstance(new_job["repeat"], dict):
        new_job["repeat"] = {"times": new_job["repeat"].get("times")}
    elif "repeat" in new_job:
        # preserve as-is if shape is unexpected
        pass
    else:
        new_job["repeat"] = {}

    for field in _RESET_FIELDS:
        new_job.pop(field, None)
    # Set specific reset values.
    for key, value in _RESET_VALUES.items():
        new_job[key] = value
    new_job["id"] = new_id
    return new_job


def _new_job_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def hermes_cron_copy(
    source_profile: str,
    target_profile: str,
    job_id: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Copy a cron job from source_profile to target_profile."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_jobs = _read_jobs(source_home)
        target_jobs = _read_jobs(target_home)

        source_job = _find_job(source_jobs, job_id)
        if not source_job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in source profile {source_profile!r}."
            )

        if _is_duplicate(target_jobs, source_job):
            raise ValueError(
                f"Target profile {target_profile!r} already has an active job "
                f"with the same name {source_job.get('name')!r} and schedule "
                f"{source_job.get('schedule_display') or source_job.get('schedule')!r}. "
                "Refusing to create a duplicate."
            )

        new_id = _new_job_id()
        new_job = _build_copy_job(source_job, new_id)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "source_job_id": str(source_job.get("id")),
                "new_target_job_id": new_id,
                "new_job_summary": _format_job_safe(new_job),
            }
            op.audit_record(
                tool="hermes_cron_copy",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
                prompt=str(source_job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        target_jobs.append(new_job)
        _write_jobs(target_home, target_jobs)
        result = {
            "success": True,
            "dry_run": False,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_job_id": str(source_job.get("id")),
            "new_target_job_id": new_id,
            "new_job": _format_job_safe(new_job),
        }
        op.audit_record(
            tool="hermes_cron_copy",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"copied to {target_profile} as {new_id}",
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=str(source_job.get("id")),
            prompt=str(source_job.get("prompt") or ""),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        op.audit_record(
            tool="hermes_cron_copy",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_COPY_ERROR",
                suggested_action="Check source/target profiles, job_id, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_move(
    source_profile: str,
    target_profile: str,
    job_id: str,
    pause_source: bool = True,
    test_run_target: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Move a cron job: copy to target, optionally test-run, pause source.

    Direct mode ordering (no dry-run):
      1. copy source -> target
      2. if copy fails, do NOT pause source
      3. if test_run_target and test run fails, do NOT pause source
      4. pause source only after copy (and optional test run) succeeds
      5. re-list both profiles
    """
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_jobs = _read_jobs(source_home)
        target_jobs = _read_jobs(target_home)

        source_job = _find_job(source_jobs, job_id)
        if not source_job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in source profile {source_profile!r}."
            )
        if _is_duplicate(target_jobs, source_job):
            raise ValueError(
                f"Target profile {target_profile!r} already has an active job "
                f"with the same name and schedule. Refusing to create a duplicate."
            )

        new_id = _new_job_id()
        new_job = _build_copy_job(source_job, new_id)

        plan = {
            "would_move": True,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_job_id": str(source_job.get("id")),
            "new_target_job_id": new_id,
            "pause_source": pause_source,
            "test_run_target": test_run_target,
            "new_job_summary": _format_job_safe(new_job),
        }

        if policy.effective_dry_run(dry_run):
            plan["dry_run"] = True
            op.audit_record(
                tool="hermes_cron_move",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
                prompt=str(source_job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)

        # Step 1: copy (write to target).
        try:
            target_jobs_after = list(target_jobs) + [new_job]
            _write_jobs(target_home, target_jobs_after)
        except Exception as exc:
            op.audit_record(
                tool="hermes_cron_move",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                error=f"copy failed: {exc}",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
            )
            raise

        # Step 2: optional test run.
        test_run_result = None
        if test_run_target:
            run_fn = runner or op.run_argv
            argv = _hermes_argv(target_profile, ["cron", "run", new_id])
            rc, out, err = run_fn(argv, timeout=120, workdir=None)
            test_run_result = {
                "returncode": rc,
                "success": rc == 0,
                "stdout": op.redact_output(out),
                "stderr": op.redact_output(err),
            }
            if rc != 0:
                # Do NOT pause source. Leave the target copy in place; caller
                # can decide whether to remove it.
                op.audit_record(
                    tool="hermes_cron_move",
                    level=policy.level,
                    apply_mode=policy.apply_mode,
                    dry_run=False,
                    success=False,
                    changed=True,
                    summary="test_run_target failed; source NOT paused",
                    source_profile=source_profile,
                    target_profile=target_profile,
                    job_id=str(source_job.get("id")),
                    error=test_run_result["stderr"],
                )
                return json.dumps(
                    {
                        "success": False,
                        "dry_run": False,
                        "error": "test_run_target failed; source was NOT paused.",
                        "copy_result": {
                            "new_target_job_id": new_id,
                            "new_job": _format_job_safe(new_job),
                        },
                        "test_run_result": test_run_result,
                    },
                    indent=2,
                )

        # Step 3: pause source.
        pause_result = None
        if pause_source:
            run_fn = runner or op.run_argv
            argv = _hermes_argv(source_profile, ["cron", "pause", str(source_job.get("id"))])
            rc, out, err = run_fn(argv, timeout=60, workdir=None)
            pause_result = {
                "returncode": rc,
                "success": rc == 0,
                "stdout": op.redact_output(out),
                "stderr": op.redact_output(err),
            }
            if rc != 0:
                source_after = _read_jobs(source_home)
                target_after = _read_jobs(target_home)
                op.audit_record(
                    tool="hermes_cron_move",
                    level=policy.level,
                    apply_mode=policy.apply_mode,
                    dry_run=False,
                    success=False,
                    changed=True,
                    summary=f"copy succeeded; pause failed for {new_id}",
                    source_profile=source_profile,
                    target_profile=target_profile,
                    job_id=str(source_job.get("id")),
                    error=pause_result["stderr"],
                )
                return json.dumps(
                    {
                        "success": False,
                        "partial": True,
                        "dry_run": False,
                        "source_profile": source_profile,
                        "target_profile": target_profile,
                        "new_target_job_id": new_id,
                        "new_job": _format_job_safe(new_job),
                        "pause_result": pause_result,
                        "test_run_result": test_run_result,
                        "source_after_count": len(source_after),
                        "target_after_count": len(target_after),
                    },
                    indent=2,
                )

        # Step 4: re-list both.
        source_after = _read_jobs(source_home)
        target_after = _read_jobs(target_home)

        op.audit_record(
            tool="hermes_cron_move",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"moved to {target_profile} as {new_id}; paused_source={pause_source}",
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=str(source_job.get("id")),
            prompt=str(source_job.get("prompt") or ""),
        )

        return json.dumps(
            {
                "success": True,
                "dry_run": False,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "new_target_job_id": new_id,
                "new_job": _format_job_safe(new_job),
                "pause_result": pause_result,
                "test_run_result": test_run_result,
                "source_after_count": len(source_after),
                "target_after_count": len(target_after),
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        op.audit_record(
            tool="hermes_cron_move",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_MOVE_ERROR",
                suggested_action="Check source/target profiles, job_id, disk permissions, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_create(
    profile: str = "default",
    schedule: str = "",
    prompt: str = "",
    name: str | None = None,
    skills: list[str] | None = None,
    deliver: str | None = None,
    repeat: int | None = None,
    script: str | None = None,
    workdir: str | None = None,
    no_agent: bool | None = None,
    context_from: list[str] | None = None,
    enabled_toolsets: list[str] | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Create a new cron job. Requires level >= cron."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)

        if not schedule:
            raise ValueError("schedule is required for creating a cron job.")
        if not prompt and not skills and not script:
            raise ValueError(
                "At least one of prompt, skills, or script is required."
            )

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)

        new_id = _new_job_id()
        job_name = (
            name
            or (prompt[:50] if prompt else "")
            or (skills[0] if skills else "")
            or "cron job"
        )

        new_job: dict[str, Any] = {
            "id": new_id,
            "name": job_name,
            "prompt": prompt,
            "schedule": schedule,
            "schedule_display": schedule,
            "skills": skills or [],
            "deliver": deliver or "local",
            "enabled": True,
            "state": "scheduled",
        }
        if repeat is not None:
            new_job["repeat"] = {"times": repeat}
        if script:
            new_job["script"] = script
        if workdir:
            new_job["workdir"] = workdir
        if no_agent is not None:
            new_job["no_agent"] = no_agent
        if context_from:
            new_job["context_from"] = list(context_from)
        if enabled_toolsets:
            new_job["enabled_toolsets"] = list(enabled_toolsets)
        # Hermes Agent scheduler contract: job["model"] is the model name string
        # and job["provider"] carries the provider (no nested model dict).
        if model_name:
            new_job["model"] = model_name
        if model_provider:
            new_job["provider"] = model_provider

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_create": True,
                "profile": profile,
                "job_id": new_id,
                "name": job_name,
                "schedule": schedule,
                "prompt_len": len(prompt),
                "skills": skills or [],
                "deliver": deliver or "local",
            }
            op.audit_record(
                tool="hermes_cron_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=new_id,
                prompt=prompt,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        jobs.append(new_job)
        _write_jobs(profile_home, jobs)

        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "job_id": new_id,
            "job": _format_job_safe(new_job),
        }
        op.audit_record(
            tool="hermes_cron_create",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"created {new_id} in {profile}",
            profile=profile,
            job_id=new_id,
            prompt=prompt,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep tool failures inside the JSON envelope.
        op.audit_record(
            tool="hermes_cron_create",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_CREATE_ERROR",
                suggested_action="Check profile, operator level/apply mode, and parameters.",
            ),
            indent=2,
        )
