"""MCP-facing status, cancellation, and result API for session jobs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_session_job_runtime import (
    ENABLE_SESSION_CONTROL_ENV,
    MAX_PROMPT_CHARS,
    MAX_RESULT_CHARS,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    MODEL_ID_RE,
    REASONING_EFFORTS,
    SESSION_ALLOWED_PROFILES_ENV,
    SESSION_JOB_TOPIC,
    _error,
    _finish_job,
    _hermes_executable,
    _lock,
    _processes,
    _reconcile,
    _recover_task_session_id,
    _redact,
    _terminate,
    start_managed_session_job,
)
from operator_session_job_store import _load, _paths, _save

__all__ = [
    "ENABLE_SESSION_CONTROL_ENV",
    "MAX_PROMPT_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_TIMEOUT",
    "MIN_TIMEOUT",
    "MODEL_ID_RE",
    "REASONING_EFFORTS",
    "SESSION_ALLOWED_PROFILES_ENV",
    "SESSION_JOB_TOPIC",
    "_error",
    "_finish_job",
    "_hermes_executable",
    "_lock",
    "_processes",
    "_reconcile",
    "_recover_task_session_id",
    "_redact",
    "_terminate",
    "hermes_session_job_cancel",
    "hermes_session_job_result",
    "hermes_session_job_status",
    "start_managed_session_job",
]


def hermes_session_job_status(
    job_id: str, hermes_root: Path | None = None
) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error(
            "JOB_NOT_FOUND",
            "Hermes session job was not found.",
            "Check the job ID returned by hermes_session_continue.",
        )
    public_meta = dict(meta)
    public_meta.pop("active_key", None)
    if public_meta.get("task_id"):
        if _recover_task_session_id(meta, hermes_root):
            _save(meta, hermes_root)
            public_meta["session_id"] = meta["session_id"]
        for key in ("workspace", "task_home", "active_key", "usage_file"):
            public_meta.pop(key, None)
    return _redact({"success": True, "job": public_meta})


def hermes_session_job_cancel(
    job_id: str, hermes_root: Path | None = None
) -> dict[str, Any]:
    """Stop a session job only when this server process still owns it."""
    _reconcile(hermes_root)
    with _lock:
        meta = _load(job_id, hermes_root)
        if not meta:
            return _error(
                "JOB_NOT_FOUND",
                "Hermes session job was not found.",
                "Check the job ID returned by hermes_session_continue.",
            )
        status = str(meta.get("status", "unknown"))
        if status != "running":
            return _redact(
                {
                    "success": True,
                    "job_id": job_id,
                    "status": status,
                    "cancelled": status == "cancelled",
                }
            )
        proc = _processes.get(job_id)
        if proc is None:
            return _redact(
                {
                    "success": True,
                    "job_id": job_id,
                    "status": "orphaned",
                    "cancelled": False,
                }
            )
        if proc.poll() is not None:
            status = "completed" if proc.returncode == 0 else "failed"
            finished = _finish_job(job_id, proc, status, hermes_root)
            return _redact(
                {
                    "success": True,
                    "job_id": job_id,
                    "status": finished.get("status", status),
                    "cancelled": finished.get("status") == "cancelled",
                }
            )
        meta["cancel_requested"] = True
        _save(meta, hermes_root)

    try:
        _terminate(proc)
    except (OSError, subprocess.TimeoutExpired):
        with _lock:
            meta = _load(job_id, hermes_root)
            if meta and meta.get("status") == "running":
                meta.pop("cancel_requested", None)
                _save(meta, hermes_root)
        return _error(
            "CANCEL_FAILED",
            "The running Hermes session process could not be stopped.",
            "Check the local process state and retry while the job is still running.",
        )

    finished = _finish_job(job_id, proc, "cancelled", hermes_root)
    return _redact(
        {
            "success": True,
            "job_id": job_id,
            "status": finished.get("status", "cancelled"),
            "cancelled": finished.get("status") == "cancelled",
        }
    )


def hermes_session_job_result(
    job_id: str, max_chars: int = MAX_RESULT_CHARS, hermes_root: Path | None = None
) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error(
            "JOB_NOT_FOUND",
            "Hermes session job was not found.",
            "Check the job ID returned by hermes_session_continue.",
        )
    if isinstance(max_chars, bool):
        return _error(
            "INVALID_MAX_CHARS",
            "max_chars must be an integer.",
            f"Choose 500 to {MAX_RESULT_CHARS} characters.",
        )
    try:
        cap = max(500, min(int(max_chars), MAX_RESULT_CHARS))
    except (TypeError, ValueError):
        return _error(
            "INVALID_MAX_CHARS",
            "max_chars must be an integer.",
            f"Choose 500 to {MAX_RESULT_CHARS} characters.",
        )
    _, output_path = _paths(job_id, hermes_root)
    try:
        response = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        response = ""
    response = op.redact_output(response)
    truncated = len(response) > cap
    return _redact(
        {
            "success": True,
            "job_id": job_id,
            "session_id": meta.get("session_id"),
            "profile": meta.get("profile", "default"),
            "status": meta.get("status"),
            "return_code": meta.get("return_code"),
            "response": response[:cap],
            "truncated": truncated,
        }
    )
