"""Manage isolated agent-browser sessions shared by Hermes and MCP clients."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import operator_browser_tabs as browser_tabs
import operator_policy as op
from operator_browser_profiles import validate_local_cdp_port
from operator_browser_state import (
    _TASK_ID_RE,
    _browser_executable,
    _ensure_private_directory,
    _read_state,
    _read_task_state,
    _socket_path,
    _state_path,
    _write_state,
    browser_available,
    browser_state_file,
    delete_browser_state,
)

_MAX_OUTPUT_CHARS = 24_000
_COMMAND_TIMEOUT_SECONDS = 45
_IDLE_TIMEOUT_MS = 86_400_000
_MAX_URL_CHARS = 4_096
_MAX_TEXT_CHARS = 16_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "code": code, "safe_message": message}


def _safe_browser_env(socket_dir: Path, hermes_root: Path | None) -> dict[str, str]:
    """Pass browser runtime settings only; do not forward provider credentials."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "TEMP",
            "APPDATA",
            "LOCALAPPDATA",
            "SYSTEMROOT",
            "WINDIR",
        }
    }
    path_entries = [item for item in env.get("PATH", "").split(os.pathsep) if item]
    if hermes_root is not None:
        node_bin = str(Path(hermes_root).expanduser() / "node" / "bin")
        if Path(node_bin).is_dir() and node_bin not in path_entries:
            path_entries.insert(0, node_bin)
    env["PATH"] = os.pathsep.join(path_entries)
    env["AGENT_BROWSER_SOCKET_DIR"] = str(socket_dir)
    env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(_IDLE_TIMEOUT_MS)
    return env


def _run(
    state: dict[str, Any],
    command: str,
    args: list[str] | None = None,
    *,
    headed: bool = False,
) -> dict[str, Any]:
    argv = [str(state["executable"])]
    if state.get("browser_source") == "hermes_profile":
        argv += ["--cdp", str(state["cdp_port"])]
    argv += ["--session", str(state["session_name"])]
    if headed:
        argv.append("--headed")
    argv += ["--json", "--max-output", str(_MAX_OUTPUT_CHARS), command, *(args or [])]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(state["task_home"]),
            env=_safe_browser_env(
                Path(str(state["socket_dir"])),
                Path(str(state["hermes_root"])) if state.get("hermes_root") else None,
            ),
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error("BROWSER_TIMEOUT", f"The browser command exceeded {_COMMAND_TIMEOUT_SECONDS} seconds.")
    except (OSError, ValueError) as exc:
        return _error("BROWSER_COMMAND_FAILED", op.redact_output(str(exc))[:500])

    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        detail = op.redact_output(result.stderr or result.stdout or "No output was returned.")
        return _error("BROWSER_INVALID_RESPONSE", detail[:1_000])
    if result.returncode != 0 or not isinstance(payload, dict) or not payload.get("success"):
        detail = payload.get("error") if isinstance(payload, dict) else None
        return _error("BROWSER_ACTION_FAILED", op.redact_output(str(detail or "The browser rejected the command."))[:1_000])
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    return {"success": True, "data": data}


def create_browser_session(
    task_id: str,
    task_home: Path,
    hermes_root: Path | None,
    *,
    headed: bool = False,
) -> dict[str, Any]:
    """Start a fresh browser profile for a managed Hermes task."""
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        return _error("INVALID_TASK_ID", "task_id has an invalid format.")
    home = Path(task_home).expanduser().resolve(strict=True)
    executable = _browser_executable(hermes_root)
    state_path = _state_path(home)
    previous = _read_task_state(home)
    if previous:
        if previous.get("browser_source") == "hermes_profile":
            return _error(
                "SHARED_BROWSER_REPLACEMENT_UNAVAILABLE",
                "A browser attached from a Hermes profile cannot be replaced by an isolated session.",
            )
        _run(previous, "close")
    socket_dir = _socket_path(task_id)
    # The browser daemon may still own files here after `close`; removing its
    # socket directory immediately makes a same-session restart fail.
    try:
        _ensure_private_directory(socket_dir)
    except OSError as exc:
        return _error("BROWSER_SOCKET_DIR_UNAVAILABLE", op.redact_output(str(exc))[:500])
    session_name = f"hg_{task_id[:16]}"
    state = {
        "version": 1,
        "task_id": task_id,
        "task_home": str(home),
        "hermes_root": str(Path(hermes_root).expanduser().resolve()) if hermes_root else "",
        "executable": executable,
        "session_name": session_name,
        "socket_dir": str(socket_dir.resolve()),
        "headed": bool(headed),
        "created_at": _now(),
        "status": "starting",
    }
    _write_state(state_path, state)
    result = _run(state, "open", ["about:blank"], headed=headed)
    if not result.get("success"):
        state_path.unlink(missing_ok=True)
        return result
    state.update({"status": "running", "updated_at": _now()})
    _write_state(state_path, state)
    return {
        "success": True,
        "browser": {
            "task_id": task_id,
            "status": "running",
            "source": "isolated",
            "headed": bool(headed),
            "created_at": state["created_at"],
        },
    }


