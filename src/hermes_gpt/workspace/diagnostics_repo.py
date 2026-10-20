"""Git status probe shared by snapshots and release checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op


def _repo_status(workdir: Path) -> dict[str, Any]:
    """Best-effort git repo status. No secrets."""
    status: dict[str, Any] = {
        "is_git_repo": False,
        "clean": None,
        "branch": None,
    }
    git_dir = workdir / ".git"
    if not git_dir.exists():
        return status
    status["is_git_repo"] = True
    try:
        rc, out, err = op.run_argv(
            ["git", "status", "--porcelain=v1"], timeout=30, workdir=str(workdir)
        )
        status["clean"] = rc == 0 and not out.strip()
    except Exception:
        status["clean"] = None
    try:
        rc, out, _ = op.run_argv(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=30,
            workdir=str(workdir),
        )
        if rc == 0:
            status["branch"] = out.strip()
    except Exception:
        status["branch"] = None
    return status
