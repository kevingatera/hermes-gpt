"""Low-level read-only probes used by Operator diagnostics."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import operator_config as op_config
import operator_gateway as op_gateway
import operator_policy as op

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_UNSUPPORTED = "UNSUPPORTED"
# A heartbeat older than five minutes is treated as stale.
_STALE_HEARTBEAT_SECONDS = 300


def _profile_home(profile: str, hermes_root: Path | None) -> Path:
    return op.resolve_profile_home(profile, hermes_root)


def _config_path(profile_home: Path) -> Path:
    return profile_home / "config.yaml"


def _env_path(profile_home: Path) -> Path:
    return profile_home / ".env"


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _jobs_file(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "jobs.json"


def _skills_dir(profile_home: Path) -> Path:
    return profile_home / "skills"


def _gateway_pid_path(profile_home: Path) -> Path:
    return profile_home / "gateway.pid"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"


def _ticker_heartbeat_path(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "ticker_heartbeat"


def _check_result(
    status: str,
    layer: str,
    code: str,
    message: str,
    suggested_action: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a single doctor check result."""
    result: dict[str, Any] = {
        "status": status,
        "layer": layer,
        "code": code,
        "message": message,
        "suggested_action": suggested_action,
    }
    if extra:
        result.update(extra)
    return result


def _is_process_alive(pid: int | None) -> bool:
    """Best-effort check whether ``pid`` is alive. Never raises."""
    if pid is None:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False


def _read_config_safe(profile_home: Path) -> dict[str, Any]:
    """Read and parse config.yaml, returning a sanitized summary or raising."""
    path = _config_path(profile_home)
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at {path.name}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read config.yaml.") from exc
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml root is not a mapping.")
    return op_config._redact_dict(cfg)


def _read_env_keys_safe(env_path: Path) -> tuple[set[str], set[str]]:
    """Return (all_keys, secret_like_keys) from a .env file. Never values."""
    keys: set[str] = set()
    secret_like: set[str] = set()
    if not env_path.exists():
        return (keys, secret_like)
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if not key:
                continue
            keys.add(key)
            if op_config._is_secret_env_name(key):
                secret_like.add(key)
    return (keys, secret_like)


def _read_cron_jobs_safe(profile_home: Path) -> list[dict[str, Any]]:
    """Read cron jobs.json and return the job list, or raise."""
    path = _jobs_file(profile_home)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        raise ValueError("jobs.json is neither a dict nor a list.")
    if not isinstance(jobs, list):
        raise ValueError("jobs.json jobs field is not a list.")
    return [j for j in jobs if isinstance(j, dict)]


def _count_skills_safe(profile_home: Path) -> int:
    """Count SKILL.md files under the profile skills directory."""
    root = _skills_dir(profile_home)
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("SKILL.md"):
        if path.is_file():
            count += 1
    return count


def _read_last_audit_record() -> dict[str, Any] | None:
    """Return the newest audit record, or None if no log exists."""
    log_path = op.audit_log_path()
    if not log_path.exists():
        return None
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _gateway_state_summary(profile_home: Path) -> dict[str, Any]:
    """Best-effort gateway state summary with no tokens."""
    state_path = _gateway_state_path(profile_home)
    summary: dict[str, Any] = {"state_file_exists": state_path.exists()}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            adapters: list[dict[str, Any]] = []
            for key in (
                "telegram",
                "discord",
                "slack",
                "signal",
                "whatsapp",
                "api_server",
            ):
                entry = state.get(key)
                if isinstance(entry, dict):
                    adapters.append(
                        {
                            "name": key,
                            "connected": bool(entry.get("connected", False)),
                        }
                    )
            summary["adapters"] = adapters
        except Exception as exc:
            summary["parse_error"] = (
                f"Could not parse gateway_state.json: {exc.__class__.__name__}"
            )
    return summary


def _check_operator_runtime() -> dict[str, Any]:
    return _check_result(
        status=STATUS_PASS,
        layer="operator",
        code="RUNTIME_REACHABLE",
        message="Operator diagnostic function is executing in the current process.",
        suggested_action="No action needed.",
        extra={
            "note": "This check confirms the tool callable ran, not a separate process boundary."
        },
    )