def create_profile_browser_session(
    task_id: str,
    task_home: Path,
    hermes_root: Path | None,
    cdp_port: int,
) -> dict[str, Any]:
    """Attach a task's browser bridge to a configured local Hermes browser."""
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        return _error("INVALID_TASK_ID", "task_id has an invalid format.")
    home = Path(task_home).expanduser().resolve(strict=True)
    try:
        if isinstance(cdp_port, bool) or not isinstance(cdp_port, int):
            raise TypeError("The selected browser profile needs a valid local browser.cdp_url.")
        validate_local_cdp_port(f"http://127.0.0.1:{cdp_port}")
        executable = _browser_executable(hermes_root)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        return _error("BROWSER_PROFILE_UNAVAILABLE", str(exc))
    state_path = _state_path(home)
    socket_dir = _socket_path(task_id)
    try:
        _ensure_private_directory(socket_dir)
    except OSError as exc:
        return _error("BROWSER_SOCKET_DIR_UNAVAILABLE", op.redact_output(str(exc))[:500])
    state = {
        "version": 1,
        "task_id": task_id,
        "task_home": str(home),
        "hermes_root": str(Path(hermes_root).expanduser().resolve()) if hermes_root else "",
        "executable": executable,
        "session_name": f"hg_{task_id[:16]}",
        "socket_dir": str(socket_dir.resolve()),
        "browser_source": "hermes_profile",
        "cdp_port": cdp_port,
        "headed": False,
        "created_at": _now(),
        "status": "starting",
    }
    _write_state(state_path, state)
    result = browser_command(home, "snapshot")
    if not result.get("success"):
        state_path.unlink(missing_ok=True)
        return result
    state.update({"status": "running", "updated_at": _now()})
    _write_state(state_path, state)
    return {
        "success": True,
        "browser": {
            "task_id": task_id,
            "status": "running",
            "source": "hermes_profile",
            "headed": False,
            "created_at": state["created_at"],
        },
    }


def browser_session_state(task_home: Path) -> dict[str, Any]:
    state = _read_task_state(Path(task_home))
    if not state:
        return _error("BROWSER_SESSION_NOT_FOUND", "This task has no managed browser session.")
    state["task_home"] = str(Path(task_home).expanduser().resolve(strict=True))
    if state.get("status") == "closed":
        return {
            "success": True,
            "browser": {
                "status": "closed",
                "source": str(state.get("browser_source") or "isolated"),
                "headed": bool(state.get("headed")),
            },
        }
    result = _run(state, "session", ["info", "--json"])
    if not result.get("success"):
        return result
    url = _run(state, "get", ["url"])
    data = result.get("data") or {}
    url_data = url.get("data")
    current_url = (
        url_data.get("url")
        if url.get("success") and isinstance(url_data, dict)
        else None
    )
    return {
        "success": True,
        "browser": {
            "status": "running" if data.get("active") else "stopped",
            "source": str(state.get("browser_source") or "isolated"),
            "headed": bool(state.get("headed")),
            "current_url": (
                op.redact_output(current_url)[:_MAX_OUTPUT_CHARS]
                if isinstance(current_url, str)
                else None
            ),
            "page_count": data.get("pageCount"),
            "created_at": state.get("created_at"),
        },
    }


