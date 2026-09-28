"""Narrow stdio MCP bridge for a managed Hermes browser session."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from mcp_compat import HermesMCP
from operator_browser import browser_state_file_command

BROWSER_STATE_FILE_ENV = "HERMES_GPT_BROWSER_STATE_FILE"


def build_server(state_file: Path) -> HermesMCP:
    """Build browser tools bound to one task's private browser-state file."""
    server = HermesMCP("hermes-gpt-browser", version="0.1.0")

    def browser_navigate(url: str) -> dict[str, Any]:
        """Open an http(s) page or about:blank in this task's configured browser."""
        return browser_state_file_command(state_file, "navigate", [url])

    def browser_snapshot() -> dict[str, Any]:
        """Read the current page's accessible text and element references."""
        return browser_state_file_command(state_file, "snapshot")

    def browser_tabs() -> dict[str, Any]:
        """List open tabs using stable IDs without returning page contents."""
        return browser_state_file_command(state_file, "tab", ["list"])

    def browser_select_tab(tab: str) -> dict[str, Any]:
        """Switch the current browser target to a tab ID or label."""
        return browser_state_file_command(state_file, "tab", ["select", tab])

    def browser_click(ref: str) -> dict[str, Any]:
        """Click one element reference from the latest browser snapshot."""
        return browser_state_file_command(state_file, "click", [ref])

    def browser_type(ref: str, text: str) -> dict[str, Any]:
        """Type text into an element reference from the latest browser snapshot."""
        return browser_state_file_command(state_file, "type", [ref, text])

    def browser_scroll(direction: str, pixels: int = 500) -> dict[str, Any]:
        """Scroll the current page by a bounded number of pixels."""
        return browser_state_file_command(state_file, "scroll", [direction, str(pixels)])

    def browser_back() -> dict[str, Any]:
        """Go back one page in this session's browser history."""
        return browser_state_file_command(state_file, "back")

    def browser_press(key: str) -> dict[str, Any]:
        """Press a keyboard key or a key combination on the current page."""
        return browser_state_file_command(state_file, "press", [key])

    server.add_tool(browser_navigate)
    server.add_tool(
        browser_snapshot,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    server.add_tool(
        browser_tabs,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    server.add_tool(
        browser_select_tab,
        annotations=ToolAnnotations(destructiveHint=True),
    )
    server.add_tool(browser_click)
    server.add_tool(browser_type)
    server.add_tool(browser_scroll)
    server.add_tool(browser_back)
    server.add_tool(browser_press)
    return server


def main() -> None:
    raw_path = os.environ.get(BROWSER_STATE_FILE_ENV, "").strip()
    if not raw_path:
        print(f"{BROWSER_STATE_FILE_ENV} is required", file=sys.stderr)
        raise SystemExit(2)
    try:
        state_file = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        print(f"Browser session state is unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    task_id = state_file.parent.name
    if (
        state_file.parent.parent.name != ".managed-browser"
        or not re.fullmatch(r"[0-9a-f]{32}", task_id)
        or state_file.name != f"{task_id}.json"
    ):
        print("Browser session state file has an invalid name", file=sys.stderr)
        raise SystemExit(2)
    build_server(state_file).run(transport="stdio")


if __name__ == "__main__":
    main()