def _check_gateway_status(profile_home: Path) -> dict[str, Any]:
    pid_path = _gateway_pid_path(profile_home)
    heartbeat_path = _ticker_heartbeat_path(profile_home)

    # Hermes versions store either a numeric PID or structured gateway state.
    # Reuse the gateway parser so diagnostics follow the same fallback as the
    # gateway status tool.
    pid: int | None = op_gateway._read_gateway_pid_from_pid_file(pid_path)
    if pid is None:
        _state = op_gateway._read_gateway_state(_gateway_state_path(profile_home))
        pid = op_gateway._read_gateway_pid_from_state(_state)

    running = _is_process_alive(pid) if pid is not None else False

    heartbeat_mtime: float | None = None
    heartbeat_stale = False
    if heartbeat_path.exists():
        try:
            heartbeat_mtime = heartbeat_path.stat().st_mtime
            heartbeat_stale = (time.time() - heartbeat_mtime) > _STALE_HEARTBEAT_SECONDS
        except OSError:
            heartbeat_mtime = None

    if pid is None and not heartbeat_path.exists():
        return _check_result(
            status=STATUS_FAIL,
            layer="gateway",
            code="GATEWAY_UNREACHABLE",
            message="Gateway PID file and heartbeat are missing; gateway status cannot be verified.",
            suggested_action="Verify Hermes is running, or run hermes_operator_recover with apply=false to plan a restart.",
        )

    if pid is not None and not running:
        return _check_result(
            status=STATUS_FAIL,
            layer="gateway",
            code="GATEWAY_DEAD_PID",
            message=f"Gateway PID file exists ({pid}) but the process is not alive.",
            suggested_action="Run hermes_operator_recover with apply=true to restart the gateway.",
            extra={"pid": pid, "running": False},
        )

    # A heartbeat alone is not proof that the gateway is alive. Stale state files
    # can survive service restarts or crashes, so never emit GATEWAY_OK without
    # a live PID.
    if pid is None:
        return _check_result(
            status=STATUS_FAIL,
            layer="gateway",
            code="GATEWAY_PID_MISSING",
            message="Gateway heartbeat exists but no live gateway PID can be verified.",
            suggested_action="Verify the live systemd service and refresh gateway state before trusting operator health.",
            extra={"pid": None, "running": False, "heartbeat_mtime": heartbeat_mtime},
        )

    if heartbeat_stale:
        return _check_result(
            status=STATUS_WARN,
            layer="gateway",
            code="GATEWAY_STALE_HEARTBEAT",
            message="Gateway heartbeat file is older than 5 minutes.",
            suggested_action="Check the gateway process or run hermes_operator_recover with apply=false.",
            extra={
                "heartbeat_mtime": heartbeat_mtime,
                "stale_seconds": _STALE_HEARTBEAT_SECONDS,
            },
        )

    extra = {"pid": pid, "running": running}
    if heartbeat_mtime is not None:
        extra["heartbeat_mtime"] = heartbeat_mtime
    extra.update(_gateway_state_summary(profile_home))

    return _check_result(
        status=STATUS_PASS,
        layer="gateway",
        code="GATEWAY_OK",
        message="Gateway appears reachable based on PID and heartbeat.",
        suggested_action="No action needed.",
        extra=extra,
    )


def _check_config_readable(profile_home: Path) -> dict[str, Any]:
    path = _config_path(profile_home)
    if not path.exists():
        return _check_result(
            status=STATUS_WARN,
            layer="config",
            code="CONFIG_MISSING",
            message="config.yaml does not exist for this profile.",
            suggested_action="Create config.yaml if this profile needs custom configuration.",
        )
    try:
        summary = _read_config_safe(profile_home)
        return _check_result(
            status=STATUS_PASS,
            layer="config",
            code="CONFIG_READABLE",
            message="config.yaml exists and parses.",
            suggested_action="No action needed.",
            extra={"top_level_keys": sorted(summary.keys())[:20]},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="config",
            code="CONFIG_UNREADABLE",
            message=f"config.yaml could not be read or parsed: {exc.__class__.__name__}",
            suggested_action="Check config.yaml syntax and permissions.",
        )


