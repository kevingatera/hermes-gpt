"""Read-only Git status and diff operations, constrained by allowed paths."""

from __future__ import annotations

# Tool handlers return structured errors instead of leaking failures over MCP.
# ruff: noqa: BLE001
import json

import operator_policy as op

# Git
# ---------------------------------------------------------------------------


def _git(argv: list[str], workdir: str, runner=None) -> tuple[int, str, str]:
    run_fn = runner or op.run_argv
    return run_fn(["git", *argv], timeout=60, workdir=workdir)


def hermes_git_status(workdir: str, runner=None) -> str:
    try:
        policy = op.OperatorPolicy()
        if not workdir:
            raise ValueError("workdir is required.")
        # If allowed_paths is set, workdir must be under one. Otherwise allow
        # any workdir (read-only git status is safe and the existing terminal
        # tool is also unguarded when enabled).
        if policy.allowed_paths and not op.path_under_allowed(
            workdir, policy.allowed_paths
        ):
            raise PermissionError(
                f"workdir {workdir!r} is not under any allowed path."
            )
        rc, out, err = _git(["status", "--porcelain=v1"], workdir, runner=runner)
        result = {
            "success": rc == 0,
            "workdir": workdir,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="GIT_STATUS_ERROR",
                suggested_action="Check workdir, allowed_paths, and git availability.",
            ),
            indent=2,
        )


def hermes_git_diff(
    workdir: str,
    pathspec: str | None = None,
    stat: bool = False,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        if not workdir:
            raise ValueError("workdir is required.")
        if policy.allowed_paths and not op.path_under_allowed(
            workdir, policy.allowed_paths
        ):
            raise PermissionError(
                f"workdir {workdir!r} is not under any allowed path."
            )
        argv: list[str] = ["diff"]
        if stat:
            argv.append("--stat")
        if pathspec:
            argv.append("--")
            argv.append(pathspec)
        rc, out, err = _git(argv, workdir, runner=runner)
        result = {
            "success": rc == 0,
            "workdir": workdir,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="GIT_DIFF_ERROR",
                suggested_action=(
                    "Check workdir, allowed_paths, pathspec, and git availability."
                ),
            ),
            indent=2,
        )
