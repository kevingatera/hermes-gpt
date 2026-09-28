"""Read narrowly scoped local browser endpoints from authorized Hermes profiles."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

import operator_policy as op

BROWSER_ALLOWED_PROFILES_ENV = "HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_DEVTOOLS_PATH_RE = re.compile(r"^/devtools/browser/[A-Za-z0-9_-]+/?$")


def validate_local_cdp_port(value: str) -> int:
    """Keep only the loopback port so a profile cannot supply a remote token URL."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("The selected browser profile needs a local browser.cdp_url.")
    raw = value.strip()
    if any(ord(char) < 32 for char in raw):
        raise ValueError("The selected browser profile needs a valid local browser.cdp_url.")
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parts = urlsplit(candidate)
        hostname = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ValueError("The selected browser profile needs a valid local browser.cdp_url.") from exc
    if (
        parts.scheme not in {"http", "ws"}
        or hostname not in _LOCAL_HOSTS
        or not port
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (
            parts.path not in {"", "/"}
            and not _DEVTOOLS_PATH_RE.fullmatch(parts.path)
        )
    ):
        raise ValueError("Only a local Chromium browser endpoint without credentials is supported.")
    return port


def _allowed_profiles() -> set[str]:
    raw = os.environ.get(BROWSER_ALLOWED_PROFILES_ENV, "")
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    if not entries or "*" in entries:
        raise PermissionError(
            f"Set {BROWSER_ALLOWED_PROFILES_ENV} to explicit Hermes profile names."
        )
    try:
        return {op.validate_profile_name(item) for item in entries}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{BROWSER_ALLOWED_PROFILES_ENV} contains an invalid profile name.") from exc


def allowed_profile_names() -> tuple[str, ...]:
    """Return the explicitly configured browser-profile aliases in stable order."""
    return tuple(sorted(_allowed_profiles()))


def profile_cdp_port(profile: str, hermes_root: Path | None) -> int:
    """Return the selected profile's local CDP port without exposing its config."""
    safe_profile = op.validate_profile_name(profile)
    if safe_profile not in _allowed_profiles():
        raise PermissionError(
            f"The requested browser profile is not authorized by {BROWSER_ALLOWED_PROFILES_ENV}."
        )
    profile_home = op.resolve_profile_home(safe_profile, hermes_root)
    data_root = (op.normalize_hermes_data_root(hermes_root) or profile_home).resolve(strict=True)
    profile_home = profile_home.resolve(strict=True)
    try:
        profile_home.relative_to(data_root)
    except ValueError as exc:
        raise PermissionError("The browser profile is outside the Hermes data root.") from exc
    config_path = profile_home / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise FileNotFoundError("The selected Hermes profile has no readable config.yaml.")
    try:
        config: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("The selected Hermes profile has an unreadable browser configuration.") from exc
    browser_config = config.get("browser") if isinstance(config, dict) else None
    endpoint = browser_config.get("cdp_url") if isinstance(browser_config, dict) else None
    return validate_local_cdp_port(endpoint)


__all__ = [
    "BROWSER_ALLOWED_PROFILES_ENV",
    "allowed_profile_names",
    "profile_cdp_port",
    "validate_local_cdp_port",
]
