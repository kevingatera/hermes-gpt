"""Prepare and launch one confined turn for a managed Hermes task."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

import operator_browser as browser
import operator_config as op_config
import operator_policy as op
import operator_runners as runners
import operator_session as sessions
import runner_confinement as confinement

MODEL_ID = "deepseek/deepseek-v4.1-flash"
TOOLSETS = "file,hermes-gpt-browser"
FILE_ONLY_TOOLSETS = "file"
MAX_TIMEOUT = 3600
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
PROVIDER_KEY_ENVS = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai-api": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "zai": ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
    "kimi-coding": ("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
    "alibaba": ("DASHSCOPE_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "groq": ("GROQ_API_KEY",),
}


def _source_root(executable: str, agent_root: Path | None) -> Path:
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
    raise FileNotFoundError("Hermes Agent source root could not be resolved for confined execution")


def _model_credentials(model: str, profile: str, hermes_root: Path | None) -> tuple[str, str]:
    """Read only the selected provider key from the explicitly allowed profile."""
    provider, separator, _model_name = model.partition("/")
    candidates = PROVIDER_KEY_ENVS.get(provider.lower())
    if not separator or not candidates:
        supported = ", ".join(sorted(PROVIDER_KEY_ENVS))
        raise ValueError(f"model must use a supported provider/model ID; supported providers: {supported}")
    profile_home = op.resolve_profile_home(profile, hermes_root) if hermes_root is not None else None
    for env_name in candidates:
        key = os.environ.get(env_name, "").strip()
        if not key and profile_home is not None:
            raw = op_config._read_env_value(profile_home / ".env", env_name) or ""
            key = runners._unquote_env_value(raw).strip()
        if key:
            return env_name, key
    raise PermissionError(f"No {provider} API key is available in the selected credential profile")


def _validate_model_and_effort(model: str, effort: str) -> tuple[str, str]:
    if not isinstance(model, str) or len(model) > 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/-]*", model):
        raise ValueError("model must be a valid provider/model ID")
    provider, separator, model_name = model.partition("/")
    if not separator or not provider or not model_name:
        raise ValueError("model must use provider/model syntax")
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        choices = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(f"reasoning_effort must be one of: {choices}")
    return model, effort


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


def _browser_mcp_config(task_home: Path, agent_python: str) -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    return {
        "mcp_servers": {
            "hermes-gpt-browser": {
                "command": agent_python,
                "args": ["-m", "hermes_gpt_browser_mcp"],
                "env": {
                    "PYTHONPATH": str(package_root),
                    "HERMES_GPT_BROWSER_STATE_FILE": str(browser.browser_state_file(task_home)),
                },
                "enabled": True,
                "connect_timeout": 15,
                "timeout": 60,
                "supports_parallel_tool_calls": False,
                "tools": {"resources": False, "prompts": False},
            }
        }
    }


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
        model = str(task.get("model") or MODEL_ID)
        reasoning_effort = str(task.get("reasoning_effort") or "high")
        browser_enabled = bool(task.get("browser_enabled"))
        return {
            "success": True,
            "dry_run": True,
            "changed": False,
            "task_id": task["task_id"],
            "workspace_id": task["workspace_id"],
            "model": model,
            "reasoning_effort": reasoning_effort,
            "toolsets": TOOLSETS if browser_enabled else FILE_ONLY_TOOLSETS,
            "browser_enabled": browser_enabled,
            "allow_workspace_write": bool(task.get("allow_workspace_write")),
        }
    if not confirm:
        return {"success": False, "code": "CONFIRMATION_REQUIRED", "safe_message": "Starting a Hermes task requires explicit confirmation."}

    profile = _profile_key_source(str(task["credential_profile"]), hermes_root)
    model, reasoning_effort = _validate_model_and_effort(
        str(task.get("model") or MODEL_ID), str(task.get("reasoning_effort") or "high")
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

    executable = sessions._hermes_executable(agent_root)
    source_root = _source_root(executable, agent_root)
    task_home = Path(str(task["task_home"])).expanduser().resolve(strict=True)
    task_root = (sessions._data_root(hermes_root) / "profiles").resolve(strict=True)
    try:
        task_home.relative_to(task_root)
    except ValueError as exc:
        raise PermissionError("Hermes task state is outside the task data root") from exc
    if task_home == workspace or workspace in task_home.parents or task_home in workspace.parents:
        raise PermissionError("Hermes task data and workspace paths must be separate")

    key_env, key = _model_credentials(model, profile, hermes_root)
    session_id = str(task.get("session_id") or "")
    toolsets = TOOLSETS if bool(task.get("browser_enabled")) else FILE_ONLY_TOOLSETS
    writable_task_paths = [task_home]
    argv = [
        executable,
        "chat",
        "--model", model,
        "--reasoning", reasoning_effort,
        "--toolsets", toolsets,
        "--ignore-rules",
        "--in", str(workspace),
    ]
    if session_id:
        argv += ["--resume", session_id]
    argv += ["--query-file", "-", "--oneshot", "-Q"]

    readonly_candidates = [source_root, Path(__file__).resolve().parent]
    if bool(task.get("browser_enabled")):
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
        configured_node = Path(hermes_root).expanduser() / "node" if hermes_root else None
        readonly_candidates.append(configured_node if configured_node and configured_node.is_dir() else browser_executable.parent)
        python = _hermes_python(executable, agent_root)
        config = _browser_mcp_config(task_home, python)
        config_path = task_home / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        try:
            config_path.chmod(0o600)
        except OSError:
            pass

    readonly_paths = _readonly_runtime_mounts(tuple(readonly_candidates), workspace, task_home)
    sandboxed_argv = confinement.wrap_argv(
        argv,
        workspace,
        writable=writable,
        readonly_paths=readonly_paths,
        writable_paths=tuple(writable_task_paths),
    )
    child_env = runners._minimal_child_env()
    child_env.update({
        "HOME": str(task_home),
        "HERMES_HOME": str(task_home),
        key_env: key,
    })
    result = sessions.start_managed_session_job(
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
            "model": model,
            "reasoning_effort": reasoning_effort,
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
            "model": model,
            "reasoning_effort": reasoning_effort,
            "toolsets": toolsets,
            "browser_enabled": bool(task.get("browser_enabled")),
        })
    return result
