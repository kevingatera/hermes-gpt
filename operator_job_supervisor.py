"""Durable runner-neutral lifecycle control for Hermes GPT background jobs.

This module owns the small amount of state that must survive an MCP server
restart: process identity, terminal-state arbitration, cancellation intent, and
cursor-based log tails. Backends still own how work is executed and how their
backend-specific result metadata is interpreted.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import operator_job_process as _job_process
import operator_job_store as _job_store
import operator_policy as op

SCHEMA_VERSION = "hermes.job/v1"
MAX_WAIT_SECONDS = 120
MAX_LOG_LINES = 100
MAX_LOG_BYTES = 64 * 1024
MAX_LOG_LINE_CHARS = 2_000
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "timed_out"})
IS_WINDOWS = os.name == "nt"

# Keep these helpers importable for existing diagnostics and tests.
_cmdline_hash = _job_process._cmdline_hash
_procfs_identity = _job_process._procfs_identity
_ps_identity = _job_process._ps_identity
_windows_identity = _job_process._windows_identity

# Keep existing private helper names available to current callers and tests.
_now = _job_store._now
_data_root = _job_store._data_root
_root = _job_store._root
_validate_job_id = _job_store._validate_job_id
_record_path = _job_store._record_path
_lock_path = _job_store._lock_path
_cancel_path = _job_store._cancel_path
_atomic_json = _job_store._atomic_json
_load_json = _job_store._load_json
_confined_relative = _job_store._confined_relative
_resolve_confined = _job_store._resolve_confined


def _record_lock(job_id: str, hermes_root: Path | None = None):
    """Use supervisor platform selection when acquiring the shared record lock."""
    return _job_store.record_lock(job_id, hermes_root, is_windows=IS_WINDOWS)


def process_identity(pid: int) -> dict[str, str] | None:
    """Return a PID-reuse-resistant identity, or None when it cannot be proven."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    if IS_WINDOWS:
        return _windows_identity(pid)
    # On Linux procfs is authoritative for process existence and identity.
    # If /proc/<pid> vanished, do not fall through to an external ``ps`` probe;
    # that only creates a race and makes tests that stub Popen affect identity.
    if Path("/proc").is_dir():
        return _procfs_identity(pid)
    return _ps_identity(pid)


def verify_process(pid: int, expected: dict[str, Any] | None) -> bool | None:
    """Return True for an exact process identity match, False for mismatch.

    None means the current process identity cannot be established. Callers must
    fail closed and must never signal a process in that state.
    """
    if not isinstance(expected, dict):
        return None
    actual = process_identity(pid)
    if actual is None:
        return None
    required = ("platform", "start_token", "cmdline_sha256")
    if any(not expected.get(key) for key in required):
        return None
    return all(str(actual.get(key)) == str(expected.get(key)) for key in required)


