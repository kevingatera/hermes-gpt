"""Dry-run-first Operator recovery workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import operator_gateway as op_gateway
import operator_policy as op
from operator_diagnostics_checks import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNSUPPORTED,
    STATUS_WARN,
    _check_gateway_status,
    _config_path,
    _count_skills_safe,
    _env_path,
    _profile_home,
    _read_config_safe,
    _read_cron_jobs_safe,
    _read_env_keys_safe,
)


def _recover_step_result(
    step: str,
    status: str,
    message: str,
    suggested_action: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "step": step,
        "status": status,
        "message": message,
        "suggested_action": suggested_action,
    }
    if extra:
        result.update(extra)
    return result


def hermes_operator_recover(
    profile: str = "default",
    apply: bool = False,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Conservative recovery sequence. Dry-run by default."""
    trace_id = op.new_trace_id()
    steps: list[dict[str, Any]] = []
    mutations_attempted = 0
    mutations_performed = 0
    warnings: list[str] = []
    failed_steps: list[str] = []

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
        policy = op.OperatorPolicy()

        # Pre-flight: if apply=True, require policy allows mutation.
        can_mutate = (
            policy.enabled
            and policy.apply_mode == "direct"
            and op.has_level("workspace", policy.level)
        )
        if apply and not can_mutate:
            result = op.make_error_envelope(
                layer="policy",
                code="PERMISSION_DENIED",
                safe_message="apply=true requires operator enabled, apply_mode=direct, and level>=workspace.",
                suggested_action="Set HERMES_GPT_OPERATOR_ENABLED=1, HERMES_GPT_OPERATOR_LEVEL=workspace (or higher), and HERMES_GPT_OPERATOR_APPLY_MODE=direct.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        # 1. read_config
        config_path = _config_path(profile_home)
        if not config_path.exists():
            steps.append(
                _recover_step_result(
                    "read_config",
                    STATUS_WARN,
                    "config.yaml does not exist for this profile.",
                    "Create config.yaml if this profile needs custom configuration.",
                )
            )
        else:
            try:
                cfg = _read_config_safe(profile_home)
                steps.append(
                    _recover_step_result(
                        "read_config",
                        STATUS_PASS,
                        "config.yaml is readable and parses.",
                        "No action needed.",
                        extra={"top_level_keys": sorted(cfg.keys())[:20]},
                    )
                )
            except Exception as exc:
                failed_steps.append("read_config")
                steps.append(
                    _recover_step_result(
                        "read_config",
                        STATUS_FAIL,
                        f"config.yaml could not be read: {exc.__class__.__name__}",
                        "Check config.yaml syntax and permissions.",
                    )
                )

        # 2. validate_env
        try:
            env_path = _env_path(profile_home)
            if env_path.exists():
                keys, secret_like = _read_env_keys_safe(env_path)
                steps.append(
                    _recover_step_result(
                        "validate_env",
                        STATUS_PASS,
                        f".env readable; {len(keys)} key(s), {len(secret_like)} secret-like.",
                        "No action needed.",
                        extra={
                            "key_count": len(keys),
                            "secret_like_count": len(secret_like),
                        },
                    )
                )
            else:
                steps.append(
                    _recover_step_result(
                        "validate_env",
                        STATUS_WARN,
                        "No .env file exists for this profile.",
                        "Create one only if this profile needs env overrides.",
                    )
                )
        except Exception as exc:
            failed_steps.append("validate_env")
            steps.append(
                _recover_step_result(
                    "validate_env",
                    STATUS_FAIL,
                    f".env could not be read: {exc.__class__.__name__}",
                    "Check .env permissions.",
                )
            )

        # 3. restart_gateway_if_needed
        gateway_check = _check_gateway_status(profile_home)
        if gateway_check["status"] in (STATUS_FAIL, STATUS_WARN):
            mutations_attempted += 1
            if apply and can_mutate:
                try:
                    rc, out, err = op_gateway._hermes_gateway_restart_raw(
                        profile, runner=runner
                    )
                    if rc == 0:
                        mutations_performed += 1
                        steps.append(
                            _recover_step_result(
                                "restart_gateway_if_needed",
                                STATUS_PASS,
                                "Gateway restart command executed successfully.",
                                "Verify gateway status with hermes_operator_doctor.",
                                extra={"returncode": rc},
                            )
                        )
                    else:
                        failed_steps.append("restart_gateway_if_needed")
                        steps.append(
                            _recover_step_result(
                                "restart_gateway_if_needed",
                                STATUS_FAIL,
                                f"Gateway restart command returned non-zero exit code {rc}.",
                                "Check Hermes logs and restart manually.",
                                extra={
                                    "returncode": rc,
                                    "stderr": op.redact_output(err)[:500],
                                },
                            )
                        )
                except Exception as exc:
                    failed_steps.append("restart_gateway_if_needed")
                    steps.append(
                        _recover_step_result(
                            "restart_gateway_if_needed",
                            STATUS_FAIL,
                            f"Gateway restart failed: {exc.__class__.__name__}",
                            "Check Hermes installation and PATH.",
                        )
                    )
            else:
                steps.append(
                    _recover_step_result(
                        "restart_gateway_if_needed",
                        STATUS_WARN if not apply else STATUS_FAIL,
                        "Gateway needs restart but apply=false or mutation not allowed.",
                        "Run hermes_operator_recover with apply=true after enabling direct operator mode.",
                        extra={
                            "would_restart": True,
                            "apply": apply,
                            "mutation_allowed": can_mutate,
                        },
                    )
                )
        else:
            steps.append(
                _recover_step_result(
                    "restart_gateway_if_needed",
                    STATUS_PASS,
                    "Gateway status is healthy; no restart needed.",
                    "No action needed.",
                )
            )

        # 4. connector_routes
        steps.append(
            _recover_step_result(
                "connector_routes",
                STATUS_UNSUPPORTED,
                "No supported connector re-registration command or API was found.",
                "If the connector is stale, recreate it in your MCP client or restart the gateway and reconnect manually.",
                extra={"supported": False, "action": "manual"},
            )
        )

        # 5. recheck_cron
        try:
            jobs = _read_cron_jobs_safe(profile_home)
            steps.append(
                _recover_step_result(
                    "recheck_cron",
                    STATUS_PASS,
                    f"Cron registry readable; {len(jobs)} job(s).",
                    "No action needed.",
                    extra={"jobs_count": len(jobs)},
                )
            )
        except Exception as exc:
            failed_steps.append("recheck_cron")
            steps.append(
                _recover_step_result(
                    "recheck_cron",
                    STATUS_FAIL,
                    f"Cron registry could not be read: {exc.__class__.__name__}",
                    "Inspect cron/jobs.json syntax and permissions.",
                )
            )

        # 6. recheck_skill_index
        try:
            count = _count_skills_safe(profile_home)
            steps.append(
                _recover_step_result(
                    "recheck_skill_index",
                    STATUS_PASS,
                    f"Skills directory readable; {count} SKILL.md file(s).",
                    "No action needed.",
                    extra={"count": count},
                )
            )
        except Exception as exc:
            failed_steps.append("recheck_skill_index")
            steps.append(
                _recover_step_result(
                    "recheck_skill_index",
                    STATUS_FAIL,
                    f"Skills directory could not be enumerated: {exc.__class__.__name__}",
                    "Check skills/ directory permissions.",
                )
            )

        # 7. write_audit_record
        try:
            record = op.audit_record(
                tool="hermes_operator_recover",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=not apply,
                success=len(failed_steps) == 0,
                changed=apply and mutations_performed > 0,
                summary=f"recover apply={apply} mutations={mutations_performed} failed={len(failed_steps)}",
                profile=profile,
                extra={"trace_id": trace_id, "failed_steps": failed_steps},
            )
            steps.append(
                _recover_step_result(
                    "write_audit_record",
                    STATUS_PASS,
                    "Audit record written."
                    if not apply
                    else "Audit record written after recovery attempts.",
                    "No action needed.",
                    extra={
                        "audit_log_path": str(op.audit_log_path()),
                        "record_timestamp": record.get("timestamp"),
                    },
                )
            )
        except Exception as exc:
            failed_steps.append("write_audit_record")
            steps.append(
                _recover_step_result(
                    "write_audit_record",
                    STATUS_WARN,
                    f"Audit record could not be written: {exc.__class__.__name__}",
                    "Check audit log path and permissions.",
                )
            )

        overall_ok = len(failed_steps) == 0
        if failed_steps:
            recommended = "Review failed steps and run hermes_operator_doctor."
        elif apply:
            recommended = "Recovery applied; run hermes_operator_doctor to verify."
        else:
            recommended = (
                "Dry-run complete; review steps and run with apply=true if appropriate."
            )

        return json.dumps(
            {
                "success": True,
                "ok": overall_ok,
                "apply": apply,
                "profile": profile,
                "steps": steps,
                "mutations_attempted": mutations_attempted,
                "mutations_performed": mutations_performed,
                "warnings": warnings,
                "failed_steps": failed_steps,
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
            code="RECOVER_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_recover again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)
