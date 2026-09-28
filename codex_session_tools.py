"""Register the opt-in session and browser tools on the Codex MCP server."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

from server_session_browser_tools import register_session_browser_tools

_READ_ONLY_HISTORY_TOOLS = (
    "hermes_bot_chat_get",
    "hermes_session_list",
    "hermes_session_search",
    "hermes_session_read",
    "hermes_session_export",
)


def register_codex_session_tools(
    server: Any,
    *,
    history_tools: Any,
    session_control_tools: Any,
    managed_task_tools: Any,
    tool_meta: Callable[[], dict[str, Any]],
    session_history_enabled: bool,
    session_control_enabled: bool,
    scoped_tasks_enabled: bool,
) -> None:
    """Register enabled session tools for an explicitly selected Codex toolset."""
    if session_history_enabled:
        for name in _READ_ONLY_HISTORY_TOOLS:
            tool = getattr(history_tools, name)
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(
                    title=name.replace("_", " "), readOnlyHint=True
                ),
            )

        if session_control_enabled:
            server.add_tool(
                history_tools.hermes_bot_chat_send,
                meta=tool_meta(),
                annotations=ToolAnnotations(
                    title="Send a turn to the current Bot Chat",
                    destructiveHint=True,
                ),
            )

    session_control_tools.register_mcp_tools(
        server,
        tool_meta=tool_meta,
        session_control_enabled=session_control_enabled,
    )
    managed_task_tools.register_mcp_tools(
        server,
        tool_meta=tool_meta,
        enabled=scoped_tasks_enabled,
    )
    if scoped_tasks_enabled:
        register_session_browser_tools(server, tool_meta=tool_meta)


__all__ = ["register_codex_session_tools"]
