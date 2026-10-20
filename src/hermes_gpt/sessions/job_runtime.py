"""Launch, supervise, and reconcile Hermes session child processes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from hermes_gpt.workspace import live_events
from hermes_gpt.policy import authorization as op
from hermes_gpt.sessions.job_store import _load, _now, _paths, _root, _save

ENABLE_SESSION_CONTROL_ENV = "HERMES_GPT_ENABLE_SESSION_CONTROL"
SESSION_ALLOWED_PROFILES_ENV = "HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES"
MAX_PROMPT_CHARS = 65_536
MAX_RESULT_CHARS = 24_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 3_600
PROGRESS_EVENT_INTERVAL_SECONDS = 15
SESSION_JOB_TOPIC = "session-job"
SESSION_ID_LINE_RE = re.compile(
    r"^\s*session_id:\s*([A-Za-z0-9_.:-]{1,256})\s*$", re.IGNORECASE
)
REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")

_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
_active_sessions: dict[str, str] = {}


def _error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(
        layer="session_control",
        code=code,
        safe_message=message,
        suggested_action=action,
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return op.redact_output(value)
    return value


def _hermes_executable(agent_root: Path | None = None) -> str:
    if agent_root:
        candidate = (
            Path(agent_root)
            / "venv"
            / ("Scripts" if os.name == "nt" else "bin")
            / ("hermes.exe" if os.name == "nt" else "hermes")
        )
        if candidate.is_file():
            return str(candidate)
    return shutil.which("hermes") or "hermes"


def _publish_job_event(
    job_id: str,
    kind: str,
    *,
    session_id: str,
    hermes_root: Path | None,
    elapsed_seconds: int = 0,
    task_id: str = "",
) -> None:
    """Publish bounded lifecycle metadata; job files remain authoritative."""
    try:
        live_events.publish_event(
            topic=SESSION_JOB_TOPIC,
            kind=kind,
            subject_type="session_job",
            subject_id=job_id,
            source="session_control",
            event_id=f"session-job:{job_id}:{kind}:{elapsed_seconds}",
            payload={
                "status": kind,
                "session_id": session_id,
                "elapsed_seconds": elapsed_seconds,
                **({"task_id": task_id} if task_id else {}),
            },
            hermes_root=hermes_root,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # The job journal is authoritative; an event-store outage must not
        # change whether a Hermes task starts or reaches its terminal state.
        return


def _publish_progress_event(
    job_id: str,
    *,
    hermes_root: Path | None,
    elapsed_seconds: int,
) -> None:
    with _lock:
        meta = _load(job_id, hermes_root) or {}
        if meta.get("status") != "running" or meta.get("cancel_requested"):
            return
        _publish_job_event(
            job_id,
            "progress",
            session_id=str(meta.get("session_id", "")),
            hermes_root=hermes_root,
            elapsed_seconds=elapsed_seconds,
            task_id=str(meta.get("task_id") or ""),
        )


def start_managed_session_job(
    *,
    argv: list[str],
    prompt: str,
    timeout: int,
    profile: str,
    hermes_root: Path | None,
    child_env: dict[str, str],
    cwd: Path,
    active_key: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Start a fixed Hermes CLI turn through the durable session-job lifecycle.

    Prompt text travels over stdin, never in process arguments or job metadata.
    Callers remain responsible for constructing a fixed, scoped command and env.
    """
    safe_timeout = max(MIN_TIMEOUT, min(int(timeout), MAX_TIMEOUT))
    job_id = uuid4().hex
    meta: dict[str, Any] = dict(metadata)
    meta.update(
        {
            "job_id": job_id,
            "session_id": str(metadata.get("session_id") or ""),
            "profile": profile,
            "status": "starting",
            "created_at": _now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "return_code": None,
            "timeout": safe_timeout,
            "prompt_len": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
    )
    # Link the durable job to its originating MCP call without storing inputs.
    from hermes_gpt.server.request_log import request_id
    trace = request_id.get()
    if trace:
        meta["request_id"] = trace
    _, output_path = _paths(job_id, hermes_root)
    stderr_path = output_path.with_suffix(".stderr.txt")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = output_path.open("w", encoding="utf-8")
    stderr = stderr_path.open("w", encoding="utf-8")
    for path in (output_path, stderr_path):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    with _lock:
        active_job = _active_sessions.get(active_key)
        if active_job:
            output.close()
            stderr.close()
            output_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
            return _error(
                "SESSION_BUSY",
                "This Hermes task already has a running job.",
                f"Wait for job {active_job} to finish before sending another turn.",
            )
        _active_sessions[active_key] = job_id
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=stderr,
            text=True,
            shell=False,
            cwd=str(cwd),
            env=child_env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as exc:
        output.close()
        stderr.close()
        output_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        with _lock:
            if _active_sessions.get(active_key) == job_id:
                _active_sessions.pop(active_key, None)
        return _error(
            "HERMES_START_FAILED",
            op.redact_output(str(exc)),
            "Check the Hermes CLI installation, provider authentication, workspace, and OS confinement.",
        )
    meta.update(
        {
            "status": "running",
            "started_at": _now(),
            "pid": proc.pid,
            "active_key": active_key,
        }
    )
    try:
        _save(meta, hermes_root)
    except OSError as exc:
        try:
            _terminate(proc)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
                proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
        finally:
            output.close()
            stderr.close()
            output_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
            with _lock:
                if _active_sessions.get(active_key) == job_id:
                    _active_sessions.pop(active_key, None)
        return _error(
            "JOB_PERSIST_FAILED",
            op.redact_output(str(exc)),
            "Check permissions on the Hermes task data root and retry.",
        )
    with _lock:
        _processes[job_id] = proc
    _publish_job_event(
        job_id,
        "running",
        session_id=str(meta.get("session_id") or ""),
        hermes_root=hermes_root,
        task_id=str(meta.get("task_id") or ""),
    )

    def _feed_prompt() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    threading.Thread(
        target=_feed_prompt, name=f"hermes-input-{job_id[:8]}", daemon=True
    ).start()
    threading.Thread(
        target=_watch,
        args=(job_id, proc, output, stderr, safe_timeout, hermes_root),
        name=f"hermes-watch-{job_id[:8]}",
        daemon=True,
    ).start()
    return _redact(
        {
            "success": True,
            "job_id": job_id,
            "task_id": str(meta.get("task_id") or "") or None,
            "session_id": str(meta.get("session_id") or "") or None,
            "status": "running",
            "model": str(meta.get("model") or "") or None,
            "reasoning_effort": str(meta.get("reasoning_effort") or "") or None,
        }
    )


def _finish_job(
    job_id: str,
    proc: subprocess.Popen[str],
    status: str,
    hermes_root: Path | None,
) -> dict[str, Any]:
    publish_status = ""
    session_id = ""
    with _lock:
        meta = _load(job_id, hermes_root) or {"job_id": job_id}
        if meta.get("status") not in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "orphaned",
        }:
            if meta.get("cancel_requested"):
                status = "cancelled"
            meta.update(
                {"status": status, "return_code": proc.poll(), "ended_at": _now()}
            )
            _save(meta, hermes_root)
            publish_status = status
        _processes.pop(job_id, None)
        session_id = str(meta.get("session_id", ""))
        profile = str(meta.get("profile", "default") or "default")
        active_key = str(meta.get("active_key") or f"{profile}:{session_id}")
        if _active_sessions.get(active_key) == job_id:
            _active_sessions.pop(active_key, None)
    if publish_status:
        _publish_job_event(
            job_id,
            publish_status,
            session_id=session_id,
            hermes_root=hermes_root,
            task_id=str(meta.get("task_id") or ""),
        )
    return meta