def browser_command(
    task_home: Path,
    command: str,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Execute one allowed agent-browser command in a task-owned session."""
    state = _read_task_state(Path(task_home))
    if not state:
        return _error("BROWSER_SESSION_NOT_FOUND", "This task has no managed browser session.")
    if state.get("status") == "closed":
        return _error("BROWSER_SESSION_CLOSED", "This task's browser session has been closed.")
    state["task_home"] = str(Path(task_home).expanduser().resolve(strict=True))
    checked_args = list(args or [])
    allowed = {
        "navigate",
        "snapshot",
        "click",
        "type",
        "fill",
        "scroll",
        "back",
        "press",
        "close",
        "tab",
    }
    if command not in allowed:
        return _error("BROWSER_COMMAND_NOT_ALLOWED", "That browser command is not available through this integration.")
    tab_operation: str | None = None
    tab_reference: str | None = None
    if command == "tab":
        tab_operation, tab_reference = browser_tabs.validate_tab_command_args(checked_args)
        if tab_operation == "invalid":
            return _error(
                "INVALID_BROWSER_TAB",
                "Use tab list or select a stable tab id such as t2 or a simple tab label.",
            )
    if command == "close" and state.get("browser_source") == "hermes_profile":
        return _error(
            "SHARED_BROWSER_CLOSE_UNAVAILABLE",
            "A browser attached from a Hermes profile cannot be closed by a managed task.",
        )
    if command == "navigate":
        if len(checked_args) != 1 or not isinstance(checked_args[0], str):
            return _error("INVALID_URL", "Provide one http, https, or about:blank URL.")
        url = checked_args[0].strip()
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
        except ValueError:
            return _error("INVALID_URL", "Provide one http, https, or about:blank URL.")
        if len(url) > _MAX_URL_CHARS or parts.scheme not in {"http", "https", "about"}:
            return _error("INVALID_URL", "Provide one http, https, or about:blank URL.")
        if parts.scheme == "about" and url != "about:blank":
            return _error("INVALID_URL", "Only about:blank is available for the about scheme.")
        if parts.scheme in {"http", "https"} and not hostname:
            return _error("INVALID_URL", "HTTP URLs must include a host name.")
        if parts.username or parts.password or any(ord(char) < 32 for char in url):
            return _error("INVALID_URL", "The URL contains unsupported credentials or control characters.")
        checked_args[0] = url
    elif command in {"click", "type", "fill"}:
        expected = 2 if command in {"type", "fill"} else 1
        if len(checked_args) != expected:
            return _error("INVALID_BROWSER_ARGUMENT", f"{command} received an invalid argument count.")
        if any(not isinstance(item, str) for item in checked_args):
            return _error("INVALID_BROWSER_ARGUMENT", "Browser arguments must be strings.")
        if command in {"type", "fill"} and len(checked_args[1]) > _MAX_TEXT_CHARS:
            return _error("BROWSER_TEXT_TOO_LARGE", f"Text exceeds the {_MAX_TEXT_CHARS}-character limit.")
    elif command == "press":
        if len(checked_args) != 1 or not re.fullmatch(r"[A-Za-z0-9_+.-]{1,40}", str(checked_args[0])):
            return _error("INVALID_BROWSER_KEY", "Provide a supported key name such as Enter or Control+a.")
    elif command == "scroll":
        if len(checked_args) != 2 or checked_args[0] not in {"up", "down", "left", "right"}:
            return _error("INVALID_SCROLL", "Use a direction and pixel count, for example down, 400.")
        try:
            pixels = int(checked_args[1])
        except (TypeError, ValueError):
            return _error("INVALID_SCROLL", "Scroll pixels must be an integer between 1 and 5000.")
        if not 1 <= pixels <= 5_000:
            return _error("INVALID_SCROLL", "Scroll pixels must be an integer between 1 and 5000.")
        checked_args[1] = str(pixels)
    elif command in {"snapshot", "back", "close"} and checked_args:
        return _error("INVALID_BROWSER_ARGUMENT", f"{command} does not accept arguments.")

    command_args = checked_args
    if command == "tab" and tab_operation == "select":
        # The MCP wrapper marks selection explicitly; the CLI accepts the ref alone.
        command_args = [tab_reference or ""]
    result = _run(state, command, command_args, headed=bool(state.get("headed")))
    if command == "tab":
        if tab_operation == "list":
            normalized = browser_tabs.normalize_tab_list(result)
            if normalized.get("success") is True:
                # agent-browser can cache the hostname as a tab title after
                # navigation. Read the active page without changing tab selection.
                title_result = _run(state, "get", ["title"], headed=bool(state.get("headed")))
                browser_tabs.refresh_active_title(normalized, title_result)
            return normalized
        return browser_tabs.normalize_tab_selection(tab_reference or "", result)
    data = result.get("data") or {}
    if "snapshot" in data:
        data["snapshot"] = op.redact_output(str(data["snapshot"]))[:_MAX_OUTPUT_CHARS]
    if command == "close" and result.get("success"):
        state.update({"status": "closed", "closed_at": _now()})
        _write_state(_state_path(Path(task_home)), state)
        return {"success": True, "browser": {"status": "closed"}}
    for key, value in list(data.items()):
        if isinstance(value, str):
            data[key] = op.redact_output(value)[:_MAX_OUTPUT_CHARS]
    return result


def browser_state_file_command(state_file: Path, command: str, args: list[str] | None = None) -> dict[str, Any]:
    """MCP-server entry point; state-file access is bound by the task's generated config."""
    path = Path(state_file).expanduser().resolve(strict=True)
    state = _read_state(path)
    task_id = path.parent.name
    if (
        not state
        or path.parent.parent.name != ".managed-browser"
        or not _TASK_ID_RE.fullmatch(task_id)
        or path.name != f"{task_id}.json"
    ):
        return _error("BROWSER_SESSION_NOT_FOUND", "This Hermes session has no managed browser session.")
    home = Path(str(state.get("task_home") or "")).expanduser().resolve(strict=True)
    if path != _state_path(home).resolve(strict=True) or state.get("task_id") != home.name:
        return _error("BROWSER_SESSION_NOT_FOUND", "This Hermes session has no managed browser session.")
    return browser_command(home, command, args)


__all__ = [
    "browser_available",
    "browser_command",
    "browser_session_state",
    "browser_state_file",
    "browser_state_file_command",
    "create_browser_session",
    "delete_browser_state",
]