def _check_env_readable(profile_home: Path) -> dict[str, Any]:
    env_path = _env_path(profile_home)
    if not env_path.exists():
        return _check_result(
            status=STATUS_WARN,
            layer="env",
            code="ENV_MISSING",
            message="No .env file exists for this profile.",
            suggested_action="Create a .env file only if this profile needs env overrides.",
            extra={"env_exists": False, "key_count": 0, "secret_like_count": 0},
        )
    try:
        keys, secret_like = _read_env_keys_safe(env_path)
        return _check_result(
            status=STATUS_PASS,
            layer="env",
            code="ENV_READABLE",
            message=".env file is readable; values are not exposed.",
            suggested_action="No action needed.",
            extra={
                "env_exists": True,
                "key_count": len(keys),
                "secret_like_count": len(secret_like),
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="env",
            code="ENV_UNREADABLE",
            message=f".env file could not be read: {exc.__class__.__name__}",
            suggested_action="Check .env file permissions.",
        )


def _check_cron_registry(profile_home: Path) -> dict[str, Any]:
    try:
        jobs = _read_cron_jobs_safe(profile_home)
        return _check_result(
            status=STATUS_PASS,
            layer="cron",
            code="CRON_REGISTRY_READABLE",
            message=f"Cron registry readable; {len(jobs)} job(s) found.",
            suggested_action="No action needed.",
            extra={"jobs_count": len(jobs)},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="cron",
            code="CRON_REGISTRY_UNREADABLE",
            message=f"Cron registry could not be read: {exc.__class__.__name__}",
            suggested_action="Inspect cron/jobs.json syntax and permissions.",
        )


def _check_skills_registry(profile_home: Path) -> dict[str, Any]:
    root = _skills_dir(profile_home)
    try:
        count = _count_skills_safe(profile_home)
        exists = root.exists()
        if not exists:
            return _check_result(
                status=STATUS_WARN,
                layer="skills",
                code="SKILLS_DIR_MISSING",
                message="Skills directory does not exist for this profile.",
                suggested_action="Create skills under this profile or verify HERMES_HOME.",
                extra={"skills_dir_exists": False, "count": 0},
            )
        return _check_result(
            status=STATUS_PASS,
            layer="skills",
            code="SKILLS_REGISTRY_READABLE",
            message=f"Skills directory readable; {count} SKILL.md file(s) found.",
            suggested_action="No action needed.",
            extra={"skills_dir_exists": True, "count": count},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="skills",
            code="SKILLS_REGISTRY_UNREADABLE",
            message=f"Skills directory could not be enumerated: {exc.__class__.__name__}",
            suggested_action="Check skills/ directory permissions.",
        )


def _check_operator_policy(profile: str, hermes_root: Path | None) -> dict[str, Any]:
    try:
        policy = op.OperatorPolicy()
        op.validate_profile_name(profile)
        profile_exists = op.profile_exists(profile, hermes_root)
        allowed = op.profile_is_allowed(profile, policy.allowed_profiles)
        return _check_result(
            status=STATUS_PASS,
            layer="policy",
            code="POLICY_OK",
            message="Operator policy parses and profile is allowed.",
            suggested_action="No action needed.",
            extra={
                "enabled": policy.enabled,
                "level": policy.level,
                "apply_mode": policy.apply_mode,
                "profile_exists": profile_exists,
                "profile_allowed": allowed,
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="policy",
            code="POLICY_INVALID",
            message=f"Operator policy check failed: {exc.__class__.__name__}",
            suggested_action="Review HERMES_GPT_OPERATOR_* environment variables.",
        )


def _check_last_audit_record() -> dict[str, Any]:
    try:
        record = _read_last_audit_record()
        if record is None:
            return _check_result(
                status=STATUS_WARN,
                layer="audit",
                code="AUDIT_LOG_EMPTY",
                message="Audit log exists but has no records yet.",
                suggested_action="No action needed; records will appear as tools are used.",
                extra={"audit_log_path": str(op.audit_log_path())},
            )
        return _check_result(
            status=STATUS_PASS,
            layer="audit",
            code="AUDIT_RECORD_READABLE",
            message="Last audit record is readable JSON.",
            suggested_action="No action needed.",
            extra={
                "audit_log_path": str(op.audit_log_path()),
                "last_record_tool": record.get("tool"),
                "last_record_timestamp": record.get("timestamp"),
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="audit",
            code="AUDIT_RECORD_UNREADABLE",
            message=f"Last audit record could not be read: {exc.__class__.__name__}",
            suggested_action="Check audit log path and permissions.",
        )


def _check_connector_api_bridge(profile_home: Path) -> dict[str, Any]:
    """Best-effort connector check. Always reports UNSUPPORTED because hermes-gpt
    does not implement a connector re-registration API or health endpoint.
    """
    state_summary = _gateway_state_summary(profile_home)
    return _check_result(
        status=STATUS_UNSUPPORTED,
        layer="connector",
        code="CONNECTOR_REREGISTRATION_UNSUPPORTED",
        message="No supported connector re-registration command or API was found.",
        suggested_action="If the connector is stale, recreate it in ChatGPT / your MCP client, or restart the gateway and reconnect manually.",
        extra={
            "supported": False,
            "action": "manual",
            "gateway_state_summary": state_summary,
        },
    )
