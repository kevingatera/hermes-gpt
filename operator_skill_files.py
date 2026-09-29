"""Write supporting skill files and handle copy/delete lifecycle previews.

MCP entry points return failures in a JSON error object so transport clients
receive the same response shape for validation and runtime errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import operator_policy as op
import operator_skill_manager as skill_manager
from operator_skill_common import (
    _MAX_FILE_BYTES,
    _find_skill_dir,
    _hash_content,
    _is_secret_filename,
    _resolve_supporting_file,
    _skill_dir,
    _validate_skill_name,
)


def hermes_skill_write_file(
    profile: str = "default",
    name: str = "",
    file_path: str = "",
    file_content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not file_path:
            raise ValueError("file_path is required.")
        if file_content is None:
            raise ValueError("file_content is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}. "
                "Create it first with hermes_skill_create."
            )

        target = _resolve_supporting_file(skill_dir, file_path)

        content_bytes = len(file_content.encode("utf-8"))
        if content_bytes > _MAX_FILE_BYTES:
            raise ValueError(
                f"File content is {content_bytes:,} bytes (limit: {_MAX_FILE_BYTES:,})."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "profile": profile,
                "skill": canon,
                "file_path": file_path,
                "target": str(target),
                "content_len": len(file_content),
                "content_sha256": _hash_content(file_content)[1],
            }
            op.audit_record(
                tool="hermes_skill_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=file_content,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        result = skill_manager._call_skill_manager(
            "write_file",
            canon,
            hermes_root=hermes_root,
            profile_home=profile_home,
            file_path=file_path,
            file_content=file_content,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager write_file failed")),
                profile=profile,
                skill_name=canon,
                content=file_content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"wrote {file_path} in skill {canon}",
            profile=profile,
            skill_name=canon,
            content=file_content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_write_file",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_WRITE_FILE_ERROR",
                suggested_action="Check skill name, file_path, content size, and operator level/apply mode.",
            ),
            indent=2,
        )


def _copy_skill_tree(source_dir: Path, target_dir: Path) -> list[Path]:
    raise RuntimeError("Direct skill copy is disabled; use dry-run only.")


def hermes_skill_copy(
    source_profile: str,
    target_profile: str,
    name: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        canon = _validate_skill_name(name)
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_dir = _find_skill_dir(source_home, canon)
        if not source_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in source profile {source_profile!r}."
            )
        target_dir = _skill_dir(target_home, canon)

        # Enumerate files for dry-run preview.
        files: list[dict[str, Any]] = []
        total_size = 0
        for src_path in source_dir.rglob("*"):
            if src_path.is_dir():
                continue
            if _is_secret_filename(src_path.name):
                continue
            try:
                size = src_path.stat().st_size
            except OSError:
                size = 0
            total_size += size
            files.append(
                {
                    "rel": str(src_path.relative_to(source_dir)),
                    "size": size,
                }
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "skill": canon,
                "source_path": str(source_dir),
                "target_path": str(target_dir),
                "file_count": len(files),
                "total_size": total_size,
                "files": files[:50],
            }
            op.audit_record(
                tool="hermes_skill_copy",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                skill_name=canon,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        op.audit_record(
            tool="hermes_skill_copy",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=False,
            changed=False,
            error="Direct skill copy is not supported safely in v0.2.0; use dry-run or Owner Mode.",
            source_profile=source_profile,
            target_profile=target_profile,
            skill_name=canon,
        )
        return json.dumps(
            {
                "success": False,
                "error": "Direct skill copy is not supported safely in v0.2.0; use dry-run or Owner Mode.",
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_copy",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_COPY_ERROR",
                suggested_action="Check source/target profiles, skill name, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_sync_to_default(
    source_profile: str,
    name: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Convenience: copy a skill from source_profile to default."""
    return hermes_skill_copy(
        source_profile=source_profile,
        target_profile="default",
        name=name,
        dry_run=dry_run,
        hermes_root=hermes_root,
    )


def hermes_skill_delete(
    profile: str = "default",
    name: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )

        # List files for preview.
        files: list[str] = []
        for p in skill_dir.rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(skill_dir)))

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_delete": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_dir),
                "file_count": len(files),
                "files_preview": files[:20],
            }
            op.audit_record(
                tool="hermes_skill_delete",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        # Pin guard: best-effort check via Hermes skill_usage only for direct
        # mutations. Dry-run always succeeds without this check.
        # When the Hermes module is unavailable (e.g. CI), the check is
        # skipped rather than blocking -- _call_skill_manager handles the
        # no-Hermes-agent case directly.
        try:
            import sys as _sys

            if str(hermes_root) not in _sys.path:
                _sys.path.insert(0, str(hermes_root))
            from tools import skill_usage  # type: ignore

            rec = skill_usage.get_record(canon)
            if rec.get("pinned"):
                raise PermissionError(
                    f"Skill {canon!r} is pinned and cannot be deleted. "
                    "Ask the user to run `hermes curator unpin " + canon + "`."
                )
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001, S110 - unavailable optional metadata keeps the legacy fallback
            # Hermes skill_usage module is not available. Proceed with
            # the skill-manager fallback, which handles deletion without
            # Hermes metadata.
            pass

        result = skill_manager._call_skill_manager(
            "delete",
            canon,
            hermes_root=hermes_root,
            profile_home=profile_home,
            absorbed_into="",
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_delete",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager delete failed")),
                profile=profile,
                skill_name=canon,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_delete",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"deleted skill {canon}",
            profile=profile,
            skill_name=canon,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_delete",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_DELETE_ERROR",
                suggested_action="Check skill name, pin status, skill manager availability, and operator level/apply mode.",
            ),
            indent=2,
        )
