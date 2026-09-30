"""Read and edit skill instructions while preserving Operator gates and audit.

MCP entry points return failures in a JSON error object so transport clients
receive the same response shape for validation and runtime errors.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_gpt.policy import authorization as op
from hermes_gpt.skills import manager as skill_manager
from hermes_gpt.skills.common import _MAX_CONTENT_CHARS, _find_skill_dir, _hash_content, _resolve_supporting_file, _skill_dir, _validate_frontmatter, _validate_skill_name


def hermes_skill_diff(
    profile: str = "default",
    name: str = "",
    proposed_content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    file_path: str = "SKILL.md",
    hermes_root: Path | None = None,
) -> str:
    """Preview a diff for a skill. Read-only — never mutates."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )

        target = _resolve_supporting_file(skill_dir, file_path)
        if not target.exists():
            raise FileNotFoundError(f"File {file_path!r} not found in skill {canon!r}.")

        current = target.read_text(encoding="utf-8", errors="replace")

        if proposed_content is not None:
            new = proposed_content
        elif old_string is not None and new_string is not None:
            if old_string not in current:
                raise ValueError("old_string not found in current content.")
            new = current.replace(old_string, new_string, 1)
        else:
            raise ValueError(
                "Provide either proposed_content or (old_string + new_string)."
            )

        diff = op.unified_diff(current, new, label=file_path)
        cur_len, cur_sha = _hash_content(current)
        new_len, new_sha = _hash_content(new)
        result = {
            "success": True,
            "profile": profile,
            "skill": canon,
            "file_path": file_path,
            "current_len": cur_len,
            "current_sha256": cur_sha,
            "proposed_len": new_len,
            "proposed_sha256": new_sha,
            "diff": diff,
        }
        op.audit_record(
            tool="hermes_skill_diff",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=True,
            changed=False,
            summary="diff preview",
            profile=profile,
            skill_name=canon,
            content=new,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_DIFF_ERROR",
                suggested_action="Check skill name, file_path, and that the skill exists.",
            ),
            indent=2,
        )


def hermes_skill_create(
    profile: str = "default",
    name: str = "",
    content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not content:
            raise ValueError("content is required for create.")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Content is {len(content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )
        _validate_frontmatter(content)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        existing = _find_skill_dir(profile_home, canon)
        if existing:
            raise ValueError(f"A skill named {canon!r} already exists at {existing}.")
        skill_dir = _skill_dir(profile_home, canon)
        skill_md = skill_dir / "SKILL.md"

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_create": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_md),
                "content_len": len(content),
                "content_sha256": _hash_content(content)[1],
                "diff": op.unified_diff("", content, label="SKILL.md"),
            }
            op.audit_record(
                tool="hermes_skill_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        result = skill_manager._call_skill_manager(
            "create",
            canon,
            hermes_root=hermes_root,
            profile_home=profile_home,
            content=content,
            category=None,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager create failed")),
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_create",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"created skill at {skill_md}",
            profile=profile,
            skill_name=canon,
            content=content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_create",
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
                code="SKILL_CREATE_ERROR",
                suggested_action="Check skill name, frontmatter, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_edit(
    profile: str = "default",
    name: str = "",
    content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not content:
            raise ValueError("content is required for edit.")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Content is {len(content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )
        _validate_frontmatter(content)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )
        skill_md = skill_dir / "SKILL.md"
        current = (
            skill_md.read_text(encoding="utf-8", errors="replace")
            if skill_md.exists()
            else ""
        )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_edit": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_md),
                "current_len": len(current),
                "proposed_len": len(content),
                "diff": op.unified_diff(current, content, label="SKILL.md"),
            }
            op.audit_record(
                tool="hermes_skill_edit",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        result = skill_manager._call_skill_manager(
            "edit",
            canon,
            hermes_root=hermes_root,
            profile_home=profile_home,
            content=content,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_edit",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager edit failed")),
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_edit",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"edited skill at {skill_md}",
            profile=profile,
            skill_name=canon,
            content=content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_edit",
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
                code="SKILL_EDIT_ERROR",
                suggested_action="Check skill name, frontmatter, skill existence, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_patch(
    profile: str = "default",
    name: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "SKILL.md",
    replace_all: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not old_string:
            raise ValueError("old_string is required for patch.")
        if new_string is None:
            raise ValueError("new_string is required for patch.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )
        target = _resolve_supporting_file(skill_dir, file_path)
        if not target.exists():
            raise FileNotFoundError(f"File {file_path!r} not found in skill {canon!r}.")

        content = target.read_text(encoding="utf-8", errors="replace")

        if old_string not in content:
            raise ValueError(
                "old_string not found in current content. Use hermes_skill_diff "
                "to preview or check the file path."
            )
        if not replace_all and content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more surrounding "
                "context for a unique match, or set replace_all=True."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            match_count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            match_count = 1

        if len(new_content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Patched content is {len(new_content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "profile": profile,
                "skill": canon,
                "file_path": file_path,
                "match_count": match_count,
                "diff": op.unified_diff(content, new_content, label=file_path),
            }
            op.audit_record(
                tool="hermes_skill_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=new_content,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        result = skill_manager._call_skill_manager(
            "patch",
            canon,
            hermes_root=hermes_root,
            profile_home=profile_home,
            old_string=old_string,
            new_string=new_string,
            file_path=file_path,
            replace_all=replace_all,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager patch failed")),
                profile=profile,
                skill_name=canon,
                content=new_content,
            )
            return json.dumps(result, indent=2)
        result.setdefault("match_count", match_count)
        op.audit_record(
            tool="hermes_skill_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"patched {file_path} ({match_count} replacement(s))",
            profile=profile,
            skill_name=canon,
            content=new_content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:  # noqa: BLE001 - keep failures inside the MCP JSON contract
        op.audit_record(
            tool="hermes_skill_patch",
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
                code="SKILL_PATCH_ERROR",
                suggested_action="Check skill name, file_path, old_string/new_string, and operator level/apply mode.",
            ),
            indent=2,
        )