def _watch(
    job_id: str,
    proc: subprocess.Popen[str],
    output: Any,
    stderr: Any,
    timeout: int,
    hermes_root: Path | None,
) -> None:
    started = time.monotonic()
    deadline = started + timeout
    progress_interval = max(0.1, PROGRESS_EVENT_INTERVAL_SECONDS)
    next_progress = started + progress_interval
    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                _terminate(proc)
                status = "timed_out"
                break
            wait_for = min(remaining, max(0.05, next_progress - now))
            try:
                proc.wait(timeout=wait_for)
                status = "completed" if proc.returncode == 0 else "failed"
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if now >= deadline:
                    _terminate(proc)
                    status = "timed_out"
                    break
                if now >= next_progress:
                    _publish_progress_event(
                        job_id,
                        hermes_root=hermes_root,
                        elapsed_seconds=int(now - started),
                    )
                    next_progress = now + progress_interval
    finally:
        output.close()
        stderr.close()
    current = _load(job_id, hermes_root) or {}
    if not current.get("session_id"):
        session_id = _session_id_from_stderr(
            _paths(job_id, hermes_root)[1].with_suffix(".stderr.txt")
        )
        if not session_id and current.get("usage_file"):
            session_id = _session_id_from_usage_file(Path(str(current["usage_file"])))
        if session_id:
            with _lock:
                current = _load(job_id, hermes_root) or {"job_id": job_id}
                current["session_id"] = session_id
                _save(current, hermes_root)
    _finish_job(job_id, proc, status, hermes_root)


def _session_id_from_stderr(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        match = SESSION_ID_LINE_RE.fullmatch(line)
        if match:
            return match.group(1)
    return ""


def _session_id_from_usage_file(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_LINE_RE.fullmatch(
        f"session_id: {session_id}"
    ):
        return ""
    return session_id


def _recover_task_session_id(
    meta: dict[str, Any], hermes_root: Path | None = None
) -> bool:
    """Recover a task session ID from Hermes' stderr line or an older usage report."""
    if not meta.get("task_id") or meta.get("session_id"):
        return False
    _job_path, output_path = _paths(str(meta.get("job_id") or ""), hermes_root)
    session_id = _session_id_from_stderr(output_path.with_suffix(".stderr.txt"))
    if not session_id and meta.get("usage_file"):
        session_id = _session_id_from_usage_file(Path(str(meta["usage_file"])))
    if not session_id:
        return False
    meta["session_id"] = session_id
    return True


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process_group = os.getpgid(proc.pid)
            os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if os.name != "nt":
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()
    if proc.poll() is None:
        proc.wait()


def _reconcile(hermes_root: Path | None = None) -> None:
    root = _root(hermes_root)
    if not root.exists():
        return
    with _lock:
        owned = set(_processes)
    for path in root.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job_id = str(meta.get("job_id", ""))
        if meta.get("status") == "running" and job_id not in owned:
            meta.update(
                {
                    "status": "orphaned",
                    "ended_at": _now(),
                    "reconciliation": "server restarted; process ownership could not be proven",
                }
            )
            _save(meta, hermes_root)
            _publish_job_event(
                job_id,
                "orphaned",
                session_id=str(meta.get("session_id", "")),
                hermes_root=hermes_root,
                task_id=str(meta.get("task_id") or ""),
            )