def _pid_exists(pid: int) -> bool | None:
    if not isinstance(pid, int) or pid <= 1:
        return False
    if IS_WINDOWS:
        # Without a verified CIM identity Windows cannot distinguish a dead PID
        # from unavailable identity tooling. Preserve uncertainty.
        return True if process_identity(pid) is not None else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def register_job(
    job_id: str,
    *,
    backend: str,
    workspace: Path | str,
    log_path: Path | str | None,
    source_record: Path | str | None,
    cancel_path: Path | str | None = None,
    status: str = "queued",
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Create the canonical durable record without overwriting a terminal job."""
    job_id = _validate_job_id(job_id)
    record_path = _record_path(job_id, hermes_root)
    with _record_lock(job_id, hermes_root):
        existing = _load_json(record_path)
        if existing:
            return existing
        record = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "backend": str(backend or "")[:64],
            "workspace": str(Path(workspace).expanduser().resolve()),
            "status": status,
            "created_at": _now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "process_identity": None,
            "process_verification": "pending",
            "returncode": None,
            "result_summary": "",
            "log_path": _confined_relative(log_path, hermes_root),
            "source_record": _confined_relative(source_record, hermes_root),
            "cancel_path": _confined_relative(cancel_path, hermes_root),
            "cancel_requested": False,
            "finalization_version": 0,
        }
        _atomic_json(record_path, record)
        return record


def mark_running(job_id: str, pid: int, *, hermes_root: Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    identity = process_identity(pid)
    record_path = _record_path(job_id, hermes_root)
    with _record_lock(job_id, hermes_root):
        record = _load_json(record_path)
        if not record:
            raise FileNotFoundError(f"job {job_id!r} is not registered")
        if str(record.get("status")) in TERMINAL_STATES:
            return record
        record.update(
            {
                "status": "running",
                "started_at": record.get("started_at") or _now(),
                "pid": int(pid),
                "process_identity": identity,
                "process_verification": "verified" if identity else "unavailable",
            }
        )
        _atomic_json(record_path, record)
        return record


def terminalize(
    job_id: str,
    status: str,
    *,
    returncode: int | None = None,
    summary: str = "",
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Atomically publish one terminal result; cancellation is monotonic."""
    job_id = _validate_job_id(job_id)
    if status not in TERMINAL_STATES:
        raise ValueError("terminal status is invalid")
    record_path = _record_path(job_id, hermes_root)
    cancel_marker = _cancel_path(job_id, hermes_root)
    with _record_lock(job_id, hermes_root):
        record = _load_json(record_path)
        if not record:
            raise FileNotFoundError(f"job {job_id!r} is not registered")
        cancel_requested = bool(record.get("cancel_requested")) or cancel_marker.exists()
        effective = "cancelled" if cancel_requested else status
        current = str(record.get("status") or "")
        if current in TERMINAL_STATES:
            if current == "cancelled" or current == effective:
                return record
            # First non-cancel terminal writer wins. A late cancel marker is the
            # only allowed monotonic override.
            if not cancel_requested:
                return record
            effective = "cancelled"
        record.update(
            {
                "status": effective,
                "ended_at": record.get("ended_at") or _now(),
                "returncode": returncode,
                "result_summary": op.redact_output(str(summary or ""))[:500],
                "cancel_requested": cancel_requested,
                "finalization_version": int(record.get("finalization_version") or 0) + 1,
            }
        )
        _atomic_json(record_path, record)
        return record


def _import_legacy(job_id: str, hermes_root: Path | None = None) -> dict[str, Any] | None:
    """Expose old records without trusting their pre-v1 numeric PID."""
    base = _data_root(hermes_root)
    candidates = [
        (base / "runner-jobs" / f"{job_id}.json", "state", base / "runner-jobs" / f"{job_id}.jsonl"),
        (base / "codex-jobs" / f"{job_id}.json", "status", base / "codex-jobs" / f"{job_id}.jsonl"),
    ]
    for source, state_field, log_path in candidates:
        meta = _load_json(source)
        if not meta:
            continue
        backend = str(meta.get("backend") or ("codex" if state_field == "status" else "unknown"))
        workspace = str(meta.get("workspace") or meta.get("workdir") or base)
        try:
            record = register_job(
                job_id,
                backend=backend,
                workspace=workspace,
                log_path=log_path,
                source_record=source,
                cancel_path=None,
                status=str(meta.get(state_field) or "unknown"),
                hermes_root=hermes_root,
            )
        except (PermissionError, ValueError):
            return None
        record["pid"] = meta.get("pid") if isinstance(meta.get("pid"), int) else None
        record["process_identity"] = None
        record["process_verification"] = "legacy-unverified"
        record["started_at"] = meta.get("started_at") or meta.get("created_at")
        record["ended_at"] = meta.get("ended_at") or meta.get("completed_at")
        record["returncode"] = meta.get("returncode", meta.get("return_code"))
        with _record_lock(job_id, hermes_root):
            _atomic_json(_record_path(job_id, hermes_root), record)
        return record
    return None


def get_job(job_id: str, *, hermes_root: Path | None = None, reconcile: bool = True) -> dict[str, Any] | None:
    job_id = _validate_job_id(job_id)
    record = _load_json(_record_path(job_id, hermes_root)) or _import_legacy(job_id, hermes_root)
    if not record or not reconcile:
        return record
    return reconcile_job(job_id, hermes_root=hermes_root)


def reconcile_job(job_id: str, *, hermes_root: Path | None = None) -> dict[str, Any] | None:
    """Re-observe a durable worker without ever trusting PID alone."""
    job_id = _validate_job_id(job_id)
    record_path = _record_path(job_id, hermes_root)
    with _record_lock(job_id, hermes_root):
        record = _load_json(record_path)
        if not record:
            return None
        if str(record.get("status")) in TERMINAL_STATES:
            return record
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return record
        expected = record.get("process_identity")
        verified = verify_process(pid, expected if isinstance(expected, dict) else None)
        if verified is True:
            record["process_verification"] = "verified"
        elif verified is False:
            # PID reuse or command identity drift. Do not signal and do not
            # invent a terminal outcome.
            record["process_verification"] = "mismatch"
            record["reconciliation"] = "recorded pid now belongs to a different process; job process is unverifiable"
        else:
            exists = _pid_exists(pid)
            if exists is False:
                record["process_verification"] = "exited-unfinalized"
                record["status"] = "failed"
                record["ended_at"] = record.get("ended_at") or _now()
                record["result_summary"] = "durable worker exited before publishing terminal state"
                record["finalization_version"] = int(record.get("finalization_version") or 0) + 1
            else:
                record["process_verification"] = "unavailable"
                record["reconciliation"] = "process identity cannot be verified; refusing process control"
        _atomic_json(record_path, record)
        return record


def request_cancel(job_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
    """Durably request cancellation and signal only a verified process tree."""
    job_id = _validate_job_id(job_id)
    record_path = _record_path(job_id, hermes_root)
    record = _load_json(record_path) or _import_legacy(job_id, hermes_root)
    if not record:
        return {"success": False, "code": "JOB_NOT_FOUND", "job_id": job_id}

    linked_cancel: Path | None = None
    signalled = False
    process_gone_after_intent = False
    with _record_lock(job_id, hermes_root):
        record = _load_json(record_path)
        if not record:
            return {"success": False, "code": "JOB_NOT_FOUND", "job_id": job_id}
        if str(record.get("status")) in TERMINAL_STATES:
            return {"success": True, "changed": False, "job_id": job_id, "status": record.get("status")}
        pid = record.get("pid")
        expected = record.get("process_identity")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(expected, dict):
            return {
                "success": False,
                "changed": False,
                "code": "JOB_PROCESS_UNVERIFIABLE",
                "job_id": job_id,
                "safe_message": "job process identity is unavailable; refusing to signal a numeric PID alone",
            }
        verified = verify_process(pid, expected)
        if verified is not True:
            record["process_verification"] = "mismatch" if verified is False else "unavailable"
            _atomic_json(record_path, record)
            return {
                "success": False,
                "changed": False,
                "code": "JOB_PROCESS_UNVERIFIABLE",
                "job_id": job_id,
                "safe_message": "job process identity could not be verified; no process was signalled",
            }

        record["cancel_requested"] = True
        _atomic_json(record_path, record)
        marker = {"job_id": job_id, "requested_at": _now()}
        cancel_marker = _cancel_path(job_id, hermes_root)
        _atomic_json(cancel_marker, marker)
        linked_cancel = _resolve_confined(record.get("cancel_path"), hermes_root)
        if linked_cancel is not None:
            _atomic_json(linked_cancel, marker)

        # Re-verify after the durable intent is published and immediately before
        # signalling. Holding the job lock prevents a competing finalizer from
        # racing this identity check into a stale process-control decision.
        verified = verify_process(pid, expected)
        if verified is not True:
            record["cancel_requested"] = False
            record["process_verification"] = "mismatch" if verified is False else "unavailable"
            _atomic_json(record_path, record)
            for marker_path in (cancel_marker, linked_cancel):
                if marker_path is None:
                    continue
                try:
                    marker_path.unlink()
                except OSError:
                    pass
            return {
                "success": False,
                "changed": False,
                "code": "JOB_PROCESS_UNVERIFIABLE",
                "job_id": job_id,
                "safe_message": "job process identity changed before cancellation; no process was signalled",
            }

        # Deliver the initial signal while the identity-protecting lock is still
        # held. This closes the server/server race between verification and the
        # first process-control action.
        if IS_WINDOWS:
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                signalled = completed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                signalled = False
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
                signalled = True
            except (ProcessLookupError, PermissionError, OSError):
                signalled = False

        if not signalled:
            still_same = verify_process(pid, expected)
            process_gone_after_intent = still_same is False or (
                still_same is None and _pid_exists(pid) is False
            )
            if not process_gone_after_intent:
                record["cancel_requested"] = False
                _atomic_json(record_path, record)
                for marker_path in (cancel_marker, linked_cancel):
                    if marker_path is None:
                        continue
                    try:
                        marker_path.unlink()
                    except OSError:
                        pass
                return {
                    "success": False,
                    "changed": False,
                    "code": "JOB_CANCEL_SIGNAL_FAILED",
                    "job_id": job_id,
                    "status": record.get("status"),
                    "safe_message": "verified job process could not be signalled; cancellation was not published",
                }

    if signalled and not IS_WINDOWS:
        deadline = time.monotonic() + 3.0
        current_verification = verify_process(pid, expected)
        while time.monotonic() < deadline and current_verification is True:
            time.sleep(0.05)
            current_verification = verify_process(pid, expected)
        if current_verification is True:
            try:
                os.killpg(pid, signal.SIGKILL)
                hard_kill_sent = True
            except (ProcessLookupError, PermissionError, OSError):
                hard_kill_sent = False
            if not hard_kill_sent and verify_process(pid, expected) is True:
                return {
                    "success": False,
                    "changed": True,
                    "code": "JOB_CANCEL_UNCONFIRMED",
                    "job_id": job_id,
                    "status": "running",
                    "safe_message": "cancellation was requested but the verified job process is still running",
                }
        elif current_verification is None and _pid_exists(pid) is not False:
            return {
                "success": False,
                "changed": True,
                "code": "JOB_CANCEL_UNCONFIRMED",
                "job_id": job_id,
                "status": "running",
                "safe_message": "cancellation was requested but process exit could not be confirmed",
            }

    terminal = terminalize(job_id, "cancelled", hermes_root=hermes_root)
    return {
        "success": True,
        "changed": True,
        "job_id": job_id,
        "status": terminal.get("status"),
        "signalled": signalled,
        "process_gone_after_intent": process_gone_after_intent,
    }


def _tail_log(record: dict[str, Any], cursor: int, max_lines: int, hermes_root: Path | None) -> dict[str, Any]:
    path = _resolve_confined(record.get("log_path"), hermes_root)
    cursor = max(0, int(cursor or 0))
    max_lines = max(1, min(int(max_lines or 50), MAX_LOG_LINES))
    if path is None or not path.is_file():
        return {"cursor": cursor, "next_cursor": cursor, "lines": [], "truncated": False}
    try:
        size = path.stat().st_size
    except OSError:
        return {"cursor": cursor, "next_cursor": cursor, "lines": [], "truncated": False}
    cursor = min(cursor, size)
    try:
        with path.open("rb") as handle:
            handle.seek(cursor)
            chunk = handle.read(MAX_LOG_BYTES + 1)
    except OSError:
        return {"cursor": cursor, "next_cursor": cursor, "lines": [], "truncated": False}
    byte_truncated = len(chunk) > MAX_LOG_BYTES
    if byte_truncated:
        chunk = chunk[:MAX_LOG_BYTES]
    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        return {"cursor": cursor, "next_cursor": cursor, "lines": [], "truncated": byte_truncated}
    complete = chunk[: last_newline + 1]
    raw_segments = complete.splitlines(keepends=True)
    line_truncated = len(raw_segments) > max_lines
    selected = raw_segments[:max_lines]
    consumed = sum(len(segment) for segment in selected)
    rendered: list[Any] = []
    for segment in selected:
        raw = segment.rstrip(b"\r\n")
        text = raw.decode("utf-8", errors="replace")
        try:
            value = json.loads(text)
        except ValueError:
            value = op.redact_output(text)[:MAX_LOG_LINE_CHARS]
        else:
            value = _redact_json(value)
        rendered.append(value)
    return {
        "cursor": cursor,
        "next_cursor": cursor + consumed,
        "lines": rendered,
        "truncated": bool(byte_truncated or line_truncated),
    }


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return op.redact_output(value)[:MAX_LOG_LINE_CHARS]
    return value


def status_payload(
    job_id: str,
    *,
    cursor: int = 0,
    max_lines: int = 50,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    record = get_job(job_id, hermes_root=hermes_root, reconcile=True)
    if not record:
        return {"success": False, "code": "JOB_NOT_FOUND", "job_id": job_id}
    return {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "job": record,
        "log": _tail_log(record, cursor, max_lines, hermes_root),
    }


def wait_payload(
    job_id: str,
    *,
    cursor: int = 0,
    wait_seconds: int = MAX_WAIT_SECONDS,
    max_lines: int = 50,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    wait_seconds = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + wait_seconds
    while True:
        payload = status_payload(job_id, cursor=cursor, max_lines=max_lines, hermes_root=hermes_root)
        if not payload.get("success"):
            return payload
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        if str(job.get("status")) in TERMINAL_STATES:
            payload["wait"] = {"timed_out": False, "wait_seconds": wait_seconds}
            return payload
        if time.monotonic() >= deadline:
            payload["wait"] = {"timed_out": True, "wait_seconds": wait_seconds}
            return payload
        time.sleep(0.1)


def _audit(tool: str, policy: op.OperatorPolicy, job_id: str, summary: str) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=True,
            changed=False,
            summary=summary,
            extra={"job_id": job_id},
        )
    except Exception:  # noqa: BLE001, S110 - A failed audit write must not alter job supervision.
        pass


def hermes_job_status(
    job_id: str,
    cursor: int = 0,
    max_lines: int = 50,
    hermes_root: Path | None = None,
) -> str:
    """Read durable status plus a cursor-based bounded log tail."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        payload = status_payload(job_id, cursor=cursor, max_lines=max_lines, hermes_root=hermes_root)
        _audit("hermes_job_status", policy, str(job_id), "read durable job status")
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError) as exc:
        return json.dumps({"success": False, "code": "JOB_STATUS_ERROR", "safe_message": op.redact_output(str(exc))[:300]}, indent=2)


def hermes_job_wait(
    job_id: str,
    cursor: int = 0,
    wait_seconds: int = MAX_WAIT_SECONDS,
    max_lines: int = 50,
    hermes_root: Path | None = None,
) -> str:
    """Long-poll a job for up to 120 seconds, returning early when terminal."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        payload = wait_payload(
            job_id,
            cursor=cursor,
            wait_seconds=wait_seconds,
            max_lines=max_lines,
            hermes_root=hermes_root,
        )
        _audit("hermes_job_wait", policy, str(job_id), "waited for durable job state")
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError) as exc:
        return json.dumps({"success": False, "code": "JOB_WAIT_ERROR", "safe_message": op.redact_output(str(exc))[:300]}, indent=2)


__all__ = [
    "MAX_WAIT_SECONDS",
    "SCHEMA_VERSION",
    "TERMINAL_STATES",
    "get_job",
    "hermes_job_status",
    "hermes_job_wait",
    "mark_running",
    "process_identity",
    "reconcile_job",
    "register_job",
    "request_cancel",
    "status_payload",
    "terminalize",
    "verify_process",
    "wait_payload",
]
