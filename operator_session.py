"""Bounded asynchronous jobs for continuing existing Hermes sessions."""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import operator_live_events as live_events
import operator_policy as op

ENABLE_SESSION_CONTROL_ENV = "HERMES_GPT_ENABLE_SESSION_CONTROL"
SESSION_ALLOWED_PROFILES_ENV = "HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES"
MAX_PROMPT_CHARS = 65_536
MAX_RESULT_CHARS = 24_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 3_600
PROGRESS_EVENT_INTERVAL_SECONDS = 15
SESSION_JOB_TOPIC = "session-job"
SESSION_ID_LINE_RE = re.compile(r"^\s*session_id:\s*([A-Za-z0-9_.:-]{1,256})\s*$", re.IGNORECASE)
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")

_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
_active_sessions: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    return _data_root(hermes_root) / "session-jobs"


def _data_root(hermes_root: Path | None = None) -> Path:
    configured_root = hermes_root or Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    )
    base = op.normalize_hermes_data_root(
        configured_root
    )
    return Path(base or Path.home() / ".hermes")


def _paths(job_id: str, hermes_root: Path | None = None) -> tuple[Path, Path]:
    root = _root(hermes_root)
    return root / f"{job_id}.json", root / f"{job_id}.txt"


def _error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(
        layer="session_control", code=code, safe_message=message, suggested_action=action
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return op.redact_output(value)
    return value


def _save(meta: dict[str, Any], hermes_root: Path | None = None) -> None:
    path, _ = _paths(meta["job_id"], hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


def _load(job_id: str, hermes_root: Path | None = None) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return None
    path, _ = _paths(job_id, hermes_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _hermes_executable(agent_root: Path | None = None) -> str:
    if agent_root:
        candidate = Path(agent_root) / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "hermes.exe" if os.name == "nt" else "hermes"
        )
        if candidate.is_file():
            return str(candidate)
    return shutil.which("hermes") or "hermes"


def validate_session_profile(
    profile: str, hermes_root: Path | None = None
) -> str | dict[str, Any]:
    """Require an explicitly scoped profile for Hermes task execution."""
    try:
        safe_profile = op.validate_profile_name(profile)
    except (TypeError, ValueError):
        return _error(
            "INVALID_PROFILE",
            "profile is not a valid Hermes profile name.",
            "Choose a valid profile name.",
        )

    configured = [
        item.strip()
        for item in os.environ.get(SESSION_ALLOWED_PROFILES_ENV, "").split(",")
        if item.strip()
    ]
    if not configured:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "No Hermes profiles are authorized for session control.",
            f"Set {SESSION_ALLOWED_PROFILES_ENV} to the restricted profile names this server may run.",
        )
    if "*" in configured:
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist does not accept wildcards.",
            f"Set {SESSION_ALLOWED_PROFILES_ENV} to explicit Hermes profile names.",
        )
    try:
        allowed = {op.validate_profile_name(item) for item in configured}
    except (TypeError, ValueError):
        return _error(
            "SESSION_PROFILE_ALLOWLIST_INVALID",
            "The session-control profile allowlist contains an invalid profile name.",
            f"Correct {SESSION_ALLOWED_PROFILES_ENV} and retry.",
        )
    if safe_profile not in allowed:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "The requested Hermes profile is not authorized for session control.",
            f"Choose a profile listed in {SESSION_ALLOWED_PROFILES_ENV}.",
        )

    try:
        op.OperatorPolicy().require_profile(safe_profile, _data_root(hermes_root))
    except FileNotFoundError:
        return _error(
            "SESSION_PROFILE_NOT_FOUND",
            "The requested Hermes profile does not exist.",
            "Create and configure the restricted Hermes profile before enabling session control.",
        )
    except PermissionError:
        return _error(
            "SESSION_PROFILE_NOT_ALLOWED",
            "The requested Hermes profile is not authorized by Operator policy.",
            f"Allow the profile with {op.OPERATOR_ALLOWED_PROFILES_ENV} as well.",
        )
    return safe_profile


def _validate_start(
    session_id: str,
    prompt: str,
    timeout: int,
    profile: str = "default",
    hermes_root: Path | None = None,
) -> tuple[str, str, int, str] | dict[str, Any]:
    if not op.env_truthy(ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id.strip()) > 256:
        return _error("INVALID_SESSION_ID", "session_id must contain 1 to 256 characters.", "Use an ID returned by hermes_session_list.")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("INVALID_PROMPT", "prompt must not be empty.", "Provide the next instruction for the existing Hermes session.")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _error("PROMPT_TOO_LARGE", f"prompt exceeds the {MAX_PROMPT_CHARS}-character limit.", "Send a shorter prompt.")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return _error("INVALID_TIMEOUT", "timeout must be an integer number of seconds.", f"Choose {MIN_TIMEOUT} to {MAX_TIMEOUT} seconds.")
    safe_profile = validate_session_profile(profile, hermes_root)
    if isinstance(safe_profile, dict):
        return safe_profile
    return session_id.strip(), prompt, max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT)), safe_profile


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


