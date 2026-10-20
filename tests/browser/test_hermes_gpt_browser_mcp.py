from __future__ import annotations

import asyncio
from pathlib import Path

from hermes_gpt.browser.bridge import build_server
from tests.conftest import wire


def test_managed_browser_bridge_registers_tab_tools():
    server = build_server(state_file=Path("/unused/state.json"))

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert "task_browser_tabs" in tools
    assert "task_browser_select_tab" in tools
    assert wire(tools["task_browser_tabs"].annotations)["readOnlyHint"] is True
    assert wire(tools["task_browser_select_tab"].annotations)["destructiveHint"] is True
