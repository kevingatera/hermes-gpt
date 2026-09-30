"""Shared callbacks for profile-aware Hermes session MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionToolContext:
    """Callbacks and operator modules needed by the session tool handlers."""

    make_adapter: Callable[..., Any]
    require_imports: Callable[[], None]
    session_history_enabled: Callable[[], bool]
    session_history_env_name: str
    session_control_enabled: Callable[[], bool]
    session_control_env_name: str
    validate_profile: Callable[[str], str]
    get_hermes_root: Callable[[], Any]
    get_agent_root: Callable[[], Any]
    session_db_available: Callable[[], bool]
    continue_session: Callable[..., Any]
    eprint: Callable[[str], None]
    policy: Any
    session_control: Any
    managed_tasks: Any
