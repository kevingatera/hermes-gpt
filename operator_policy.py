"""Operator policy gates and structured error helpers for hermes-gpt.

This module keeps the policy snapshot and structured errors together. It also
re-exports audit, path, and subprocess helpers for existing Operator callers.

Design rules enforced here:
- Default behavior is read-only.
- Mutating tools are disabled by default.
- Dry-run is the default apply mode.
- Direct mutation requires explicit env opt-in.
- Owner Mode requires an additional explicit acknowledgement.
- No secrets are exposed.
- No `.env` raw read/write.
- No vault/token/auth/cookie/SSH access.
- Child processes use the fixed-argv, `shell=False` helper in
  ``operator_subprocess``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import operator_audit as _audit
import operator_policy_paths as _paths
import operator_subprocess as _subprocess
from operator_redaction import redact_output

# Keep the historical imports used by the Operator tools and adapters.
set_audit_log_override = _audit.set_audit_log_override
audit_log_path = _audit.audit_log_path
audit_record = _audit.audit_record
audit_tail = _audit.audit_tail
run_argv = _subprocess.run_argv
AUDIT_LOG_HERMES_PATH = _audit.AUDIT_LOG_HERMES_PATH
AUDIT_LOG_FALLBACK_PATH = _audit.AUDIT_LOG_FALLBACK_PATH
DEFAULT_DENIED_BASENAMES = _paths.DEFAULT_DENIED_BASENAMES
DEFAULT_DENIED_DIR_NAMES = _paths.DEFAULT_DENIED_DIR_NAMES
SECRET_PATH_SUBSTRINGS = _paths.SECRET_PATH_SUBSTRINGS
_normalize_path = _paths._normalize_path
normalize_hermes_data_root = _paths.normalize_hermes_data_root
is_denied_path = _paths.is_denied_path
validate_profile_name = _paths.validate_profile_name
parse_allowed_profiles = _paths.parse_allowed_profiles
profile_is_allowed = _paths.profile_is_allowed
list_existing_profiles = _paths.list_existing_profiles
resolve_profile_home = _paths.resolve_profile_home
profile_exists = _paths.profile_exists
parse_path_list = _paths.parse_path_list
path_under_allowed = _paths.path_under_allowed

# ---------------------------------------------------------------------------
# Env var names
# ---------------------------------------------------------------------------

OPERATOR_ENABLED_ENV = "HERMES_GPT_OPERATOR_ENABLED"
OPERATOR_LEVEL_ENV = "HERMES_GPT_OPERATOR_LEVEL"
OPERATOR_APPLY_MODE_ENV = "HERMES_GPT_OPERATOR_APPLY_MODE"
OPERATOR_ALLOWED_PROFILES_ENV = "HERMES_GPT_OPERATOR_ALLOWED_PROFILES"
OPERATOR_ALLOWED_PATHS_ENV = "HERMES_GPT_OPERATOR_ALLOWED_PATHS"
OPERATOR_DENIED_PATHS_ENV = "HERMES_GPT_OPERATOR_DENIED_PATHS"
OWNER_ACK_ENV = "HERMES_GPT_OWNER_ACK"
OWNER_ACTIVE_ENV = "HERMES_GPT_OWNER_ACTIVE"

OWNER_ACK_REQUIRED_VALUE = "I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"

# ---------------------------------------------------------------------------
# Truthy helper
# ---------------------------------------------------------------------------

_TRUTHY_VALUES = {"1", "true", "yes", "on", "enabled"}


def is_truthy(value: Any) -> bool:
    """Return True if ``value`` is a recognized truthy string.

    Truthy: "1", "true", "yes", "on", "enabled" (case-insensitive).
    Falsey: "0", "false", "no", "off", "disabled", empty/unset, anything else.
    Non-string inputs are coerced via str().
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in _TRUTHY_VALUES


def env_truthy(name: str) -> bool:
    """Read env var ``name`` and apply ``is_truthy``."""
    return is_truthy(os.environ.get(name))


# ---------------------------------------------------------------------------
# Structured error envelope
# ---------------------------------------------------------------------------

# Allowed layer values. New layers should be added here so callers stay
# consistent and tooling can rely on a bounded vocabulary.
_ERROR_LAYERS: frozenset[str] = frozenset(
    {
        "operator",
        "policy",
        "config",
        "env",
        "cron",
        "skills",
        "gateway",
        "workspace",
        "owner",
        "audit",
        "connector",
        "release",
        "session_control",
        "system",
    }
)


def new_trace_id() -> str:
    """Return a short random trace id for correlating operator failures."""
    import uuid

    return uuid.uuid4().hex[:16]


