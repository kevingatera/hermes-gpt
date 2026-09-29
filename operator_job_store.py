"""Confined storage helpers for durable Hermes job records."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_root(hermes_root: Path | None = None) -> Path:
    configured = hermes_root or Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    )
    normalized = op.normalize_hermes_data_root(configured)
    return Path(normalized or configured).expanduser().resolve()


def _root(hermes_root: Path | None = None) -> Path:
    return _data_root(hermes_root) / "job-supervisor"


def _validate_job_id(job_id: str) -> str:
    value = str(job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(value):
        raise ValueError("job_id has an invalid format")
    return value


def _record_path(job_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{_validate_job_id(job_id)}.json"


def _lock_path(job_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{_validate_job_id(job_id)}.lock"


def _cancel_path(job_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{_validate_job_id(job_id)}.cancel.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
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


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@contextlib.contextmanager
def record_lock(
    job_id: str,
    hermes_root: Path | None = None,
    *,
    is_windows: bool | None = None,
) -> Iterator[None]:
    """Serialize writers across independently restarted server processes."""
    windows = os.name == "nt" if is_windows is None else is_windows
    path = _lock_path(job_id, hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+b")
    try:
        try:
            path.chmod(0o600)
        except OSError:
            pass
        if windows:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _confined_relative(
    path: Path | str | None, hermes_root: Path | None = None
) -> str | None:
    if path is None or str(path) == "":
        return None
    base = _data_root(hermes_root)
    resolved = Path(path).expanduser().resolve()
    try:
        return str(resolved.relative_to(base))
    except ValueError as exc:
        raise PermissionError(
            "job artifact path is outside the Hermes data root"
        ) from exc


def _resolve_confined(
    relative: str | None, hermes_root: Path | None = None
) -> Path | None:
    if not relative:
        return None
    base = _data_root(hermes_root)
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise PermissionError("job artifact path escapes the Hermes data root") from exc
    return resolved
