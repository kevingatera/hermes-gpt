"""Acknowledged Owner-mode command and file operations.

Mutations require the Owner acknowledgement and direct apply mode. Commands
run as argv without a shell, and file tools continue to deny secret paths.
"""

from __future__ import annotations

# Tool handlers return structured errors instead of leaking failures over MCP.
# ruff: noqa: BLE001
import json
import os
import re
from pathlib import Path

from hermes_gpt.policy import authorization as op
from hermes_gpt.workspace.commands import _split_command_argv
from hermes_gpt.workspace.file_utils import _atomic_write_text, _backup_file

# Owner Mode
# ---------------------------------------------------------------------------

# Catastrophic command patterns that even Owner Mode refuses.
_CATASTROPHIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\s+/(\s|$)"),
    re.compile(r"\brm\s+-rf\s+/\*"),
    re.compile(r"(?i)\bdel\s+/s\b"),
    re.compile(r"(?i)\bformat\b"),
    re.compile(r"(?i)powershell.*-EncodedCommand"),
    re.compile(r"(?i)\b(curl|wget)\b[^|]*\|\s*(bash|sh)"),
    re.compile(r"(?i)\bgit\s+push\b.*--force"),
    re.compile(r"(?i)\bgit\s+push\b.*\s-f\b"),
    re.compile(r"(?i)\bgit\s+add\s+-A\b"),
    re.compile(r"(?i)\bgit\s+add\s+\.\s*$"),
)


def _command_touches_secrets(command: str) -> bool:
    """Heuristic: does the command mention secret/vault/token/.env/ssh paths?"""
    lower = command.lower()
    needles = (
        ".env",
        "vault",
        "mcp-tokens",
        "auth.json",
        ".ssh",
        "id_rsa",
        "id_ed25519",
        "authorized_keys",
        ".aws",
        ".gnupg",
        ".kube",
        "webhook_subscriptions.json",
        "oauth",
        "token",
        "secret",
        "credential",
        "cookie",
        "password",
    )
    return any(n in lower for n in needles)


def _deferred_self_restart_argv(argv: list[str]) -> list[str] | None:
    """Return a delayed systemd command for an exact Hermes GPT self-restart.

    Running ``systemctl --user restart hermes-gpt-server.service`` synchronously
    from the server kills the process that is waiting on ``systemctl``. The
    operator then records rc=-15 even though systemd successfully restarts the
    service. Schedule only this exact self-restart a few seconds later so the
    MCP response and audit record can complete first.
    """
    if os.name == "nt" or len(argv) != 4:
        return None
    if Path(argv[0]).name != "systemctl":
        return None
    if argv[1:] != ["--user", "restart", "hermes-gpt-server.service"]:
        return None
    return [
        "systemd-run",
        "--user",
        "--on-active=3s",
        "--collect",
        "systemctl",
        "--user",
        "restart",
        "hermes-gpt-server.service",
    ]


def hermes_owner_run_command(
    command: str,
    timeout: int = 120,
    workdir: str | None = None,
    dry_run: bool = True,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if not command or not command.strip():
            raise ValueError("command is required.")

        # Catastrophic pattern block.
        for pattern in _CATASTROPHIC_PATTERNS:
            if pattern.search(command):
                raise PermissionError(
                    f"Command blocked by catastrophic-pattern guard: {command!r}"
                )
        if _command_touches_secrets(command):
            raise PermissionError(
                "Command touches secret-like paths (.env / vault / token / ssh). "
                "Owner Mode does not permit secret access. Edit the file directly "
                "on a trusted shell."
            )

        try:
            argv = _split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from exc
        if not argv:
            raise ValueError("Empty command after parse.")

        deferred_restart_argv = _deferred_self_restart_argv(argv)
        effective_argv = deferred_restart_argv or argv

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": effective_argv,
                "requested_argv": argv if deferred_restart_argv else None,
                "deferred_self_restart": bool(deferred_restart_argv),
                "shell": False,
                "workdir": workdir,
                "timeout": max(1, min(int(timeout), 600)),
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_run_command",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                extra={"argv": argv, "workdir": workdir or ""},
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(effective_argv, timeout=timeout, workdir=workdir)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "owner_mode": True,
            "returncode": rc,
            "argv": argv,
            "executed_argv": effective_argv if deferred_restart_argv else argv,
            "deferred_self_restart": bool(deferred_restart_argv),
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        op.audit_record(
            tool="hermes_owner_run_command",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=(
                f"rc={rc} argv={argv} deferred_self_restart=True"
                if deferred_restart_argv
                else f"rc={rc} argv={argv}"
            ),
            error=op.redact_output(err) if rc != 0 else "",
            extra={
                "workdir": workdir or "",
                "executed_argv": effective_argv,
                "deferred_self_restart": bool(deferred_restart_argv),
            },
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_run_command",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_RUN_COMMAND_ERROR",
                suggested_action=(
                    "Check owner acknowledgement, command safety, and operator "
                    "level/apply mode."
                ),
            ),
            indent=2,
        )


def hermes_owner_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        # Owner mode still denies secret paths.
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env). Owner Mode "
                "does not override this in the current release."
            )

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
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_patch",
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
            "owner_mode": True,
            "path": str(p),
            "match_count": match_count,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_owner_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"owner patched {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_patch",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_PATCH_ERROR",
                suggested_action=(
                    "Check owner acknowledgement, path, old_string/new_string, "
                    "and operator level/apply mode."
                ),
            ),
            indent=2,
        )


def hermes_owner_write_file(
    path: str,
    content: str,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if content is None:
            raise ValueError("content is required.")
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env). Owner Mode "
                "does not override this in the current release."
            )

        p = op._normalize_path(path)
        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "path": str(p),
                "exists": p.exists(),
                "content_len": len(content),
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_write_file",
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
            "owner_mode": True,
            "path": str(p),
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_owner_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"owner wrote {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_write_file",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_WRITE_ERROR",
                suggested_action=(
                    "Check owner acknowledgement, path, denied-path policy, "
                    "and operator level/apply mode."
                ),
            ),
            indent=2,
        )
