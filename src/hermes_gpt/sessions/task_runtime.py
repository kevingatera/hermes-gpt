"""Prepare and launch one confined turn for a managed Hermes task."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.browser import session as browser
from hermes_gpt.policy import authorization as op
from hermes_gpt.sessions import session as sessions
from hermes_gpt.sessions import job_store
from hermes_gpt.sessions import jobs as job_runtime
from hermes_gpt.sessions import task_mcp
from hermes_gpt.sessions import task_mounts
from hermes_gpt.sessions import task_profile
from hermes_gpt.policy import confinement
from hermes_gpt.sessions.model_options import ModelOverrides

TOOLSETS = "profile-configured+task-browser"
PROFILE_DEFAULT_TOOLSETS = "profile-configured"
MAX_TIMEOUT = 3600
_BROWSER_BRIDGE_MODULES = (
    "hermes_gpt",
    "hermes_gpt.browser",
    "hermes_gpt.policy",
    "hermes_gpt.paths",
    "hermes_gpt.browser.bridge",
    "hermes_gpt.browser.launcher",
    "hermes_gpt.mcp_compat",
    "hermes_gpt.browser.session",
    "hermes_gpt.browser.profiles",
    "hermes_gpt.browser.state",
    "hermes_gpt.browser.tabs",
    "hermes_gpt.policy.authorization",
    "hermes_gpt.policy.audit",
    "hermes_gpt.policy.paths",
    "hermes_gpt.policy.subprocess",
    "hermes_gpt.policy.redaction",
)


def _source_root(executable: str, agent_root: Path | None) -> Path:
    """Find the Hermes source tree that the confined CLI needs to import."""
    candidates: list[Path] = []
    if agent_root is not None:
        candidates.append(Path(agent_root).expanduser())
    configured = os.environ.get("HERMES_GPT_HERMES_SOURCE_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    try:
        resolved = Path(executable).expanduser().resolve()
        candidates.extend(resolved.parents)
    except OSError:
        pass
    for candidate in candidates:
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            continue
        if (root / "hermes_cli" / "main.py").is_file() and (root / "agent").is_dir():
            return root

    install_root = _installed_source_root(executable)
    if install_root is not None:
        try:
            root = install_root.resolve(strict=True)
        except OSError:
            root = None
        if root and (root / "hermes_cli" / "main.py").is_file() and (root / "agent").is_dir():
            return root
    raise FileNotFoundError("Hermes Agent source root could not be resolved for confined execution")


def _installed_source_root(executable: str) -> Path | None:
    """Read the installation path from Hermes when its CLI is a wrapper script."""
    safe_env = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
        }
        and value
    }
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=Path.home(),
            env=safe_env,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "install directory":
            candidate = Path(value.strip()).expanduser()
            if candidate.is_dir():
                return candidate
    return None


def _hermes_python(executable: str, agent_root: Path | None) -> str:
    if agent_root is not None:
        scripts = "Scripts" if os.name == "nt" else "bin"
        python_name = "python.exe" if os.name == "nt" else "python"
        candidate = Path(agent_root) / "venv" / scripts / python_name
        if candidate.is_file():
            return str(candidate)
    executable_path = Path(executable)
    candidate = executable_path.with_name("python.exe" if os.name == "nt" else "python")
    return str(candidate) if candidate.is_file() else sys.executable


def _browser_bridge_runtime_files() -> tuple[Path, ...]:
    """Return the bridge's local Python modules as narrow read-only mounts."""
    return tuple(
        Path(importlib.import_module(name).__file__).resolve(strict=True)
        for name in _BROWSER_BRIDGE_MODULES
    )


def _readonly_runtime_mounts(
    candidates: tuple[Path, ...], workspace: Path, task_home: Path
) -> tuple[Path, ...]:
    """Select disjoint runtime paths; workspace and task home are mounted separately."""
    protected = (workspace.resolve(), task_home.resolve())
    selected: list[Path] = []
    existing = {path.resolve(strict=True) for path in candidates if path.exists()}
    for candidate in sorted(existing, key=lambda path: len(path.parts)):
        if any(candidate == other or candidate in other.parents or other in candidate.parents for other in protected):
            continue
        if any(candidate == other or candidate in other.parents or other in candidate.parents for other in selected):
            continue
        selected.append(candidate)
    return tuple(selected)


