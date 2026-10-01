"""Profile-scoped Hermes session validation and execution requests."""

from __future__ import annotations

from hermes_gpt.policy import runtime_settings

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from hermes_gpt.policy import authorization as op
from hermes_gpt.sessions.job_store import _data_root
from hermes_gpt.sessions.jobs import ENABLE_SESSION_CONTROL_ENV, MAX_PROMPT_CHARS, MAX_RESULT_CHARS, MAX_TIMEOUT, MIN_TIMEOUT, MODEL_ID_RE, REASONING_EFFORTS, SESSION_ALLOWED_PROFILES_ENV, _error, _hermes_executable, hermes_session_job_cancel, hermes_session_job_result, hermes_session_job_status, start_managed_session_job


def validate_session_profile(
    profile: str, hermes_root: Path | None = None
) -> str | dict[str, Any]:
    """Require an explicitly scoped profile for Hermes task execution."""
    try:
        safe_profile = op.validate_profile_name(profile)
    except (TypeError, ValueError):
        return _error(
            "INVALID_PROFILE",
            "profile is not a valid Hermes profile name.",
            "Choose a valid profile name.",
        )

    configured = [
        item.strip()
        for item in runtime_settings.getenv(SESSION_ALLOWED_PROFILES_ENV, "").split(",")
        if item.strip()
    ]
    if not configured:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "No Hermes profiles are authorized for session control.",
            f"Set {SESSION_ALLOWED_PROFILES_ENV} to the restricted profile names this server may run.",
        )
    if "*" in configured:
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist does not accept wildcards.",
            f"Set {SESSION_ALLOWED_PROFILES_ENV} to explicit Hermes profile names.",
        )
    try:
        allowed = {op.validate_profile_name(item) for item in configured}
    except (TypeError, ValueError):
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist contains an invalid profile name.",
            f"Correct {SESSION_ALLOWED_PROFILES_ENV} and retry.",
        )
    if safe_profile not in allowed:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "The requested Hermes profile is not authorized for session control.",
            f"Choose a profile listed in {SESSION_ALLOWED_PROFILES_ENV}.",
        )

    try:
        op.OperatorPolicy().require_profile(safe_profile, _data_root(hermes_root))
    except FileNotFoundError:
        return _error(
            "SESSION_PROFILE_NOT_FOUND",
            "The requested Hermes profile does not exist.",
            "Create and configure the restricted Hermes profile before enabling session control.",
        )
    except PermissionError:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "The requested Hermes profile is not authorized by Operator policy.",
            f"Allow the profile with {op.OPERATOR_ALLOWED_PROFILES_ENV} as well.",
        )
    return safe_profile


def _validate_prompt_timeout(
    prompt: str,
    timeout: int,
    *,
    existing_session: bool = False,
) -> tuple[str, int] | dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        return _error(
            "INVALID_PROMPT",
            "prompt must not be empty.",
            (
                "Provide the next instruction for the existing Hermes session."
                if existing_session
                else "Provide a prompt for the Hermes session."
            ),
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        return _error(
            "PROMPT_TOO_LARGE",
            f"prompt exceeds the {MAX_PROMPT_CHARS}-character limit.",
            "Send a shorter prompt.",
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return _error(
            "INVALID_TIMEOUT",
            "timeout must be an integer number of seconds.",
            f"Choose {MIN_TIMEOUT} to {MAX_TIMEOUT} seconds.",
        )
    return prompt, max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))


def _validate_model_overrides(
    model: str | None,
    reasoning_effort: str | None,
) -> dict[str, Any] | None:
    if model is not None and (
        not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model)
    ):
        return _error(
            "INVALID_MODEL",
            "model must be a provider/model identifier of at most 256 characters.",
            "Choose a model ID accepted by the Hermes CLI.",
        )
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str)
        or reasoning_effort not in REASONING_EFFORTS
    ):
        return _error(
            "INVALID_REASONING_EFFORT",
            "reasoning_effort is not supported by the Hermes CLI.",
            f"Choose one of: {', '.join(sorted(REASONING_EFFORTS))}.",
        )
    return None


