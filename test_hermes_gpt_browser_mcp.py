from __future__ import annotations

import asyncio
from pathlib import Path

from hermes_gpt_browser_mcp import build_server


def test_managed_browser_bridge_registers_tab_tools():
    server = build_server(state_file=Path("/unused/state.json"))

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert "browser_tabs" in tools
    assert "browser_select_tab" in tools
    assert tools["browser_tabs"].annotations.read_only_hint is True
    assert tools["browser_select_tab"].annotations.destructive_hint is True
