"""Sanitized config.yaml tools and the legacy profile-config import surface."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_config_common import (
    _backup_file,
    _config_path,
    _is_secret_assignment,
    _is_secret_key,
)
from operator_env import (
    _is_secret_env_name,
    _read_env_value,
    hermes_env_copy_nonsecret,
    hermes_env_set_nonsecret,
    hermes_env_status,
)

# Preserve the import surface used by the server, diagnostics, and runner workers.
__all__ = (
    "_is_secret_env_name",
    "_read_env_value",
    "hermes_config_get",
    "hermes_config_patch",
    "hermes_config_set",
    "hermes_env_copy_nonsecret",
    "hermes_env_set_nonsecret",
    "hermes_env_status",
)

# Config get / set / patch
# ---------------------------------------------------------------------------


def _safe_config_view(cfg: dict[str, Any], key_path: str | None) -> Any:
    """Return a sanitized view of ``cfg``.

    Without a key_path: return a top-level summary that redacts any
    secret-looking key. With a key_path: walk the dotted path and return
    the value, redacting secret-looking leaves.
    """
    if key_path is None:
        return _redact_dict(cfg)
    parts = [p for p in key_path.split(".") if p]
    cur: Any = cfg
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return _redact_leaf(cur, ".".join(parts))


def _redact_dict(d: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 6:
        return {"_truncated": True}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _is_secret_key(str(k)):
            out[k] = "<redacted>" if v not in (None, "", [], {}) else v
            continue
        if isinstance(v, dict):
            out[k] = _redact_dict(v, depth + 1)
        elif isinstance(v, list):
            out[k] = [_redact_leaf(item, str(k)) for item in v[:20]]
        else:
            out[k] = _redact_leaf(v, str(k))
    return out


def _redact_leaf(value: Any, key: str) -> Any:
    if _is_secret_key(key) and value not in (None, "", [], {}):
        return "<redacted>"
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_leaf(item, key) for item in value[:20]]
    return value


def hermes_config_get(
    profile: str = "default",
    key_path: str | None = None,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        if not path.exists():
            return json.dumps(
                {"success": True, "profile": profile, "exists": False, "value": None},
                indent=2,
            )
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for config tools.") from exc
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        # If a specific key is requested and it's secret-like, refuse even
        # to return the redacted value — the caller does not need to know
        # whether a secret exists at that path. Return None instead.
        if key_path is not None and _is_secret_key(key_path):
            return json.dumps(
                {
                    "success": True,
                    "profile": profile,
                    "key_path": key_path,
                    "value": None,
                    "redacted": True,
                    "note": "Secret-looking key path; value not returned.",
                },
                indent=2,
            )
        view = _safe_config_view(cfg, key_path)
        return json.dumps(
            {
                "success": True,
                "profile": profile,
                "key_path": key_path,
                "value": view,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:  # noqa: BLE001 - Return structured failures from MCP tools.
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="config",
                code="CONFIG_READ_ERROR",
                suggested_action="Check that config.yaml exists, is readable, and is valid YAML.",
            ),
            indent=2,
        )


def _set_dotted(cfg: dict[str, Any], key_path: str, value: Any) -> None:
    parts = [p for p in key_path.split(".") if p]
    if not parts:
        raise ValueError("key_path is required.")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def hermes_config_set(
    profile: str = "default",
    key_path: str = "",
    value: Any = None,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        if not key_path:
            raise ValueError("key_path is required.")
        if _is_secret_key(key_path):
            raise PermissionError(
                f"key_path {key_path!r} looks secret-like. Operator config "
                "set refuses to write secret-like keys."
            )

        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for config tools.") from exc

        cfg: dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            if not isinstance(cfg, dict):
                cfg = {}

        # Coerce value: try to interpret as YAML scalar for parity with
        # `hermes config set`, but fall back to string.
        coerced: Any = value
        if isinstance(value, str):
            try:
                coerced = yaml.safe_load(value)
            except yaml.YAMLError:
                coerced = value

        before = _safe_config_view(cfg, key_path)
        new_cfg = json.loads(json.dumps(cfg, default=str))  # deep copy via json
        _set_dotted(new_cfg, key_path, coerced)
        after = _safe_config_view(new_cfg, key_path)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_set": True,
                "profile": profile,
                "key_path": key_path,
                "before": before,
                "after": after,
            }
            op.audit_record(
                tool="hermes_config_set",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                key=key_path,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(path)
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(new_cfg, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "key_path": key_path,
            "before": before,
            "after": after,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_config_set",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"set {key_path}",
            profile=profile,
            key=key_path,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - Return structured failures from MCP tools.
        op.audit_record(
            tool="hermes_config_set",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            key=key_path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="config",
                code="CONFIG_WRITE_ERROR",
                suggested_action="Check config.yaml permissions, key path, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_config_patch(
    profile: str = "default",
    old_string: str = "",
    new_string: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        if not path.exists():
            raise FileNotFoundError(f"config.yaml not found at {path}.")

        content = path.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise ValueError("old_string not found in config.yaml.")
        if content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more context."
            )
        if _is_secret_key(old_string) or _is_secret_assignment(old_string):
            raise PermissionError("old_string looks secret-like. Refusing.")
        if _is_secret_key(new_string) or _is_secret_assignment(new_string):
            raise PermissionError(
                "new_string looks like it sets a secret-like key. Refusing."
            )

        new_content = content.replace(old_string, new_string, 1)
        diff = op.unified_diff(content, new_content, label="config.yaml")
        if _is_secret_assignment(diff):
            raise PermissionError(
                "Generated diff contains secret-like key assignments. Refusing."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "profile": profile,
                "diff": diff,
            }
            op.audit_record(
                tool="hermes_config_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(path)
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp, path)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_config_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary="patched config.yaml",
            profile=profile,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - Return structured failures from MCP tools.
        op.audit_record(
            tool="hermes_config_patch",
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
                layer="config",
                code="CONFIG_PATCH_ERROR",
                suggested_action="Check config.yaml contents, patch strings, and operator level/apply mode.",
            ),
            indent=2,
        )


# Existing runner and server adapters import env tools from this module.
