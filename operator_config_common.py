"""Shared profile paths, backups, and secret-name checks for config tools."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Key safety rules
# ---------------------------------------------------------------------------

# Substrings that mark a config key path or env var name as secret-like.
_SECRET_KEY_SUBSTRINGS = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "auth",
    "cookie",
    "private",
    "oauth",
)

# Env var names that influence subprocess / loader / interpreter behavior.
# Mirrors Hermes' own _ENV_VAR_NAME_DENYLIST so the operator layer does not
# become a bypass for it. These are refused by env_set_nonsecret regardless
# of secret-like naming.
_ENV_NAME_DENYLIST: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_DEBUG",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "SHELL",
        "BROWSER",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
        "GIT_SHELL",
        "HERMES_HOME",
        "HERMES_PROFILE",
        "HERMES_CONFIG",
        "HERMES_ENV",
    }
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:token|secret|password|api[_-]?key|passwd|auth)\s*[:=]\s*[\"']?[^\s\"'#;]{4,}"
)


def _is_secret_key(key_path: str) -> bool:
    lower = (key_path or "").lower()
    return any(s in lower for s in _SECRET_KEY_SUBSTRINGS)


def _is_secret_assignment(text: str) -> bool:
    return bool(text) and bool(_SECRET_ASSIGNMENT_RE.search(text))


def _is_secret_env_name(name: str) -> bool:
    upper = (name or "").upper()
    for s in _SECRET_KEY_SUBSTRINGS:
        if s.upper() in upper:
            return True
    return False


def _config_path(profile_home: Path) -> Path:
    return profile_home / "config.yaml"


def _env_path(profile_home: Path) -> Path:
    return profile_home / ".env"


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    try:
        shutil.copy2(path, bak)
        return bak
    except OSError:
        return None
