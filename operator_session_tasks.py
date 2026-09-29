"""Scoped Hermes tasks built on the existing session-job lifecycle.

Each task gets a private clone of an allowed Hermes profile, its own session
data, and an optional isolated browser. The child is OS-confined to its
authorized workspace and the runtime resources required by that profile.
Follow-up turns reuse the same task home, browser profile, and Hermes session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import operator_browser as browser
import operator_browser_profiles as browser_profiles
import operator_policy as op
import operator_session as sessions
import operator_session_job_store as job_store
import operator_session_jobs as job_runtime
import operator_session_task_profile as task_profile
import operator_session_task_runtime as runtime
import runner_confinement as confinement
from operator_session_task_runtime import (
    PROFILE_DEFAULT_TOOLSETS,
    REASONING_EFFORTS,
    TOOLSETS,
)
from operator_session_task_store import (
    _TASK_ID_RE,
    MAX_TASK_LIST_LIMIT,
    MAX_TASK_LIST_OFFSET,
    _now,
    _public_task_summary,
    _read_json,
    _save_task_record,
    _task_path,
    _task_root,
    _task_setting,
    _write_json,
)

ENABLE_SCOPED_TASKS_ENV = "HERMES_GPT_ENABLE_SCOPED_TASKS"
TASK_WORKSPACES_ENV = "HERMES_GPT_TASK_WORKSPACES"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


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
            "model_selection": "selected Hermes profile default unless model is overridden",
            "toolsets": TOOLSETS,
            "reasoning_efforts": sorted(REASONING_EFFORTS),
            "browser_available": browser.browser_available(hermes_root),
            "write_confinement_available": confinement.confinement_available(writable=True),
            "read_confinement_available": confinement.confinement_available(writable=False),
        }
    except (OSError, ValueError) as exc:
        return {"success": False, "code": "TASK_WORKSPACES_UNAVAILABLE", "safe_message": str(exc)}


def hermes_task_list(
    limit: int = 20,
    offset: int = 0,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """List resumable managed sessions without returning private task fields."""
    try:
        if not op.env_truthy(ENABLE_SCOPED_TASKS_ENV):
            raise PermissionError(f"Scoped Hermes tasks are disabled. Set {ENABLE_SCOPED_TASKS_ENV}=1.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TASK_LIST_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {MAX_TASK_LIST_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_TASK_LIST_OFFSET:
            raise ValueError(f"offset must be an integer from 0 to {MAX_TASK_LIST_OFFSET}")

        op.OperatorPolicy().require_level("read_only")
        tasks_root = _task_root(hermes_root)
        summaries = []
        if tasks_root.is_dir():
            for path in tasks_root.glob("*.json"):
                if not _TASK_ID_RE.fullmatch(path.stem):
                    continue
                task = _read_json(path)
                if not task or task.get("task_id") != path.stem:
                    continue
                summary = _public_task_summary(task)
                if summary is not None:
                    summaries.append(summary)

        summaries.sort(
            key=lambda item: (item["created_at"], item["task_id"]),
            reverse=True,
        )
        page = summaries[offset : offset + limit]
        has_more = offset + len(page) < len(summaries)
        return {
            "success": True,
            "tasks": page,
            "returned_count": len(page),
            "total_count": len(summaries),
            "offset": offset,
            "next_offset": offset + len(page) if has_more else None,
            "has_more": has_more,
        }
    except (OSError, TypeError, ValueError) as exc:
        return {"success": False, "code": "TASK_LIST_ERROR", "safe_message": str(exc)}


def hermes_task_start(
    prompt: str,
    workspace_id: str,
    profile: str = "default",
    allow_workspace_write: bool = False,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 900,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    browser_enabled: bool = True,
    headed_browser: bool = False,
    browser_profile: str | None = None,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    credential_profile: str | None = None,
) -> dict[str, Any]:
    """Start a scoped Hermes session with a private workspace and browser."""
    task_home: Path | None = None
    task_record: Path | None = None
    browser_cdp_port: int | None = None
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
        if browser_profile is not None and not isinstance(browser_profile, str):
            raise TypeError("browser_profile must be a Hermes profile name or omitted")
        selected_browser_profile = str(browser_profile or "").strip()
        if selected_browser_profile:
            if not browser_enabled:
                raise ValueError("browser_profile requires browser_enabled=true")
            if headed_browser:
                raise ValueError("headed_browser cannot be used with a configured Hermes browser")
            selected_browser_profile = runtime._profile_key_source(selected_browser_profile, hermes_root)
            browser_cdp_port = browser_profiles.profile_cdp_port(
                selected_browser_profile, hermes_root
            )
        model, reasoning_effort = runtime._validate_model_and_effort(model, reasoning_effort)
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        workspaces = _workspaces(policy)
        alias = str(workspace_id or "").strip()
        workspace = workspaces.get(alias)
        if workspace is None:
            raise ValueError("workspace_id is not configured for scoped Hermes tasks")
        if credential_profile and profile != "default" and credential_profile != profile:
            raise ValueError("profile and credential_profile must identify the same Hermes profile")
        selected_profile = runtime._profile_key_source(
            str(profile if profile != "default" else credential_profile or profile),
            hermes_root,
        )
        task_id = uuid4().hex
        toolsets = TOOLSETS if browser_enabled else PROFILE_DEFAULT_TOOLSETS
        if not browser_enabled:
            browser_source = "disabled"
        elif browser_cdp_port is not None:
            browser_source = "hermes_profile"
        else:
            browser_source = "isolated"
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
                "browser_source": browser_source,
                "headed_browser": headed_browser,
                "allow_workspace_write": allow_workspace_write,
            }
        if not confirm:
            return {"success": False, "code": "CONFIRMATION_REQUIRED", "safe_message": "Starting a Hermes task requires explicit confirmation."}
        task_home = (job_store._data_root(hermes_root) / "profiles" / task_id).resolve()
        executable = job_runtime._hermes_executable(agent_root)
        task_home = task_profile.prepare_task_profile(
            task_id,
            selected_profile,
            task_home,
            hermes_root=hermes_root,
            executable=executable,
            source_home=op.resolve_profile_home(selected_profile, hermes_root),
        )
        if browser_enabled:
            if browser_cdp_port is not None:
                browser_result = browser.create_profile_browser_session(
                    task_id, task_home, hermes_root, browser_cdp_port
                )
            else:
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
            "hermes_root": str(job_store._data_root(hermes_root)),
            "profile": selected_profile,
            "allow_workspace_write": bool(allow_workspace_write),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "toolsets": toolsets,
            "browser_enabled": browser_enabled,
            "browser_source": browser_source,
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
            if browser_enabled and browser_cdp_port is None:
                browser.browser_command(task_home, "close")
            if browser_enabled:
                browser.delete_browser_state(task_home)
            shutil.rmtree(task_home, ignore_errors=True)
            task_record.unlink(missing_ok=True)
            return dry_result
        return dry_result
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        if task_home is not None:
            if browser_cdp_port is None:
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
            model, reasoning_effort
        )
        task["model"] = selected_model
        task["reasoning_effort"] = selected_effort
        task["toolsets"] = TOOLSETS if bool(task.get("browser_enabled")) else PROFILE_DEFAULT_TOOLSETS
        latest_job = job_store._load(str(task.get("latest_job_id") or ""), hermes_root) or {}
        if job_runtime._recover_task_session_id(latest_job, hermes_root):
            job_store._save(latest_job, hermes_root)
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
            jobs_root = job_store._root(hermes_root)
            candidates = []
            for path in jobs_root.glob("*.json") if jobs_root.is_dir() else ():
                job = _read_json(path)
                if job and job.get("task_id") == task_id:
                    candidates.append(job)
            if candidates:
                latest = max(candidates, key=lambda item: (int(item.get("task_turn", 0)), str(item.get("created_at", ""))))
                job_id = str(latest.get("job_id") or "")
                task["latest_job_id"] = job_id
        job = job_runtime.hermes_session_job_status(job_id, hermes_root).get("job") if job_id else None
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
                "model": _task_setting(task, "model", 128),
                "reasoning_effort": _task_setting(task, "reasoning_effort", 32),
                "toolsets": str(task.get("toolsets") or PROFILE_DEFAULT_TOOLSETS),
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
    return job_runtime.hermes_session_job_result(job_id, max_chars, hermes_root)


__all__ = [
    "ENABLE_SCOPED_TASKS_ENV",
    "TASK_WORKSPACES_ENV",
    "TOOLSETS",
    "hermes_task_continue",
    "hermes_task_list",
    "hermes_task_result",
    "hermes_task_start",
    "hermes_task_status",
    "hermes_task_workspaces",
]
