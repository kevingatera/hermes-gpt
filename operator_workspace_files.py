"""Scoped workspace file operations and the bounded test command runner.

File reads and writes use the operator path policy. Writes keep the dry-run
and direct-apply gates, and test commands must match the local allowlist.
"""

from __future__ import annotations

# Tool handlers return structured errors instead of leaking failures over MCP.
# ruff: noqa: BLE001
import json

import operator_policy as op
from operator_command_utils import _split_command_argv
from operator_file_utils import _atomic_write_text, _backup_file

# Workspace read / patch / write_file / run_test
# ---------------------------------------------------------------------------


def hermes_workspace_read(
    path: str,
    offset: int = 1,
    limit: int = 500,
) -> str:
    """Read a file. Read-only but applies operator path policy (deny secrets)."""
    try:
        policy = op.OperatorPolicy()
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy."
            )
        # If allowed_paths is set, require the path to be under one of them.
        # If allowed_paths is empty, allow reads anywhere that's not denied
        # (read-only mode is the default and the existing hermes_read_file
        # tool already exists).
        if policy.allowed_paths and not op.path_under_allowed(
            path, policy.allowed_paths
        ):
            raise PermissionError(
                f"Path {path!r} is not under any allowed path in "
                f"{op.OPERATOR_ALLOWED_PATHS_ENV}."
            )
        p = op._normalize_path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        start = max(1, int(offset)) - 1
        end = start + max(1, int(limit))
        chunk = "".join(lines[start:end])
        result = {
            "success": True,
            "path": str(p),
            "offset": start + 1,
            "limit": end - start,
            "total_lines": len(lines),
            "content": chunk,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_READ_ERROR",
                suggested_action=(
                    "Check path, allowed_paths, denied-path policy, and that "
                    "the file exists."
                ),
            ),
            indent=2,
        )


def hermes_workspace_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(path)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        p = op._normalize_path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise ValueError("old_string not found in file.")
        if not replace_all and content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more context "
                "or set replace_all=True."
            )
        if replace_all:
            new_content = content.replace(old_string, new_string)
            match_count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            match_count = 1

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "path": str(p),
                "match_count": match_count,
                "diff": op.unified_diff(content, new_content, label=p.name),
            }
            op.audit_record(
                tool="hermes_workspace_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(p)
        _atomic_write_text(p, new_content)
        result = {
            "success": True,
            "dry_run": False,
            "path": str(p),
            "match_count": match_count,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_workspace_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"patched {p.name} ({match_count} replacement(s))",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_patch",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_PATCH_ERROR",
                suggested_action=(
                    "Check path, allowed_paths, old_string/new_string, and "
                    "operator level/apply mode."
                ),
            ),
            indent=2,
        )


def hermes_workspace_write_file(
    path: str,
    content: str,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(path)
        if content is None:
            raise ValueError("content is required.")

        p = op._normalize_path(path)
        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "path": str(p),
                "exists": p.exists(),
                "content_len": len(content),
            }
            op.audit_record(
                tool="hermes_workspace_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        backup = _backup_file(p) if p.exists() else None
        _atomic_write_text(p, content)
        result = {
            "success": True,
            "dry_run": False,
            "path": str(p),
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_workspace_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"wrote {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_write_file",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_WRITE_ERROR",
                suggested_action=(
                    "Check path, allowed_paths, denied-path policy, and "
                    "operator level/apply mode."
                ),
            ),
            indent=2,
        )


# Allowlist for run_test. Each entry is a tuple of (argv_prefix, max_args).
# The prefix must match exactly; the rest is bounded.
_TEST_COMMAND_ALLOWLIST: tuple[tuple[tuple[str, ...], int], ...] = (
    (("pytest",), 8),
    (("python", "-m", "pytest"), 8),
    (("npm", "test"), 4),
    (("npm", "run", "test"), 4),
    (("npm", "run", "lint"), 4),
    (("ruff", "check"), 4),
    (("mypy",), 4),
    (("git", "status"), 4),
    (("git", "diff"), 4),
)

# Substrings that mark a command as dangerous and must be refused.
_DANGEROUS_PATTERNS: tuple[str, ...] = (
    "rm ",
    "del ",
    "format ",
    "powershell",
    "curl ",
    "wget ",
    "bash -c",
    "cmd /c",
    "git add",
    "git commit",
    "git push",
    "|",
    ">",
    "<",
    ";",
    "&",
    "EncodedCommand",
    "||",
    "&&",
    "`",
    "$(",
)


def _is_allowed_test_command(argv: list[str]) -> tuple[bool, str]:
    """Check whether argv matches the test/lint allowlist."""
    if not argv:
        return (False, "Empty command.")
    cmd = " ".join(argv)
    # Reject dangerous substrings first.
    for needle in _DANGEROUS_PATTERNS:
        if needle in cmd:
            return (False, f"Command contains forbidden substring {needle!r}.")
    for prefix, max_extra in _TEST_COMMAND_ALLOWLIST:
        if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
            extra = argv[len(prefix) :]
            if len(extra) > max_extra:
                return (False, f"Too many arguments for {prefix!r}.")
            return (True, "")
    return (False, "Command not in the test/lint allowlist.")


def hermes_workspace_run_test(
    command: str,
    workdir: str | None = None,
    timeout: int = 120,
    dry_run: bool = True,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        if not command or not command.strip():
            raise ValueError("command is required.")

        # Parse with shlex so we never invoke a shell.
        try:
            argv = _split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from exc

        allowed, reason = _is_allowed_test_command(argv)
        if not allowed:
            raise PermissionError(reason)

        # A configured workspace root constrains the test workdir.
        if workdir and policy.allowed_paths and not op.path_under_allowed(
            workdir, policy.allowed_paths
        ):
            raise PermissionError(
                f"workdir {workdir!r} is not under any allowed path."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": argv,
                "shell": False,
                "workdir": workdir,
                "timeout": max(1, min(int(timeout), 600)),
            }
            op.audit_record(
                tool="hermes_workspace_run_test",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=workdir or "",
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=timeout, workdir=workdir)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "argv": argv,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        op.audit_record(
            tool="hermes_workspace_run_test",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=False,  # test runs are not file mutations
            summary=f"rc={rc} argv={argv}",
            path=workdir or "",
            error=op.redact_output(err) if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_run_test",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_RUN_TEST_ERROR",
                suggested_action=(
                    "Check command allowlist, workdir, and operator "
                    "level/apply mode."
                ),
            ),
            indent=2,
        )
