"""Read-only path, file, and SQLite helpers for Mission Control surfaces."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op
from hermes_gpt.missions.common import _resolve_root


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite database read-only (``file:...?mode=ro``). Never creates."""
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path.name}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _state_db(profile_home: Path) -> Path:
    return profile_home / "state.db"


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _cron_executions_db(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "executions.db"


def _profile_home(profile: str, hermes_root: Path | None) -> Path:
    return op.resolve_profile_home(profile, hermes_root)


def _iter_profiles(hermes_root: Path | None) -> Iterable[str]:
    return op.list_existing_profiles(hermes_root)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, returning None if missing/unreadable. Never denied paths."""
    if not path.exists() or op.is_denied_path(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_json_list_file(path: Path) -> list[Any]:
    if not path.exists() or op.is_denied_path(path):
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    return []


def _logs_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "logs"


def _kanban_boards_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "kanban" / "boards"


def _codex_jobs_dir(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "codex-jobs"


def _fleet_authority_manifest(root: Path) -> Path:
    """Resolve the fleet-authority manifest under ``hermes_root`` (hermetic).

    Mirrors ``operator_fleet._manifest_path`` but roots the lookup at the
    resolved hermes_root rather than the ambient ``HERMES_HOME`` env / home
    dir, so mission reads are hermetic and honor the passed root (design D5).
    """
    return root / "config" / "fleet-authority.json"


def _vault_db_path(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "hermes-vault-data" / "vault.db"


def _vault_access_requests(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "hermes-vault-data" / "access_requests.json"


def _interrupted_turns_path(hermes_root: Path | None) -> Path:
    return _resolve_root(hermes_root) / "desktop" / "interrupted_turns.json"


def _action_items_path() -> Path:
    return Path.home() / "nexus-wiki" / "ops" / "state" / "action-items.json"


def _errors_log(hermes_root: Path | None) -> Path:
    return _logs_dir(hermes_root) / "errors.log"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"
