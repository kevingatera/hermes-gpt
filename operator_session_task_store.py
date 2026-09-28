"""Private JSON storage and public summaries for managed Hermes tasks."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_session_job_store as session_store
from operator_session_task_runtime import MODEL_ID

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_TASK_LIST_LIMIT = 100
MAX_TASK_LIST_OFFSET = 100_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_root(hermes_root: Path | None) -> Path:
    return session_store._root(hermes_root) / "tasks"


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


def _public_task_summary(task: dict[str, Any]) -> dict[str, Any] | None:
    """Project a task record onto fields needed to choose a session to resume."""
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return None

    def text_field(name: str, maximum: int, default: str = "") -> str:
        value = task.get(name)
        return value[:maximum] if isinstance(value, str) else default

    turn_count = task.get("turn_count")
    if not isinstance(turn_count, int) or isinstance(turn_count, bool):
        turn_count = 0

    browser_enabled = task.get("browser_enabled") is True
    if not browser_enabled:
        browser_source = "disabled"
    elif task.get("browser_source") == "hermes_profile":
        browser_source = "hermes_profile"
    else:
        # Older records and unknown values use the isolated-browser default.
        browser_source = "isolated"

    return {
        "task_id": task_id,
        "workspace_id": text_field("workspace_id", 64),
        "status": text_field("status", 32, "unknown"),
        "model": text_field("model", 128, MODEL_ID),
        "reasoning_effort": text_field("reasoning_effort", 32, "high"),
        "browser_enabled": browser_enabled,
        "browser_source": browser_source,
        "headed_browser": (
            browser_enabled
            and browser_source == "isolated"
            and task.get("headed_browser") is True
        ),
        "turn_count": max(0, turn_count),
        "created_at": text_field("created_at", 64),
        "updated_at": text_field("updated_at", 64),
    }
