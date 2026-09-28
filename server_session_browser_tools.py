"""Register browser operations for scoped tasks and authorized Hermes profiles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

import operator_profile_browser as profile_browser
import operator_session_browser as task_browser


def register_session_browser_tools(
    server: Any,
    *,
    tool_meta: Callable[[], dict[str, Any]],
) -> None:
    """Register browser tools under the scoped-session feature gate."""
    task_read_only_tools = (
        task_browser.hermes_task_browser_status,
        task_browser.hermes_task_browser_snapshot,
        task_browser.hermes_task_browser_tabs,
    )
    for tool in task_read_only_tools:
        server.add_tool(
            tool,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title=tool.__name__.replace("_", " "), readOnlyHint=True
            ),
        )

    profile_read_only_tools = (
        (
            profile_browser.hermes_browser_profile_list,
            "List authorized local Hermes browser profiles",
        ),
        (
            profile_browser.hermes_browser_profile_status,
            "Read an authorized Hermes browser profile status",
        ),
        (
            profile_browser.hermes_browser_profile_snapshot,
            "Read the current page of an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_tabs,
            "List tabs in an authorized Hermes browser profile",
        ),
    )
    for tool, title in profile_read_only_tools:
        server.add_tool(
            tool,
            meta=tool_meta(),
            annotations=ToolAnnotations(title=title, readOnlyHint=True),
        )

    task_mutating_tools = (
        task_browser.hermes_task_browser_navigate,
        task_browser.hermes_task_browser_click,
        task_browser.hermes_task_browser_type,
        task_browser.hermes_task_browser_scroll,
        task_browser.hermes_task_browser_back,
        task_browser.hermes_task_browser_press,
        task_browser.hermes_task_browser_select_tab,
        task_browser.hermes_task_browser_close,
        task_browser.hermes_task_browser_restart,
    )
    for tool in task_mutating_tools:
        server.add_tool(
            tool,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title=tool.__name__.replace("_", " "), destructiveHint=True
            ),
        )

    profile_mutating_tools = (
        (
            profile_browser.hermes_browser_profile_attach,
            "Attach ChatGPT controls to an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_navigate,
            "Navigate an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_click,
            "Click in an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_type,
            "Type into an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_scroll,
            "Scroll an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_back,
            "Go back in an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_press,
            "Press a key in an authorized Hermes browser profile",
        ),
        (
            profile_browser.hermes_browser_profile_select_tab,
            "Select a tab in an authorized Hermes browser profile",
        ),
    )
    for tool, title in profile_mutating_tools:
        server.add_tool(
            tool,
            meta=tool_meta(),
            annotations=ToolAnnotations(title=title, destructiveHint=True),
        )


__all__ = ["register_session_browser_tools"]
