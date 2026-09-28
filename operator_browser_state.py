"""Private descriptor storage and executable lookup for browser sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SESSION_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _browser_executable(hermes_root: Path | None) -> str:
    """Resolve an installed agent-browser CLI without invoking installers."""
    candidates: list[str] = []
    if hermes_root is not None:
        node_bin = Path(hermes_root).expanduser() / "node" / "bin"
        for name in ("agent-browser", "agent-browser.exe", "agent-browser.cmd"):
            candidates.append(str(node_bin / name))
    located = shutil.which("agent-browser")
    if located:
        candidates.append(located)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    raise FileNotFoundError("agent-browser is not installed or is not available on PATH")


def browser_available(hermes_root: Path | None) -> bool:
    try:
        _browser_executable(hermes_root)
    except FileNotFoundError:
        return False
    return True


def _state_path(task_home: Path) -> Path:
    home = Path(task_home).expanduser().resolve()
    state_dir = home.parent / ".managed-browser" / home.name
    return state_dir / f"{home.name}.json"


def _legacy_state_path(task_home: Path) -> Path:
    """Return the pre-task-directory descriptor path for migration."""
    home = Path(task_home).expanduser().resolve()
    return home.parent / ".managed-browser" / f"{home.name}.json"


def _socket_path(task_id: str) -> Path:
    # The CLI appends session and socket names, so keep this base path short.
    return Path("/tmp").resolve() / f"hgpt-{task_id}"


def _is_private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False
    return stat.S_IMODE(info.st_mode) == 0o700


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PermissionError("Private browser state directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise PermissionError("Private browser state directory is not owned by this user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
        if not _is_private_directory(path):
            raise PermissionError("Private browser state directory permissions could not be set")


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _ensure_private_directory(path.parent.parent)
    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def _read_state(path: Path, *, expected_path: Path | None = None) -> dict[str, Any] | None:
    raw_path = Path(path).expanduser()
    if raw_path.is_symlink() or not _is_private_directory(raw_path.parent):
        return None
    try:
        file_info = raw_path.stat()
        if (hasattr(os, "getuid") and file_info.st_uid != os.getuid()) or stat.S_IMODE(file_info.st_mode) & 0o077:
            return None
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    session_name = str(raw.get("session_name") or "")
    task_id = str(raw.get("task_id") or "")
    if not _TASK_ID_RE.fullmatch(task_id) or session_name != f"hg_{task_id[:16]}":
        return None
    if not _SESSION_NAME_RE.fullmatch(session_name):
        return None
    try:
        task_home = Path(str(raw.get("task_home") or "")).expanduser().resolve(strict=True)
        state_path = Path(path).expanduser().resolve(strict=True)
        socket_dir = Path(str(raw.get("socket_dir") or "")).expanduser().resolve()
        expected_state = (expected_path or _state_path(task_home)).resolve(strict=True)
        expected_socket_dir = _socket_path(task_id).resolve()
        hermes_root = Path(str(raw.get("hermes_root") or "")).expanduser() if raw.get("hermes_root") else None
        executable = Path(str(raw.get("executable") or "")).expanduser().resolve(strict=True)
        installed_executable = Path(_browser_executable(hermes_root)).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    browser_source = raw.get("browser_source", "isolated")
    if browser_source not in {"isolated", "hermes_profile"}:
        return None
    if browser_source == "hermes_profile":
        port = raw.get("cdp_port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
    if (
        task_home.name != task_id
        or state_path != expected_state
        or socket_dir != expected_socket_dir
        or (raw.get("status") != "closed" and not socket_dir.is_dir())
        or executable != installed_executable
        or raw.get("status") not in {"starting", "running", "closed"}
    ):
        return None
    return raw


def _migrate_legacy_state(task_home: Path) -> None:
    """Move a valid older descriptor into a private directory for this task."""
    current_path = _state_path(task_home)
    legacy_path = _legacy_state_path(task_home)
    if current_path.exists() or not legacy_path.is_file():
        return
    _ensure_private_directory(legacy_path.parent)
    if not _read_state(legacy_path, expected_path=legacy_path):
        return
    _ensure_private_directory(current_path.parent)
    try:
        legacy_path.replace(current_path)
    except FileNotFoundError:
        # Another process may have migrated the descriptor first.
        return


def _read_task_state(task_home: Path) -> dict[str, Any] | None:
    _migrate_legacy_state(task_home)
    return _read_state(_state_path(task_home))


def browser_state_file(task_home: Path) -> Path:
    """Return the validated state file path for the child MCP server config."""
    home = Path(task_home).expanduser().resolve(strict=True)
    _migrate_legacy_state(home)
    path = _state_path(home)
    if not _read_state(path):
        raise FileNotFoundError("Managed browser state is unavailable")
    return path.resolve(strict=True)


def delete_browser_state(task_home: Path) -> None:
    """Remove the bridge descriptor and let the idle daemon expire naturally."""
    home = Path(task_home).expanduser().resolve()
    if not _TASK_ID_RE.fullmatch(home.name):
        return
    state_path = _state_path(home)
    state_path.unlink(missing_ok=True)
    try:
        state_path.parent.rmdir()
    except OSError:
        pass
    _legacy_state_path(home).unlink(missing_ok=True)


__all__ = ["browser_available", "browser_state_file", "delete_browser_state"]