def _sanitize_exception_message(exc: Exception) -> str:
    """Return a safe exception message.

    Applies best-effort redaction of secret-looking values (API keys, tokens,
    bearer headers, AWS keys) and absolute filesystem paths while preserving
    human-readable error text such as "denied", "blocked", or "not found".
    The result is capped to avoid accidental dumps.
    """
    raw = str(exc)
    # Redact known secret value patterns.
    redacted = redact_output(raw)
    # Redact absolute filesystem paths.
    redacted = re.sub(
        r"(?i)([a-z]:\\[^\s]*|\\\\[^\s]*|/[^\s]*)",
        "[REDACTED_PATH]",
        redacted,
    )
    # Cap length to avoid accidental dumps.
    return redacted[:500]


def make_error_envelope(
    *,
    layer: str,
    code: str,
    safe_message: str,
    suggested_action: str,
    trace_id: str | None = None,
    legacy_error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe structured error envelope for operator-facing failures.

    Backward compatibility: the envelope still includes ``success: false`` and
    ``error`` so existing callers continue to work.
    """
    if layer not in _ERROR_LAYERS:
        layer = "operator"
    if not code:
        code = "UNKNOWN_ERROR"
    if not safe_message:
        safe_message = "An operator error occurred."
    if not suggested_action:
        suggested_action = "Run hermes_operator_doctor for more details."
    tid = trace_id or new_trace_id()
    error = legacy_error or safe_message
    envelope: dict[str, Any] = {
        "success": False,
        "ok": False,
        "error": error,
        "layer": layer,
        "code": code,
        "safe_message": safe_message,
        "suggested_action": suggested_action,
        "trace_id": tid,
    }
    if extra:
        for key, value in extra.items():
            if isinstance(value, str):
                envelope[key] = value[:500]
            else:
                envelope[key] = value
    return envelope


def error_from_exception(
    exc: Exception,
    *,
    layer: str,
    code: str,
    suggested_action: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an error envelope from an exception, sanitizing the message."""
    safe_message = _sanitize_exception_message(exc)
    return make_error_envelope(
        layer=layer,
        code=code,
        safe_message=safe_message,
        suggested_action=suggested_action,
        trace_id=trace_id,
        legacy_error=safe_message,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Operator levels
# ---------------------------------------------------------------------------

# Ordered from least to most privilege. Higher levels include all lower
# capabilities.
LEVELS = ["read_only", "cron", "skills", "skills_config", "workspace", "owner"]


def level_rank(level: str) -> int:
    """Return the integer rank of a level name. Unknown levels map to -1."""
    try:
        return LEVELS.index(level)
    except ValueError:
        return -1


def has_level(required: str, actual: str) -> bool:
    """Return True if ``actual`` level satisfies ``required`` (>= rank)."""
    return level_rank(actual) >= level_rank(required)


# ---------------------------------------------------------------------------
# Policy snapshot
# ---------------------------------------------------------------------------


class OperatorPolicy:
    """Snapshot of the operator policy at call time.

    Reading env vars at construction time means tests that monkeypatch env
    get a fresh policy each call.
    """

    __slots__ = (
        "allowed_paths",
        "allowed_profiles",
        "apply_mode",
        "denied_paths",
        "enabled",
        "level",
        "mutation_allowed",
        "owner_ack",
        "owner_active",
        "owner_mode_ready",
    )

    def __init__(self) -> None:
        self.enabled = env_truthy(OPERATOR_ENABLED_ENV)
        self.owner_active = env_truthy(OWNER_ACTIVE_ENV)
        raw_level = os.environ.get(OPERATOR_LEVEL_ENV, "read_only").strip().lower()
        if raw_level not in LEVELS:
            raw_level = "read_only"
        if raw_level == "owner" and not self.owner_active:
            raw_level = "workspace"
        self.level = raw_level

        raw_mode = os.environ.get(OPERATOR_APPLY_MODE_ENV, "dry_run").strip().lower()
        if raw_mode not in {"dry_run", "direct"}:
            raw_mode = "dry_run"
        self.apply_mode = raw_mode

        self.allowed_profiles = parse_allowed_profiles(
            os.environ.get(OPERATOR_ALLOWED_PROFILES_ENV)
        )
        self.allowed_paths = parse_path_list(os.environ.get(OPERATOR_ALLOWED_PATHS_ENV))
        # Denied paths env adds to the built-in defaults; it cannot remove
        # the defaults. We don't store the env list as paths here because
        # ``is_denied_path`` already covers the built-in conservative set.
        self.denied_paths = parse_path_list(os.environ.get(OPERATOR_DENIED_PATHS_ENV))

        self.owner_ack = os.environ.get(OWNER_ACK_ENV, "")

        self.owner_mode_ready = (
            self.enabled
            and self.owner_active
            and self.level == "owner"
            and self.owner_ack == OWNER_ACK_REQUIRED_VALUE
        )

        self.mutation_allowed = (
            self.enabled
            and self.apply_mode == "direct"
            and level_rank(self.level) >= level_rank("cron")
        )

    # --- convenience -------------------------------------------------------

    def effective_dry_run(self, requested_dry_run: bool) -> bool:
        """Effective dry-run is True if either input says dry-run OR policy is
        not in direct apply mode."""
        if requested_dry_run:
            return True
        return self.apply_mode != "direct"

    def require_enabled(self) -> None:
        if not self.enabled:
            raise PermissionError(
                f"Operator mode is disabled. Set {OPERATOR_ENABLED_ENV}=1 to enable it."
            )

    def require_level(self, required: str) -> None:
        self.require_enabled()
        if not has_level(required, self.level):
            raise PermissionError(
                f"Operator level {self.level!r} does not satisfy required level {required!r}. "
                f"Set {OPERATOR_LEVEL_ENV} to at least {required!r}."
            )

    def require_mutation(self, dry_run: bool) -> None:
        """Gate for any mutating operation. Dry-run is allowed at any enabled
        level that satisfies ``required``. Direct requires direct apply mode."""
        # Level check is caller's responsibility; this method gates the
        # apply-mode axis only.
        if self.effective_dry_run(dry_run):
            return
        # Direct path: require enabled + direct mode.
        if not (self.enabled and self.apply_mode == "direct"):
            raise PermissionError(
                "Direct mutation requires operator mode enabled with "
                f"{OPERATOR_APPLY_MODE_ENV}=direct."
            )

    def require_owner(self, dry_run: bool) -> None:
        """Gate for owner-only operations."""
        self.require_level("owner")
        if not self.owner_mode_ready:
            raise PermissionError(
                "Owner Mode requires "
                f"{OPERATOR_ENABLED_ENV}=1, {OPERATOR_LEVEL_ENV}=owner, "
                f"{OWNER_ACTIVE_ENV}=1, and "
                f"{OWNER_ACK_ENV}={OWNER_ACK_REQUIRED_VALUE!r}."
            )
        # Owner direct still requires direct apply mode + dry_run=False.
        if not self.effective_dry_run(dry_run) and self.apply_mode != "direct":
            raise PermissionError(
                f"Owner direct mutation requires {OPERATOR_APPLY_MODE_ENV}=direct."
            )

    def require_profile(self, profile: str, hermes_root: Path | None) -> None:
        canon = validate_profile_name(profile)
        if not profile_exists(canon, hermes_root):
            raise FileNotFoundError(
                f"Profile {canon!r} does not exist under "
                f"{hermes_root or '<hermes root unavailable>'}."
            )
        if not profile_is_allowed(canon, self.allowed_profiles):
            raise PermissionError(
                f"Profile {canon!r} is not in the allowed profiles list "
                f"({OPERATOR_ALLOWED_PROFILES_ENV})."
            )

    def require_workspace_path(self, path: str | os.PathLike[str]) -> None:
        """For workspace/owner file tools: path must be under an allowed
        path AND not a denied path. Owner mode does NOT bypass the denied
        check (no secret override in this PR)."""
        if is_denied_path(path):
            raise PermissionError(
                f"Path {str(path)!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env)."
            )
        if not self.allowed_paths:
            raise PermissionError(
                "Workspace writes are disabled because "
                f"{OPERATOR_ALLOWED_PATHS_ENV} is empty. Set it to one or more "
                "workspace root directories."
            )
        if not path_under_allowed(path, self.allowed_paths):
            raise PermissionError(
                f"Path {str(path)!r} is not under any allowed path in "
                f"{OPERATOR_ALLOWED_PATHS_ENV}."
            )

    def to_summary(self) -> dict[str, Any]:
        """Return a JSON-safe summary. Never includes raw env values."""
        return {
            "enabled": self.enabled,
            "level": self.level,
            "apply_mode": self.apply_mode,
            "allowed_profiles": list(self.allowed_profiles),
            "allowed_paths_count": len(self.allowed_paths),
            "allowed_paths_summary": [str(p) for p in self.allowed_paths[:8]],
            "denied_paths_count": len(self.denied_paths),
            "denied_paths_summary": [str(p) for p in self.denied_paths[:8]],
            "owner_active": self.owner_active,
            "owner_mode_ready": self.owner_mode_ready,
            "mutation_allowed": self.mutation_allowed,
            "available_capability_groups": _capability_groups(self.level),
        }


def _capability_groups(level: str) -> list[str]:
    """Return the capability group names available at ``level``."""
    groups: list[str] = ["read_only"]
    if level_rank(level) >= level_rank("cron"):
        groups.append("cron")
    if level_rank(level) >= level_rank("skills"):
        groups.append("skills")
    if level_rank(level) >= level_rank("skills_config"):
        groups.append("skills_config")
    if level_rank(level) >= level_rank("workspace"):
        groups.append("workspace")
    if level_rank(level) >= level_rank("owner"):
        groups.append("owner")
    return groups


# ---------------------------------------------------------------------------
# Unified diff helper
# ---------------------------------------------------------------------------


def unified_diff(old: str, new: str, label: str = "content") -> str:
    """Return a unified diff string. Empty if no changes."""
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
    )
    return "".join(diff)
