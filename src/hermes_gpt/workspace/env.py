"""Read and update non-secret environment entries in Hermes profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_gpt.policy import authorization as op
from hermes_gpt.workspace.config_common import _ENV_NAME_DENYLIST, _ENV_NAME_RE, _backup_file, _env_path, _is_secret_env_name

# Env status / set / copy
# ---------------------------------------------------------------------------


def _read_env_keys(env_path: Path) -> set[str]:
    """Return the set of keys defined in a .env file. Never returns values."""
    keys: set[str] = set()
    if not env_path.exists():
        return keys
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key:
                    keys.add(key)
    except OSError:
        return set()
    return keys


def hermes_env_status(
    profile: str = "default",
    keys: list[str] | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Return set/unset status for env keys. Never returns values."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        env_path = _env_path(profile_home)
        existing = _read_env_keys(env_path)

        if not keys:
            # Return summary of all keys (names only).
            summary = [
                {"key": k, "set": True, "secret_like": _is_secret_env_name(k)}
                for k in sorted(existing)
            ]
            return json.dumps(
                {
                    "success": True,
                    "profile": profile,
                    "env_exists": env_path.exists(),
                    "keys": summary,
                },
                indent=2,
            )

        results = []
        for k in keys:
            if not _ENV_NAME_RE.match(k):
                results.append(
                    {"key": k, "set": False, "secret_like": True, "invalid_name": True}
                )
                continue
            results.append(
                {
                    "key": k,
                    "set": k in existing,
                    "secret_like": _is_secret_env_name(k),
                }
            )
        return json.dumps(
            {"success": True, "profile": profile, "keys": results}, indent=2
        )
    except Exception as exc:  # noqa: BLE001 - Keep read failures in the tool's JSON response.
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_READ_ERROR",
                suggested_action="Check .env file permissions and profile name.",
            ),
            indent=2,
        )


def _validate_env_key_for_write(key: str) -> None:
    if not key:
        raise ValueError("key is required.")
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"Invalid env var name {key!r}.")
    if _is_secret_env_name(key):
        raise PermissionError(
            f"Refusing to set secret-looking env key {key!r}. "
            "Operator env tools only write non-secret keys."
        )
    if key in _ENV_NAME_DENYLIST:
        raise PermissionError(
            f"Refusing to set denylisted env var {key!r}. This name influences "
            "subprocess execution or Hermes runtime location."
        )


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    """Write ``key=value`` to ``env_path``, preserving comments and existing lines.

    Replaces the existing line if the key is already present; otherwise
    appends. The value is written verbatim (no shell escaping) — the .env
    format is plain KEY=VALUE.
    """
    lines: list[str] = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    new_line = f"{key}={value}\n"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            if not replaced:
                out.append(new_line)
                replaced = True
            # Skip duplicate original lines for the same key.
            continue
        out.append(line)
    if not replaced:
        # Ensure there's a blank line between existing content and the new
        # key when the file isn't empty and doesn't already end with one.
        if out and out[-1].strip() != "":
            out.append("\n")
        out.append(new_line)
    tmp = env_path.with_suffix(".env.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.replace(tmp, env_path)


def hermes_env_set_nonsecret(
    profile: str = "default",
    key: str = "",
    value: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        _validate_env_key_for_write(key)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        env_path = _env_path(profile_home)
        existing_keys = _read_env_keys(env_path)
        already_set = key in existing_keys

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_set": True,
                "profile": profile,
                "key": key,
                "target": str(env_path),
                "already_set": already_set,
                "value_len": len(value) if value else 0,
                # Do NOT include the value itself.
            }
            op.audit_record(
                tool="hermes_env_set_nonsecret",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                key=key,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(env_path)
        _write_env_key(env_path, key, value)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "key": key,
            "already_set": already_set,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_env_set_nonsecret",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"set env key {key}",
            profile=profile,
            key=key,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - Preserve the tool audit/error response for failures.
        op.audit_record(
            tool="hermes_env_set_nonsecret",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            key=key,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_WRITE_ERROR",
                suggested_action="Check .env permissions, key name, and operator level/apply mode.",
            ),
            indent=2,
        )


def _read_env_value(env_path: Path, key: str) -> str | None:
    """Return the raw value for ``key`` from ``env_path``.

    Internal helper. Never expose the returned value to tool output — used
    only by env_copy_nonsecret to copy a value across profiles without
    printing it.
    """
    if not env_path.exists():
        return None
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            existing_key, _, existing_value = stripped.partition("=")
            if existing_key.strip() == key:
                return existing_value.strip()
    return None


def hermes_env_copy_nonsecret(
    source_profile: str,
    target_profile: str,
    key: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        _validate_env_key_for_write(key)
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_env = _env_path(source_home)
        target_env = _env_path(target_home)

        value = _read_env_value(source_env, key)
        if value is None:
            raise FileNotFoundError(
                f"Key {key!r} not set in source profile {source_profile!r}."
            )

        target_existing = _read_env_keys(target_env)
        already_set = key in target_existing

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "key": key,
                "target": str(target_env),
                "already_set_in_target": already_set,
                "value_len": len(value),
                # Do NOT include the value itself.
            }
            op.audit_record(
                tool="hermes_env_copy_nonsecret",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                key=key,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(target_env)
        _write_env_key(target_env, key, value)
        result = {
            "success": True,
            "dry_run": False,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "key": key,
            "already_set_in_target": already_set,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_env_copy_nonsecret",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"copied env key {key}",
            source_profile=source_profile,
            target_profile=target_profile,
            key=key,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - Preserve the tool audit/error response for failures.
        op.audit_record(
            tool="hermes_env_copy_nonsecret",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            key=key,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_COPY_ERROR",
                suggested_action="Check source/target profiles, key name, and operator level/apply mode.",
            ),
            indent=2,
        )
