"""Scoped Hermes tasks built on the existing session-job lifecycle.

Each task gets a private Hermes data home and a narrow shared-browser MCP
toolset. The child is OS-confined to its authorized workspace, its own session
data, and the read-only Hermes runtime. Follow-up turns reuse the same task
home, browser profile, and Hermes session ID.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import operator_browser as browser
import operator_policy as op
import operator_session as sessions
import operator_session_task_runtime as runtime
import runner_confinement as confinement
from operator_session_task_runtime import (
    FILE_ONLY_TOOLSETS,
    MODEL_ID,
    PROVIDER_KEY_ENVS,
    REASONING_EFFORTS,
    TOOLSETS,
)

ENABLE_SCOPED_TASKS_ENV = "HERMES_GPT_ENABLE_SCOPED_TASKS"
TASK_WORKSPACES_ENV = "HERMES_GPT_TASK_WORKSPACES"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_root(hermes_root: Path | None) -> Path:
    return sessions._root(hermes_root) / "tasks"


def _task_path(task_id: str, hermes_root: Path | None) -> Path:
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        raise ValueError("task_id has an invalid format")
    return _task_root(hermes_root) / f"{task_id}.json"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _save_task_record(task: dict[str, Any], hermes_root: Path | None) -> None:
    task["updated_at"] = _now()
    _write_json(_task_path(str(task["task_id"]), hermes_root), task)


def _workspaces(policy: op.OperatorPolicy) -> dict[str, Path]:
    raw = os.environ.get(TASK_WORKSPACES_ENV, "").strip()
    if not raw:
        raise PermissionError(f"{TASK_WORKSPACES_ENV} must map workspace IDs to authorized paths")
    try:
        configured = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{TASK_WORKSPACES_ENV} must be a JSON object") from exc
    if not isinstance(configured, dict) or not configured or len(configured) > 32:
        raise ValueError(f"{TASK_WORKSPACES_ENV} must be a non-empty JSON object with at most 32 entries")
    out: dict[str, Path] = {}
    for alias, raw_path in configured.items():
        name = str(alias)
        if not _ALIAS_RE.fullmatch(name):
            raise ValueError(f"{TASK_WORKSPACES_ENV} contains an invalid workspace ID")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"{TASK_WORKSPACES_ENV} workspace paths must be non-empty strings")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"configured workspace {name!r} is not an accessible directory")
        policy.require_workspace_path(path)
        out[name] = path
    return out


def hermes_task_workspaces(hermes_root: Path | None = None) -> dict[str, Any]:
    """List configured workspace IDs without disclosing their host paths."""
    try:
        if not op.env_truthy(ENABLE_SCOPED_TASKS_ENV):
            raise PermissionError(f"Scoped Hermes tasks are disabled. Set {ENABLE_SCOPED_TASKS_ENV}=1.")
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        entries = _workspaces(policy)
        return {
            "success": True,
            "workspaces": [
                {"id": alias, "label": path.name or alias}
                for alias, path in sorted(entries.items())
            ],
            "model": MODEL_ID,
            "toolsets": TOOLSETS,
            "reasoning_efforts": sorted(REASONING_EFFORTS),
            "supported_model_providers": sorted(PROVIDER_KEY_ENVS),
            "browser_available": browser.browser_available(hermes_root),
            "write_confinement_available": confinement.confinement_available(writable=True),
            "read_confinement_available": confinement.confinement_available(writable=False),
        }
    except (OSError, ValueError) as exc:
        return {"success": False, "code": "TASK_WORKSPACES_UNAVAILABLE", "safe_message": str(exc)}


def hermes_task_start(
    prompt: str,
    workspace_id: str,
    credential_profile: str = "default",
    allow_workspace_write: bool = False,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 900,
    *,
    model: str = MODEL_ID,
    reasoning_effort: str = "high",
    browser_enabled: bool = True,
    headed_browser: bool = False,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Start a scoped Hermes session with a private workspace and browser."""
    task_home: Path | None = None
    task_record: Path | None = None
    try:
        if not op.env_truthy(ENABLE_SCOPED_TASKS_ENV):
            raise PermissionError(f"Scoped Hermes tasks are disabled. Set {ENABLE_SCOPED_TASKS_ENV}=1.")
        safe_prompt, safe_timeout = runtime.validate_prompt(prompt, timeout)
        if not isinstance(allow_workspace_write, bool):
            raise TypeError("allow_workspace_write must be a boolean")
        if not isinstance(browser_enabled, bool) or not isinstance(headed_browser, bool):
            raise TypeError("browser_enabled and headed_browser must be booleans")
        if headed_browser and not browser_enabled:
            raise ValueError("headed_browser requires browser_enabled=true")
        model, reasoning_effort = runtime._validate_model_and_effort(model, reasoning_effort)
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        workspaces = _workspaces(policy)
        alias = str(workspace_id or "").strip()
        workspace = workspaces.get(alias)
        if workspace is None:
            raise ValueError("workspace_id is not configured for scoped Hermes tasks")
        profile = runtime._profile_key_source(str(credential_profile or "default"), hermes_root)
        runtime._model_credentials(model, profile, hermes_root)
        task_id = uuid4().hex
        toolsets = TOOLSETS if browser_enabled else FILE_ONLY_TOOLSETS
        if policy.effective_dry_run(dry_run):
            return {
                "success": True,
                "dry_run": True,
                "changed": False,
                "task_id": task_id,
                "workspace_id": alias,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "toolsets": toolsets,
                "browser_enabled": browser_enabled,
                "headed_browser": headed_browser,
                "allow_workspace_write": allow_workspace_write,
            }
        if not confirm:
            return {"success": False, "code": "CONFIRMATION_REQUIRED", "safe_message": "Starting a Hermes task requires explicit confirmation."}
        task_home = (sessions._data_root(hermes_root) / "profiles" / task_id).resolve()
        task_home.mkdir(parents=True, mode=0o700)
        try:
            task_home.chmod(0o700)
        except OSError:
            pass
        if browser_enabled:
            browser_result = browser.create_browser_session(
                task_id,
                task_home,
                hermes_root,
                headed=headed_browser,
            )
            if not browser_result.get("success"):
                shutil.rmtree(task_home, ignore_errors=True)
                return browser_result
        task = {
            "task_id": task_id,
            "workspace_id": alias,
            "workspace": str(workspace),
            "task_home": str(task_home),
            "hermes_root": str(sessions._data_root(hermes_root)),
            "credential_profile": profile,
            "allow_workspace_write": bool(allow_workspace_write),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "toolsets": toolsets,
            "browser_enabled": browser_enabled,
            "headed_browser": headed_browser,
            "session_id": "",
            "latest_job_id": "",
            "turn_count": 0,
            "status": "created",
            "created_at": _now(),
            "updated_at": _now(),
        }
        task_record = _task_path(task_id, hermes_root)
        _write_json(task_record, task)
        dry_result = runtime.start_turn(
            task=task,
            prompt=safe_prompt,
            timeout=safe_timeout,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=hermes_root,
            agent_root=agent_root,
            save_task=lambda record: _save_task_record(record, hermes_root),
        )
        if dry_result.get("dry_run") or not dry_result.get("success"):
            if browser_enabled:
                browser.browser_command(task_home, "close")
                browser.delete_browser_state(task_home)
            shutil.rmtree(task_home, ignore_errors=True)
            task_record.unlink(missing_ok=True)
            return dry_result
        return dry_result
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        if task_home is not None:
            browser.browser_command(task_home, "close")
            browser.delete_browser_state(task_home)
        if task_record is not None:
            task_record.unlink(missing_ok=True)
        if task_home is not None:
            shutil.rmtree(task_home, ignore_errors=True)
        return {"success": False, "code": "TASK_START_ERROR", "safe_message": str(exc)}


