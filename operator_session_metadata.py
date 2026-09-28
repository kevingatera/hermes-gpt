"""Profile-scoped title and pin updates for existing Hermes sessions."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import operator_policy as op
import operator_session as sessions

MAX_SESSION_TITLE_CHARS = 100
MAX_SESSION_ID_CHARS = 256
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
SESSION_COMMAND_TIMEOUT = 30

_CLI_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMPDIR",
        "USER",
        "WINDIR",
    }
)


def _error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(
        layer="session_control",
        code=code,
        safe_message=message,
        suggested_action=action,
    )


def _validate_session_id(session_id: str) -> str | dict[str, Any]:
    if (
        not isinstance(session_id, str)
        or len(session_id) > MAX_SESSION_ID_CHARS
        or not SESSION_ID_RE.fullmatch(session_id)
    ):
        return _error(
            "INVALID_SESSION_ID",
            "session_id contains unsupported characters or exceeds 256 characters.",
            "Use an exact or unique-prefix ID returned by hermes_session_list.",
        )
    return session_id


def _profile_cli(
    profile: str,
    hermes_root: Path | None,
    agent_root: Path | None,
) -> tuple[list[str], Path, dict[str, str], str] | dict[str, Any]:
    safe_profile = sessions.validate_session_profile(profile, hermes_root)
    if isinstance(safe_profile, dict):
        return safe_profile
    profile_home, inherited_env = sessions._profile_runtime(safe_profile, hermes_root)
    child_env = {
        key: value
        for key, value in inherited_env.items()
        if key in _CLI_ENV_KEYS
    }
    child_env["HERMES_HOME"] = str(profile_home)
    child_env["HERMES_PROFILE"] = safe_profile
    return (
        [sessions._hermes_executable(agent_root)],
        profile_home,
        child_env,
        safe_profile,
    )


def _run_session_command(
    args: list[str],
    *,
    profile: str,
    hermes_root: Path | None,
    agent_root: Path | None,
    success_fields: dict[str, Any],
) -> dict[str, Any]:
    if not op.env_truthy(sessions.ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {sessions.ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    runtime = _profile_cli(profile, hermes_root, agent_root)
    if isinstance(runtime, dict):
        return runtime
    executable, profile_home, child_env, safe_profile = runtime

    try:
        completed = subprocess.run(
            [*executable, "sessions", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(profile_home),
            env=child_env,
            timeout=SESSION_COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error(
            "SESSION_ACTION_TIMEOUT",
            "Hermes did not finish updating the session metadata in time.",
            "Check the profile session database and retry.",
        )
    except (OSError, ValueError) as exc:
        return _error(
            "HERMES_START_FAILED",
            op.redact_output(str(exc))[:500],
            "Check the Hermes CLI installation and profile data directory.",
        )

    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or "Hermes rejected the session update."
        ).strip()
        return _error(
            "SESSION_ACTION_FAILED",
            op.redact_output(detail)[:1_000],
            "Check the session ID and profile, then retry.",
        )
    return {"success": True, "profile": safe_profile, **success_fields}


def hermes_session_rename(
    session_id: str,
    title: str,
    *,
    profile: str = "default",
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Set the title of one session through Hermes' session-store command."""
    checked_id = _validate_session_id(session_id)
    if isinstance(checked_id, dict):
        return checked_id
    if not isinstance(title, str):
        return _error(
            "INVALID_SESSION_TITLE",
            f"title must contain 1 to {MAX_SESSION_TITLE_CHARS} printable characters.",
            "Provide a shorter title without control characters or newlines.",
        )
    safe_title = title.strip()
    if (
        not safe_title
        or len(safe_title) > MAX_SESSION_TITLE_CHARS
        or not safe_title.isprintable()
    ):
        return _error(
            "INVALID_SESSION_TITLE",
            f"title must contain 1 to {MAX_SESSION_TITLE_CHARS} printable characters.",
            "Provide a shorter title without control characters or newlines.",
        )
    return _run_session_command(
        ["rename", "--", checked_id, safe_title],
        profile=profile,
        hermes_root=hermes_root,
        agent_root=agent_root,
        success_fields={"session_id": checked_id, "renamed": True},
    )


def hermes_session_pin(
    session_id: str,
    pinned: bool = True,
    *,
    profile: str = "default",
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Set or clear Hermes' durable pin flag for one session and its lineage."""
    checked_id = _validate_session_id(session_id)
    if isinstance(checked_id, dict):
        return checked_id
    if not isinstance(pinned, bool):
        return _error(
            "INVALID_PIN_STATE",
            "pinned must be a boolean.",
            "Set pinned to true or false.",
        )
    action = "pin" if pinned else "unpin"
    return _run_session_command(
        [action, "--", checked_id],
        profile=profile,
        hermes_root=hermes_root,
        agent_root=agent_root,
        success_fields={"session_id": checked_id, "pinned": pinned},
    )


__all__ = [
    "MAX_SESSION_TITLE_CHARS",
    "hermes_session_pin",
    "hermes_session_rename",
]
