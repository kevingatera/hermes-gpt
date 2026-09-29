"""Read-only discovery for Hermes profiles authorized for session control."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

import operator_policy as op
import operator_session as sessions
from operator_session_job_store import _data_root

_MAX_CONFIG_BYTES = 1_000_000
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(
        layer="session_control",
        code=code,
        safe_message=message,
        suggested_action=action,
    )


def _profile_defaults(profile_home: Path) -> dict[str, str]:
    """Return only model settings safe to show to an MCP client."""
    config_path = profile_home / "config.yaml"
    try:
        with config_path.open("rb") as config_file:
            config_bytes = config_file.read(_MAX_CONFIG_BYTES + 1)
        if len(config_bytes) > _MAX_CONFIG_BYTES:
            return {}
        config = yaml.safe_load(config_bytes)
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    if not isinstance(config, dict):
        return {}

    defaults: dict[str, str] = {}
    model_config = config.get("model")
    if isinstance(model_config, dict):
        default_model = model_config.get("default")
        if (
            isinstance(default_model, str)
            and sessions.MODEL_ID_RE.fullmatch(default_model.strip())
            and op.redact_output(default_model.strip()) == default_model.strip()
        ):
            defaults["default_model"] = default_model.strip()
        provider = model_config.get("provider")
        if (
            isinstance(provider, str)
            and _PROVIDER_ID_RE.fullmatch(provider.strip())
            and op.redact_output(provider.strip()) == provider.strip()
        ):
            defaults["configured_provider"] = provider.strip()

    agent_config = config.get("agent")
    effort = agent_config.get("reasoning_effort") if isinstance(agent_config, dict) else None
    if isinstance(effort, str) and effort in sessions.REASONING_EFFORTS:
        defaults["configured_reasoning_effort"] = effort
    return defaults


def hermes_session_profiles(hermes_root: Path | None = None) -> dict[str, Any]:
    """List existing profiles allowed by both session control and Operator policy."""
    if not op.env_truthy(sessions.ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {sessions.ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    raw_allowed = sessions.SESSION_ALLOWED_PROFILES_ENV
    configured = [
        item.strip()
        for item in os.environ.get(raw_allowed, "").split(",")
        if item.strip()
    ]
    if not configured:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "No Hermes profiles are authorized for session control.",
            f"Set {raw_allowed} to the restricted profile names this server may run.",
        )
    if "*" in configured:
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist does not accept wildcards.",
            f"Set {raw_allowed} to explicit Hermes profile names.",
        )
    try:
        allowed = {op.validate_profile_name(item) for item in configured}
    except (TypeError, ValueError):
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist contains an invalid profile name.",
            f"Correct {raw_allowed} and retry.",
        )

    root = _data_root(hermes_root)
    profiles: list[dict[str, str]] = []
    for profile in op.list_existing_profiles(root):
        if profile not in allowed:
            continue
        authorized = sessions.validate_session_profile(profile, root)
        if isinstance(authorized, dict):
            continue
        profile_home = op.resolve_profile_home(profile, root)
        profiles.append({"profile": profile, **_profile_defaults(profile_home)})

    if not profiles:
        return _error(
            "SESSION_PROFILE_NOT_FOUND",
            "No existing Hermes profiles are authorized for session control.",
            "Check the session-control and Operator profile allowlists, then create the selected Hermes profile.",
        )
    profiles.sort(key=lambda item: item["profile"])
    return {
        "success": True,
        "profiles": profiles,
        "reasoning_efforts": sorted(sessions.REASONING_EFFORTS),
    }


__all__ = ["hermes_session_profiles"]