def hermes_session_continue(
    session_id: str,
    prompt: str,
    timeout: int = 900,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Start one bounded turn in an existing Hermes session."""
    checked = _validate_start(session_id, prompt, timeout, profile, hermes_root)
    if isinstance(checked, dict):
        return checked
    safe_id, safe_prompt, safe_timeout, safe_profile = checked
    if model is not None and (not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model)):
        return _error("INVALID_MODEL", "model must be a provider/model identifier of at most 256 characters.", "Choose a model ID accepted by the Hermes CLI.")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS
    ):
        return _error(
            "INVALID_REASONING_EFFORT",
            "reasoning_effort is not supported by the Hermes CLI.",
            f"Choose one of: {', '.join(sorted(REASONING_EFFORTS))}.",
        )
    executable = _hermes_executable(agent_root)
    argv = [executable, "--resume", safe_id]
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["--reasoning", reasoning_effort])
    argv.extend(["--query-file", "-", "--oneshot", "-Q"])
    child_env = os.environ.copy()
    base_home = (
        Path(hermes_root)
        if hermes_root is not None
        else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    profile_home = op.resolve_profile_home(safe_profile, base_home)
    child_env["HERMES_HOME"] = str(profile_home)
    child_env["HERMES_PROFILE"] = safe_profile
    return start_managed_session_job(
        argv=argv,
        prompt=safe_prompt,
        timeout=safe_timeout,
        profile=safe_profile,
        hermes_root=hermes_root,
        child_env=child_env,
        cwd=profile_home,
        active_key=f"{safe_profile}:{safe_id}",
        metadata={
            "session_id": safe_id,
            "model": model or None,
            "reasoning_effort": reasoning_effort or None,
        },
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
    meta.update({
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
    })
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
    meta.update({"status": "running", "started_at": _now(), "pid": proc.pid, "active_key": active_key})
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

    threading.Thread(target=_feed_prompt, name=f"hermes-input-{job_id[:8]}", daemon=True).start()
    threading.Thread(
        target=_watch,
        args=(job_id, proc, output, stderr, safe_timeout, hermes_root),
        name=f"hermes-watch-{job_id[:8]}",
        daemon=True,
    ).start()
    return _redact({
        "success": True,
        "job_id": job_id,
        "task_id": str(meta.get("task_id") or "") or None,
        "session_id": str(meta.get("session_id") or "") or None,
        "status": "running",
        "model": str(meta.get("model") or "") or None,
    })


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
        if meta.get("status") not in {"completed", "failed", "timed_out", "cancelled", "orphaned"}:
            if meta.get("cancel_requested"):
                status = "cancelled"
            meta.update({"status": status, "return_code": proc.poll(), "ended_at": _now()})
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
        session_id = _session_id_from_stderr(_paths(job_id, hermes_root)[1].with_suffix(".stderr.txt"))
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
    if not isinstance(session_id, str) or not SESSION_ID_LINE_RE.fullmatch(f"session_id: {session_id}"):
        return ""
    return session_id


def _recover_task_session_id(meta: dict[str, Any]) -> bool:
    """Recover a one-shot task's session ID from Hermes' structured usage report."""
    if not meta.get("task_id") or meta.get("session_id") or not meta.get("usage_file"):
        return False
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
            meta.update({
                "status": "orphaned",
                "ended_at": _now(),
                "reconciliation": "server restarted; process ownership could not be proven",
            })
            _save(meta, hermes_root)
            _publish_job_event(
                job_id,
                "orphaned",
                session_id=str(meta.get("session_id", "")),
                hermes_root=hermes_root,
                task_id=str(meta.get("task_id") or ""),
            )


def hermes_session_job_status(job_id: str, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error("JOB_NOT_FOUND", "Hermes session job was not found.", "Check the job ID returned by hermes_session_continue.")
    public_meta = dict(meta)
    public_meta.pop("active_key", None)
    if public_meta.get("task_id"):
        if _recover_task_session_id(meta):
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
            return _redact({
                "success": True,
                "job_id": job_id,
                "status": status,
                "cancelled": status == "cancelled",
            })
        proc = _processes.get(job_id)
        if proc is None:
            return _redact({
                "success": True,
                "job_id": job_id,
                "status": "orphaned",
                "cancelled": False,
            })
        if proc.poll() is not None:
            status = "completed" if proc.returncode == 0 else "failed"
            finished = _finish_job(job_id, proc, status, hermes_root)
            return _redact({
                "success": True,
                "job_id": job_id,
                "status": finished.get("status", status),
                "cancelled": finished.get("status") == "cancelled",
            })
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
    return _redact({
        "success": True,
        "job_id": job_id,
        "status": finished.get("status", "cancelled"),
        "cancelled": finished.get("status") == "cancelled",
    })


def hermes_session_job_result(job_id: str, max_chars: int = MAX_RESULT_CHARS, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error("JOB_NOT_FOUND", "Hermes session job was not found.", "Check the job ID returned by hermes_session_continue.")
    if isinstance(max_chars, bool):
        return _error("INVALID_MAX_CHARS", "max_chars must be an integer.", f"Choose 500 to {MAX_RESULT_CHARS} characters.")
    try:
        cap = max(500, min(int(max_chars), MAX_RESULT_CHARS))
    except (TypeError, ValueError):
        return _error("INVALID_MAX_CHARS", "max_chars must be an integer.", f"Choose 500 to {MAX_RESULT_CHARS} characters.")
    _, output_path = _paths(job_id, hermes_root)
    try:
        response = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        response = ""
    response = op.redact_output(response)
    truncated = len(response) > cap
    return _redact({
        "success": True,
        "job_id": job_id,
        "session_id": meta.get("session_id"),
        "profile": meta.get("profile", "default"),
        "status": meta.get("status"),
        "return_code": meta.get("return_code"),
        "response": response[:cap],
        "truncated": truncated,
    })