def _profile_key_source(profile: str, hermes_root: Path | None) -> str:
    checked = sessions.validate_session_profile(profile, hermes_root)
    if isinstance(checked, dict):
        raise PermissionError(str(checked.get("safe_message") or "Hermes profile is not authorized"))
    return checked


def validate_prompt(prompt: str, timeout: int) -> tuple[str, int]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must not be empty")
    if len(prompt) > sessions.MAX_PROMPT_CHARS:
        raise ValueError(f"prompt exceeds the {sessions.MAX_PROMPT_CHARS}-character limit")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise TypeError("timeout must be an integer number of seconds")
    return prompt, max(sessions.MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))


def start_turn(
    *,
    task: dict[str, Any],
    prompt: str,
    timeout: int,
    confirm: bool,
    dry_run: bool,
    hermes_root: Path | None,
    agent_root: Path | None,
    save_task: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Build the confined Hermes command and record the durable job it starts."""
    policy = op.OperatorPolicy()
    policy.require_level("workspace")
    policy.require_mutation(dry_run)
    if policy.effective_dry_run(dry_run):
        overrides = ModelOverrides(
            task.get("model"), task.get("reasoning_effort")
        )
        browser_enabled = bool(task.get("browser_enabled"))
        return {
            "success": True,
            "dry_run": True,
            "changed": False,
            "task_id": task["task_id"],
            "workspace_id": task["workspace_id"],
            "model": overrides.model,
            "reasoning_effort": overrides.reasoning_effort,
            "toolsets": TOOLSETS if browser_enabled else PROFILE_DEFAULT_TOOLSETS,
            "browser_enabled": browser_enabled,
            "allow_workspace_write": bool(task.get("allow_workspace_write")),
        }
    if not confirm:
        return {"success": False, "code": "CONFIRMATION_REQUIRED", "safe_message": "Starting a Hermes task requires explicit confirmation."}

    profile = _profile_key_source(
        str(task.get("profile") or task.get("credential_profile") or "default"),
        hermes_root,
    )
    overrides = ModelOverrides(
        task.get("model"), task.get("reasoning_effort")
    )
    workspace = Path(str(task["workspace"])).expanduser().resolve(strict=True)
    policy.require_workspace_path(workspace)
    confinement.validate_workspace_boundary(workspace)
    writable = bool(task.get("allow_workspace_write"))
    if not confinement.confinement_available(writable=writable):
        posture = "workspace-write" if writable else "read-only"
        return {
            "success": False,
            "code": "TASK_CONFINEMENT_UNAVAILABLE",
            "safe_message": f"Hermes tasks require usable {posture} OS confinement; enable {confinement.CONFINEMENT_ENABLE_ENV} and install a working bwrap or sandbox-exec.",
        }

    executable = job_runtime._hermes_executable(agent_root)
    source_root = _source_root(executable, agent_root)
    task_home = Path(str(task["task_home"])).expanduser()
    profile_home = op.resolve_profile_home(profile, hermes_root)
    task_home = task_profile.prepare_task_profile(
        str(task["task_id"]),
        profile,
        task_home,
        hermes_root=hermes_root,
        executable=executable,
        source_home=profile_home,
    )
    task_root = (job_store._data_root(hermes_root) / "profiles").resolve(strict=True)
    try:
        task_home.relative_to(task_root)
    except ValueError as exc:
        raise PermissionError("Hermes task state is outside the task data root") from exc
    if task_home == workspace or workspace in task_home.parents or task_home in workspace.parents:
        raise PermissionError("Hermes task data and workspace paths must be separate")

    session_id = str(task.get("session_id") or "")
    browser_enabled = bool(task.get("browser_enabled"))
    toolsets = TOOLSETS if browser_enabled else PROFILE_DEFAULT_TOOLSETS
    writable_task_paths = [task_home]
    argv = [executable, "chat"]
    if overrides.model is not None:
        argv.extend(("--model", overrides.model))
    if overrides.reasoning_effort is not None:
        argv.extend(("--reasoning", overrides.reasoning_effort))
    argv.extend(("--in", str(workspace)))
    if session_id:
        argv += ["--resume", session_id]
    argv += ["--query-file", "-", "--oneshot", "-Q"]

    readonly_candidates = [source_root]
    if not browser_enabled:
        readonly_candidates.append(Path(__file__).resolve().parent)
    hermes_data_root = job_store._data_root(hermes_root).resolve()
    configured_node = hermes_data_root / "node"
    if configured_node.is_dir():
        readonly_candidates.append(configured_node)

    if browser_enabled:
        browser_state = json.loads(browser.browser_state_file(task_home).read_text(encoding="utf-8"))
        browser_executable_path = Path(str(browser_state["executable"])).expanduser()
        browser_executable = browser_executable_path.resolve(strict=True)
        # Keep the bridge target fixed even though Hermes can write its own home.
        # Mount the private task directory, not only its descriptor file. A
        # file bind creates synthetic 0755 parents inside bubblewrap, which
        # correctly fail the bridge's private-directory check.
        readonly_candidates.append(browser.browser_state_file(task_home).parent)
        # agent-browser may be a symlink. The bridge validates and launches the
        # configured path, so expose its directory as well as the resolved
        # binary's directory below.
        readonly_candidates.append(browser_executable_path.parent)
        writable_task_paths.append(Path(str(browser_state["socket_dir"])))
        if not configured_node.is_dir():
            readonly_candidates.append(browser_executable.parent)
        # Run the bridge in the same virtual environment as the Hermes source
        # tree. The plugin host's Python may have an interpreter symlink whose
        # base runtime is outside the task's approved read-only mounts.
        python = _hermes_python(executable, source_root)
        task_mcp.configure_task_browser(str(task["task_id"]), task_home, python)
        # The workspace can be a subdirectory of this plugin checkout. Binding
        # the whole checkout would overlap that workspace, so expose only the
        # bridge modules the child MCP server imports.
        readonly_candidates.extend(_browser_bridge_runtime_files())

    profile_runtime_candidates = task_mcp.configured_mcp_runtime_paths(
        task_home, os.environ.get("PATH")
    ) + task_profile.profile_resource_runtime_paths(task_home)
    readonly_candidates.extend(
        task_mounts.readonly_profile_runtime_paths(
            profile_runtime_candidates,
            workspace,
            task_home,
            hermes_data_root,
        )
    )

    readonly_paths = _readonly_runtime_mounts(tuple(readonly_candidates), workspace, task_home)
    sandboxed_argv = confinement.wrap_argv(
        argv,
        workspace,
        writable=writable,
        readonly_paths=readonly_paths,
        writable_paths=tuple(writable_task_paths),
    )
    child_env = os.environ.copy()
    child_env.update({
        "HOME": str(task_home),
        "HERMES_HOME": str(task_home),
        "HERMES_PROFILE": str(task["task_id"]),
    })
    result = job_runtime.start_managed_session_job(
        argv=sandboxed_argv,
        prompt=prompt,
        timeout=timeout,
        profile=profile,
        hermes_root=hermes_root,
        child_env=child_env,
        cwd=workspace,
        active_key=f"task:{task['task_id']}",
        metadata={
            "task_id": task["task_id"],
            "workspace_id": task["workspace_id"],
            "workspace": str(workspace),
            "task_home": str(task_home),
            "allow_workspace_write": writable,
            "model": overrides.model,
            "reasoning_effort": overrides.reasoning_effort,
            "toolsets": toolsets,
            "browser_enabled": bool(task.get("browser_enabled")),
            "session_id": session_id,
            "task_turn": int(task.get("turn_count", 0)) + 1,
        },
    )
    if result.get("success"):
        task.update({
            "latest_job_id": result["job_id"],
            "turn_count": int(task.get("turn_count", 0)) + 1,
            "status": "running",
        })
        try:
            save_task(task)
        except OSError:
            # The durable job record is authoritative if task metadata cannot
            # be refreshed after the child has already started.
            result["task_state_save_warning"] = "Task started; status will recover from its durable job record."
        result.update({
            "workspace_id": task["workspace_id"],
            "model": overrides.model,
            "reasoning_effort": overrides.reasoning_effort,
            "toolsets": toolsets,
            "browser_enabled": bool(task.get("browser_enabled")),
        })
    return result
