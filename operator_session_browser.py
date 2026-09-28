"""Authorized ChatGPT controls for browser sessions owned by managed Hermes tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import operator_browser as browser
import operator_policy as op
import operator_session_job_store as session_store
import operator_session_tasks as tasks


def _authorized_task(task_id: str, hermes_root: Path | None) -> tuple[dict[str, Any], Path] | dict[str, Any]:
    if not op.env_truthy(tasks.ENABLE_SCOPED_TASKS_ENV):
        return {
            "success": False,
            "code": "TASKS_DISABLED",
            "safe_message": f"Scoped Hermes tasks are disabled. Set {tasks.ENABLE_SCOPED_TASKS_ENV}=1.",
        }
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        task = tasks._read_json(tasks._task_path(str(task_id), hermes_root))
        if not task or task.get("task_id") != task_id:
            return {"success": False, "code": "TASK_NOT_FOUND", "safe_message": "Hermes task was not found."}
        task_home = Path(str(task.get("task_home") or "")).expanduser().resolve(strict=True)
        task_root = (session_store._data_root(hermes_root) / "profiles").resolve(strict=True)
        if task_home.name != task_id or task_root not in task_home.parents:
            return {
                "success": False,
                "code": "TASK_STATE_INVALID",
                "safe_message": "Hermes task state is outside its authorized data root.",
            }
        return task, task_home
    except (PermissionError, TypeError, ValueError, OSError) as exc:
        return {"success": False, "code": "TASK_BROWSER_ACCESS_DENIED", "safe_message": str(exc)}


def _browser_read(task_id: str, command: str, hermes_root: Path | None) -> dict[str, Any]:
    checked = _authorized_task(task_id, hermes_root)
    if isinstance(checked, dict):
        return checked
    task, task_home = checked
    if not task.get("browser_enabled"):
        return {
            "success": False,
            "code": "TASK_BROWSER_DISABLED",
            "safe_message": "This Hermes task was created without browser access.",
        }
    return browser.browser_command(task_home, command)


def _browser_action(
    task_id: str,
    command: str,
    args: list[str],
    confirm: bool,
    dry_run: bool,
    hermes_root: Path | None,
) -> dict[str, Any]:
    checked = _authorized_task(task_id, hermes_root)
    if isinstance(checked, dict):
        return checked
    task, task_home = checked
    if not task.get("browser_enabled"):
        return {
            "success": False,
            "code": "TASK_BROWSER_DISABLED",
            "safe_message": "This Hermes task was created without browser access.",
        }
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
    except PermissionError as exc:
        return {"success": False, "code": "TASK_BROWSER_POLICY_DENIED", "safe_message": str(exc)}
    if policy.effective_dry_run(dry_run):
        return {"success": True, "dry_run": True, "changed": False, "task_id": task_id, "action": command}
    if not isinstance(confirm, bool) or not confirm:
        return {
            "success": False,
            "code": "CONFIRMATION_REQUIRED",
            "safe_message": "Browser actions require confirm=true.",
        }
    return browser.browser_command(task_home, command, args)


def hermes_task_browser_status(task_id: str) -> dict[str, Any]:
    checked = _authorized_task(task_id, None)
    if isinstance(checked, dict):
        return checked
    task, task_home = checked
    if not task.get("browser_enabled"):
        return {
            "success": False,
            "code": "TASK_BROWSER_DISABLED",
            "safe_message": "This Hermes task was created without browser access.",
        }
    return browser.browser_session_state(task_home)


def hermes_task_browser_snapshot(task_id: str) -> dict[str, Any]:
    return _browser_read(task_id, "snapshot", None)


def hermes_task_browser_navigate(
    task_id: str, url: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "navigate", [url], confirm, dry_run, None)


def hermes_task_browser_click(
    task_id: str, ref: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "click", [ref], confirm, dry_run, None)


def hermes_task_browser_type(
    task_id: str, ref: str, text: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "type", [ref, text], confirm, dry_run, None)


def hermes_task_browser_scroll(
    task_id: str, direction: str, pixels: int = 500, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "scroll", [direction, str(pixels)], confirm, dry_run, None)


def hermes_task_browser_back(
    task_id: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "back", [], confirm, dry_run, None)


def hermes_task_browser_press(
    task_id: str, key: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    return _browser_action(task_id, "press", [key], confirm, dry_run, None)


def hermes_task_browser_close(
    task_id: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    """Close a task-owned isolated browser after its mutation gates pass."""
    return _browser_action(task_id, "close", [], confirm, dry_run, None)


def hermes_task_browser_restart(
    task_id: str, confirm: bool = False, dry_run: bool = True,
) -> dict[str, Any]:
    checked = _authorized_task(task_id, None)
    if isinstance(checked, dict):
        return checked
    task, task_home = checked
    if not task.get("browser_enabled"):
        return {
            "success": False,
            "code": "TASK_BROWSER_DISABLED",
            "safe_message": "This Hermes task was created without browser access.",
        }
    if task.get("browser_source") == "hermes_profile":
        return {
            "success": False,
            "code": "SHARED_BROWSER_RESTART_UNAVAILABLE",
            "safe_message": "A browser attached from a Hermes profile cannot be restarted by a managed task.",
        }
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
    except PermissionError as exc:
        return {"success": False, "code": "TASK_BROWSER_POLICY_DENIED", "safe_message": str(exc)}
    if policy.effective_dry_run(dry_run):
        return {"success": True, "dry_run": True, "changed": False, "task_id": task_id, "action": "restart_browser"}
    if not isinstance(confirm, bool) or not confirm:
        return {
            "success": False,
            "code": "CONFIRMATION_REQUIRED",
            "safe_message": "Restarting the browser requires confirm=true.",
        }
    root = Path(str(task.get("hermes_root") or session_store._data_root(None)))
    return browser.create_browser_session(
        task_id,
        task_home,
        root,
        headed=bool(task.get("headed_browser")),
    )


__all__ = [
    "hermes_task_browser_back",
    "hermes_task_browser_click",
    "hermes_task_browser_close",
    "hermes_task_browser_navigate",
    "hermes_task_browser_press",
    "hermes_task_browser_restart",
    "hermes_task_browser_scroll",
    "hermes_task_browser_snapshot",
    "hermes_task_browser_status",
    "hermes_task_browser_type",
]