def hermes_task_continue(
    task_id: str,
    prompt: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 900,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Continue the same isolated Hermes session and its original workspace scope."""
    try:
        if not op.env_truthy(ENABLE_SCOPED_TASKS_ENV):
            raise PermissionError(f"Scoped Hermes tasks are disabled. Set {ENABLE_SCOPED_TASKS_ENV}=1.")
        safe_prompt, safe_timeout = runtime.validate_prompt(prompt, timeout)
        task = _read_json(_task_path(str(task_id), hermes_root))
        if not task or task.get("task_id") != task_id:
            return {"success": False, "code": "TASK_NOT_FOUND", "safe_message": "Hermes task was not found."}
        selected_model, selected_effort = runtime._validate_model_and_effort(
            model or str(task.get("model") or MODEL_ID),
            reasoning_effort or str(task.get("reasoning_effort") or "high"),
        )
        task["model"] = selected_model
        task["reasoning_effort"] = selected_effort
        task["toolsets"] = TOOLSETS if bool(task.get("browser_enabled")) else FILE_ONLY_TOOLSETS
        latest_job = sessions._load(str(task.get("latest_job_id") or ""), hermes_root) or {}
        if sessions._recover_task_session_id(latest_job):
            sessions._save(latest_job, hermes_root)
        if latest_job.get("status") in {"starting", "running"}:
            return {"success": False, "code": "TASK_BUSY", "safe_message": "Wait for the current Hermes task turn to finish."}
        session_id = str(latest_job.get("session_id") or task.get("session_id") or "")
        if not session_id:
            return {"success": False, "code": "TASK_SESSION_UNAVAILABLE", "safe_message": "The Hermes session ID was not recorded, so this task cannot be resumed."}
        task["session_id"] = session_id
        if latest_job.get("status"):
            task["status"] = str(latest_job["status"])
        return runtime.start_turn(
            task=task,
            prompt=safe_prompt,
            timeout=safe_timeout,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=hermes_root,
            agent_root=agent_root,
            save_task=lambda record: _save_task_record(record, hermes_root),
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"success": False, "code": "TASK_CONTINUE_ERROR", "safe_message": str(exc)}


def hermes_task_status(task_id: str, hermes_root: Path | None = None) -> dict[str, Any]:
    try:
        if not op.env_truthy(ENABLE_SCOPED_TASKS_ENV):
            raise PermissionError(f"Scoped Hermes tasks are disabled. Set {ENABLE_SCOPED_TASKS_ENV}=1.")
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        task = _read_json(_task_path(str(task_id), hermes_root))
        if not task or task.get("task_id") != task_id:
            return {"success": False, "code": "TASK_NOT_FOUND", "safe_message": "Hermes task was not found."}
        job_id = str(task.get("latest_job_id") or "")
        if not job_id:
            jobs_root = sessions._root(hermes_root)
            candidates = []
            for path in jobs_root.glob("*.json") if jobs_root.is_dir() else ():
                job = _read_json(path)
                if job and job.get("task_id") == task_id:
                    candidates.append(job)
            if candidates:
                latest = max(candidates, key=lambda item: (int(item.get("task_turn", 0)), str(item.get("created_at", ""))))
                job_id = str(latest.get("job_id") or "")
                task["latest_job_id"] = job_id
        job = sessions.hermes_session_job_status(job_id, hermes_root).get("job") if job_id else None
        if isinstance(job, dict):
            if job.get("session_id"):
                task["session_id"] = job["session_id"]
            task["status"] = str(job.get("status") or task.get("status"))
            task["updated_at"] = _now()
            _write_json(_task_path(task_id, hermes_root), task)
        return {
            "success": True,
            "task": {
                "task_id": task_id,
                "workspace_id": task.get("workspace_id"),
                "status": task.get("status"),
                "model": str(task.get("model") or MODEL_ID),
                "reasoning_effort": str(task.get("reasoning_effort") or "high"),
                "toolsets": str(task.get("toolsets") or FILE_ONLY_TOOLSETS),
                "browser_enabled": bool(task.get("browser_enabled")),
                "headed_browser": bool(task.get("headed_browser")),
                "browser": browser.browser_session_state(Path(str(task["task_home"])))
                if bool(task.get("browser_enabled")) else None,
                "allow_workspace_write": bool(task.get("allow_workspace_write")),
                "session_id": task.get("session_id") or None,
                "latest_job_id": job_id or None,
                "turn_count": int(task.get("turn_count", 0)),
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
                "job": job,
            },
        }
    except (OSError, TypeError, ValueError) as exc:
        return {"success": False, "code": "TASK_STATUS_ERROR", "safe_message": str(exc)}


def hermes_task_result(task_id: str, max_chars: int = sessions.MAX_RESULT_CHARS, hermes_root: Path | None = None) -> dict[str, Any]:
    status = hermes_task_status(task_id, hermes_root)
    if not status.get("success"):
        return status
    job_id = str((status.get("task") or {}).get("latest_job_id") or "")
    if not job_id:
        return {"success": False, "code": "TASK_RESULT_UNAVAILABLE", "safe_message": "Hermes task has no completed turn yet."}
    return sessions.hermes_session_job_result(job_id, max_chars, hermes_root)


__all__ = [
    "ENABLE_SCOPED_TASKS_ENV",
    "MODEL_ID",
    "TASK_WORKSPACES_ENV",
    "TOOLSETS",
    "hermes_task_continue",
    "hermes_task_result",
    "hermes_task_start",
    "hermes_task_status",
    "hermes_task_workspaces",
]
