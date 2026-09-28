"""Private file-backed state for Hermes session-control jobs."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    return _data_root(hermes_root) / "session-jobs"


def _data_root(hermes_root: Path | None = None) -> Path:
    configured_root = hermes_root or Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    )
    base = op.normalize_hermes_data_root(configured_root)
    return Path(base or Path.home() / ".hermes")


def _paths(job_id: str, hermes_root: Path | None = None) -> tuple[Path, Path]:
    root = _root(hermes_root)
    return root / f"{job_id}.json", root / f"{job_id}.txt"


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
