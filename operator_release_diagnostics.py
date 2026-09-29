"""Repository and package readiness checks for Hermes GPT releases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_diagnostics_repo import _repo_status
from versioning import VERSION


def _find_secret_files(workdir: Path) -> list[str]:
    """Return relative paths of secret-looking files under workdir."""
    secret_names = op.DEFAULT_DENIED_BASENAMES | op.DEFAULT_DENIED_DIR_NAMES
    secret_substrings = op.SECRET_PATH_SUBSTRINGS
    allowed_source_paths = {
        Path("web/src/shared/tokens.ts"),
        Path("web/src/shared/tokens.css"),
    }
    found: list[str] = []
    for root, dirs, files in os.walk(workdir):
        root_path = Path(root)
        # Skip git internals, caches, and local virtualenvs (dev artifacts
        # that are never shipped; third-party packages inside them can contain
        # secret-like filenames, e.g. keyring/credentials.py).
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".worktrees",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
                ".venv",
                "venv",
                ".tox",
                ".nox",
            }
        ]
        for name in files:
            lower = name.lower()
            if name in secret_names or lower.startswith(".env.") or name == ".env":
                found.append(str(root_path / name))
                continue
            candidate = root_path / name
            try:
                rel_candidate = candidate.relative_to(workdir)
            except ValueError:
                rel_candidate = None
            # The substring heuristic targets secret-bearing data files
            # (tokens.json, oauth-store.json, credentials.yaml, ...). Python
            # source and markdown docs are legitimate even when their names
            # contain "oauth"/"token"/"secret". The two known web design-token
            # source assets are separately allowlisted by exact repository path;
            # similarly named files elsewhere remain subject to the scan.
            if lower.endswith((".py", ".md")) or rel_candidate in allowed_source_paths:
                continue
            for sub in secret_substrings:
                if sub in lower:
                    found.append(str(root_path / name))
                    break
        for name in dirs:
            if name in secret_names:
                found.append(str(root_path / name))
    # Return paths relative to workdir for readability.
    rels: list[str] = []
    for p in found:
        try:
            rels.append(str(Path(p).relative_to(workdir)))
        except ValueError:
            rels.append(str(p))
    return sorted(set(rels))


def _pyproject_version(workdir: Path) -> str | None:
    path = workdir / "pyproject.toml"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("version"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _file_contains(workdir: Path, filename: str, needle: str) -> bool:
    path = workdir / filename
    if not path.exists():
        return False
    try:
        return needle in path.read_text(encoding="utf-8")
    except Exception:
        return False


def _previous_git_tag(workdir: Path) -> str | None:
    try:
        rc, out, _ = op.run_argv(
            ["git", "describe", "--tags", "--abbrev=0"],
            timeout=30,
            workdir=str(workdir),
        )
        if rc == 0:
            return out.strip()
    except Exception:
        pass
    return None


def hermes_release_doctor(
    workdir: str | None = None,
    full_tests: bool = False,
    timeout: int = 180,
    runner=None,
) -> str:
    """Check whether the repo/operator is safe to ship at the current version."""
    trace_id = op.new_trace_id()
    try:
        wd = (
            Path(workdir).expanduser().resolve()
            if workdir
            else Path(__file__).resolve().parent
        )
        blocking: list[str] = []
        warnings: list[str] = []

        # Secret-file scan.
        secret_files = _find_secret_files(wd)
        if secret_files:
            blocking.append(
                f"Secret-like files found in tree: {', '.join(secret_files[:10])}"
            )

        # Git repo / branch / dirty tree.
        repo = _repo_status(wd)
        if not repo["is_git_repo"]:
            warnings.append("Working directory is not a git repo.")
        if repo["branch"] is None:
            warnings.append("Could not detect git branch.")
        if repo["clean"] is False:
            warnings.append("Working tree has uncommitted changes.")

        # pyproject.toml version.
        version = _pyproject_version(wd)
        if version is None:
            blocking.append("Could not read version from pyproject.toml.")
        elif version != VERSION:
            warnings.append(
                f"pyproject.toml version is {version!r}; expected {VERSION!r}."
            )

        # CHANGELOG mentions version.
        if not _file_contains(wd, "CHANGELOG.md", VERSION):
            warnings.append(f"CHANGELOG.md does not mention {VERSION}.")

        # README/docs mention reliability tools.
        if not _file_contains(wd, "README.md", "hermes_operator_doctor"):
            warnings.append("README.md does not mention the new diagnostic tools.")
        if not _file_contains(wd, "docs/operator-mode.md", "hermes_operator_recover"):
            warnings.append(
                "docs/operator-mode.md does not mention hermes_operator_recover."
            )

        # Import / py_compile check.
        try:
            import server  # noqa: F401
        except Exception as exc:
            blocking.append(f"server.py cannot be imported: {exc.__class__.__name__}")

        try:
            op.run_argv(
                [sys.executable, "-m", "py_compile", "server.py"],
                timeout=60,
                workdir=str(wd),
            )
        except Exception as exc:
            blocking.append(f"py_compile server.py failed: {exc.__class__.__name__}")

        # Operator apply mode.
        apply_mode = (
            os.environ.get(op.OPERATOR_APPLY_MODE_ENV, "dry_run").strip().lower()
        )
        if apply_mode == "direct":
            warnings.append(
                f"{op.OPERATOR_APPLY_MODE_ENV}=direct is set; releases should ship dry-run by default."
            )

        # Version vs previous tag.
        prev_tag = _previous_git_tag(wd)
        if prev_tag and version and prev_tag.endswith(version):
            warnings.append(
                f"Version {version} matches the previous tag {prev_tag}; consider bumping."
            )

        # Optional full test run.
        test_result: dict[str, Any] | None = None
        if full_tests:
            run_fn = runner or op.run_argv
            try:
                rc, out, err = run_fn(
                    [sys.executable, "-m", "pytest", "-q"],
                    timeout=max(60, int(timeout)),
                    workdir=str(wd),
                )
                test_result = {"returncode": rc, "success": rc == 0}
                if rc != 0:
                    blocking.append("pytest suite failed.")
            except subprocess.TimeoutExpired:
                blocking.append(f"pytest timed out after {timeout}s.")
                test_result = {"returncode": None, "success": False, "error": "timeout"}
            except Exception as exc:
                blocking.append(f"pytest could not run: {exc.__class__.__name__}")
                test_result = {
                    "returncode": None,
                    "success": False,
                    "error": exc.__class__.__name__,
                }

        if blocking:
            status = "BLOCKED"
            recommended = "Fix blocking issues before tagging a release."
        elif warnings:
            status = "WARN"
            recommended = (
                "Review warnings, then run with full_tests=true before tagging."
            )
        else:
            status = "PASS"
            recommended = f"Ready to tag v{VERSION}."

        return json.dumps(
            {
                "success": True,
                "status": status,
                "blocking_issues": blocking,
                "non_blocking_issues": warnings,
                "recommended_release_type": "minor",
                "recommended_next_action": recommended,
                "version": version,
                "repo_status": repo,
                "full_tests": full_tests,
                "test_result": test_result,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="release",
            code="RELEASE_DOCTOR_INTERNAL_ERROR",
            suggested_action="Run hermes_release_doctor again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)
