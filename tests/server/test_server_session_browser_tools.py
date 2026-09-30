from __future__ import annotations

import asyncio

from hermes_gpt.server import app as server


def test_browser_tab_tools_are_gated_and_have_safe_annotations(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SCOPED_TASKS_ENV, "1")

    tools = {
        tool.name: tool
        for tool in asyncio.run(server.build_server().list_tools())
    }

    assert "hermes_task_browser_tabs" in tools
    assert "hermes_task_browser_select_tab" in tools
    assert "hermes_browser_profile_tabs" in tools
    assert "hermes_browser_profile_select_tab" in tools
    assert tools["hermes_task_browser_tabs"].annotations.read_only_hint is True
    assert tools["hermes_browser_profile_tabs"].annotations.read_only_hint is True
    assert tools["hermes_task_browser_select_tab"].annotations.destructive_hint is True
    assert tools["hermes_browser_profile_select_tab"].annotations.destructive_hint is True
    assert (
        tools["hermes_browser_profile_list"].annotations.title
        == "List authorized local Hermes browser profiles"
    )
    assert (
        tools["hermes_browser_profile_attach"].annotations.title
        == "Attach ChatGPT controls to an authorized Hermes browser profile"
    )


def test_browser_tab_tools_are_absent_when_scoped_tasks_are_disabled(monkeypatch):
    monkeypatch.delenv(server.ENABLE_SCOPED_TASKS_ENV, raising=False)

    names = {
        tool.name
        for tool in asyncio.run(server.build_server().list_tools())
    }

    assert "hermes_task_browser_tabs" not in names
    assert "hermes_browser_profile_tabs" not in names