def _profile_runtime(
    profile: str,
    hermes_root: Path | None,
) -> tuple[Path, dict[str, str]]:
    base_home = (
        Path(hermes_root)
        if hermes_root is not None
        else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    profile_home = op.resolve_profile_home(profile, base_home)
    child_env = runtime_settings.effective_environment()
    # Child tools receive the current policy, not connection administration.
    child_env[runtime_settings.ADMIN_ENV] = "0"
    child_env["HERMES_HOME"] = str(profile_home)
    child_env["HERMES_PROFILE"] = profile
    return profile_home, child_env


def _validate_start(
    session_id: str,
    prompt: str,
    timeout: int,
    profile: str = "default",
    hermes_root: Path | None = None,
) -> tuple[str, str, int, str] | dict[str, Any]:
    if not op.env_truthy(ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id.strip()) > 256
    ):
        return _error(
            "INVALID_SESSION_ID",
            "session_id must contain 1 to 256 characters.",
            "Use an ID returned by hermes_session_list.",
        )
    prompt_timeout = _validate_prompt_timeout(
        prompt, timeout, existing_session=True
    )
    if isinstance(prompt_timeout, dict):
        return prompt_timeout
    safe_prompt, safe_timeout = prompt_timeout
    safe_profile = validate_session_profile(profile, hermes_root)
    if isinstance(safe_profile, dict):
        return safe_profile
    return (
        session_id.strip(),
        safe_prompt,
        safe_timeout,
        safe_profile,
    )


def hermes_session_continue(
    session_id: str,
    prompt: str,
    timeout: int = 900,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Start one bounded turn in an existing Hermes session."""
    checked = _validate_start(session_id, prompt, timeout, profile, hermes_root)
    if isinstance(checked, dict):
        return checked
    safe_id, safe_prompt, safe_timeout, safe_profile = checked
    override_error = _validate_model_overrides(model, reasoning_effort)
    if override_error:
        return override_error
    executable = _hermes_executable(agent_root)
    argv = [executable, "chat", "--resume", safe_id]
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["--reasoning", reasoning_effort])
    argv.extend(["--query-file", "-", "--oneshot", "-Q"])
    profile_home, child_env = _profile_runtime(safe_profile, hermes_root)
    return start_managed_session_job(
        argv=argv,
        prompt=safe_prompt,
        timeout=safe_timeout,
        profile=safe_profile,
        hermes_root=hermes_root,
        child_env=child_env,
        cwd=profile_home,
        active_key=f"{safe_profile}:{safe_id}",
        metadata={
            "session_id": safe_id,
            "model": model or None,
            "reasoning_effort": reasoning_effort or None,
        },
    )


def hermes_session_start(
    prompt: str,
    timeout: int = 900,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Start a bounded turn in a new session on an authorized Hermes profile."""
    if not op.env_truthy(ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    prompt_timeout = _validate_prompt_timeout(prompt, timeout)
    if isinstance(prompt_timeout, dict):
        return prompt_timeout
    safe_prompt, safe_timeout = prompt_timeout
    safe_profile = validate_session_profile(profile, hermes_root)
    if isinstance(safe_profile, dict):
        return safe_profile
    override_error = _validate_model_overrides(model, reasoning_effort)
    if override_error:
        return override_error

    executable = _hermes_executable(agent_root)
    argv = [executable, "chat"]
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["--reasoning", reasoning_effort])
    argv.extend(["--query-file", "-", "--oneshot", "-Q"])
    profile_home, child_env = _profile_runtime(safe_profile, hermes_root)
    return start_managed_session_job(
        argv=argv,
        prompt=safe_prompt,
        timeout=safe_timeout,
        profile=safe_profile,
        hermes_root=hermes_root,
        child_env=child_env,
        cwd=profile_home,
        active_key=f"{safe_profile}:new:{uuid4().hex}",
        metadata={
            "session_id": "",
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
    )


__all__ = [
    "ENABLE_SESSION_CONTROL_ENV",
    "MAX_PROMPT_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_TIMEOUT",
    "MIN_TIMEOUT",
    "MODEL_ID_RE",
    "REASONING_EFFORTS",
    "SESSION_ALLOWED_PROFILES_ENV",
    "hermes_session_continue",
    "hermes_session_job_cancel",
    "hermes_session_job_result",
    "hermes_session_job_status",
    "hermes_session_start",
    "start_managed_session_job",
    "validate_session_profile",
]
