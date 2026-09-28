"""Session start, continuation, and scoped task tests."""

import asyncio
import sqlite3

import pytest

import server
from conftest import wire
from server_test_helpers import clear_gate_envs, tool_names, tools_by_name
from session_test_fakes import FakeSessionDB


def test_phase2_tools_are_gated_and_registered(monkeypatch):
    clear_gate_envs(monkeypatch)
    names = tool_names(server.build_server())
    assert "hermes_session_search" not in names
    assert "hermes_session_list" not in names
    assert "hermes_session_read" not in names

    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    names = tool_names(server.build_server())
    assert "hermes_session_search" in names
    assert "hermes_session_list" in names
    assert "hermes_session_read" in names
    assert "hermes_session_export" in names
    assert "hermes_bot_chat_get" in names
    assert "hermes_session_lineage_export" not in names


def test_managed_task_list_is_registered_only_when_scoped_tasks_are_enabled(
    monkeypatch,
):
    clear_gate_envs(monkeypatch)
    assert "hermes_task_list" not in tool_names(server.build_server())
    assert "hermes_browser_profile_attach" not in tool_names(server.build_server())

    monkeypatch.setenv(server.ENABLE_SCOPED_TASKS_ENV, "1")
    tools = tools_by_name(server.build_server())
    assert "hermes_task_list" in tools
    assert "hermes_browser_profile_attach" in tools
    assert tools["hermes_task_list"].annotations.read_only_hint is True


def test_managed_task_list_runs_through_mcp_tool_call(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SCOPED_TASKS_ENV, "1")
    monkeypatch.setenv(server.op_policy.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(server.op_policy.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setattr(server, "_default_hermes_root", lambda: tmp_path)
    task_id = "d" * 32
    record = {
        "task_id": task_id,
        "workspace_id": "demo",
        "status": "completed",
        "model": "deepseek/deepseek-v4.1-flash",
        "reasoning_effort": "high",
        "browser_enabled": True,
        "turn_count": 1,
        "created_at": "2026-09-28T10:00:00+00:00",
        "updated_at": "2026-09-28T10:00:00+00:00",
    }
    server.op_session_tasks._write_json(
        server.op_session_tasks._task_path(task_id, tmp_path), record
    )

    mcp = server.build_server()
    result = asyncio.run(mcp.call_tool("hermes_task_list", {"limit": 5}))

    assert wire(result)["isError"] is False
    listed = wire(result)["structuredContent"]
    assert listed["success"] is True
    assert listed["tasks"][0]["task_id"] == task_id
    assert listed["tasks"][0]["model"] == "deepseek/deepseek-v4.1-flash"


def test_managed_task_start_exposes_browser_profile_through_mcp(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SCOPED_TASKS_ENV, "1")
    captured = {}

    def start_task(**kwargs):
        captured.update(kwargs)
        return {"success": True, "task_id": "e" * 32}

    monkeypatch.setattr(server.op_session_tasks, "hermes_task_start", start_task)
    mcp = server.build_server()
    tool = tools_by_name(mcp)["hermes_task_start"]
    schema = tool.model_dump(by_alias=True)["inputSchema"]
    result = asyncio.run(
        mcp.call_tool(
            "hermes_task_start",
            {
                "prompt": "Inspect the currently configured browser.",
                "workspace_id": "demo",
                "browser_profile": "memtest",
            },
        )
    )

    assert "browser_profile" in schema["properties"]
    assert tool.annotations.destructive_hint is True
    assert wire(result)["isError"] is False
    assert captured["browser_profile"] == "memtest"


def test_session_continue_resolves_id_before_runner_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(server.SESSION_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(connection)
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: tmp_path)
    dispatched = {}

    def fake_continue(session_id, prompt, timeout, **kwargs):
        dispatched.update(
            session_id=session_id, prompt=prompt, timeout=timeout, **kwargs
        )
        return {"success": True, "job_id": "a" * 32, "status": "running"}

    monkeypatch.setattr(server.op_session, "hermes_session_continue", fake_continue)
    result = server.hermes_session_continue("prefix", "continue safely", timeout=123)

    assert result["success"] is True
    assert dispatched["session_id"] == "session-1"
    assert dispatched["prompt"] == "continue safely"
    assert dispatched["timeout"] == 123
    assert dispatched["hermes_root"] == tmp_path
    assert dispatched["profile"] == "default"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_session_continue_resolves_id_in_requested_profile(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(server.SESSION_ALLOWED_PROFILES_ENV, "project-manager")
    monkeypatch.setenv(
        server.op_policy.OPERATOR_ALLOWED_PROFILES_ENV, "project-manager"
    )
    (tmp_path / "profiles" / "project-manager").mkdir(parents=True)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(
        server, "_validate_session_profile", lambda profile="default": profile
    )
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(connection)
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: tmp_path)
    dispatched = {}

    def fake_continue(session_id, prompt, timeout, **kwargs):
        dispatched.update(
            session_id=session_id, prompt=prompt, timeout=timeout, **kwargs
        )
        return {"success": True, "job_id": "b" * 32, "status": "running"}

    monkeypatch.setattr(server.op_session, "hermes_session_continue", fake_continue)
    result = server.hermes_session_continue(
        "prefix",
        "send to project manager",
        timeout=60,
        profile="project-manager",
    )

    assert result["success"] is True
    assert dispatched["session_id"] == "session-1"
    assert dispatched["profile"] == "project-manager"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_bot_chat_send_targets_current_tip_in_requested_profile(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(
        server, "_validate_session_profile", lambda profile="default": profile
    )
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        session_rows=[
            {
                "id": "bot-registry",
                "title": "Bot Chat",
                "source": "desktop",
                "archived": 0,
            },
            {
                "id": "bot-current",
                "source": "desktop",
                "archived": 0,
                "_compression_tip_for": "bot-registry",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    dispatched = {}

    def fake_continue(session_id, prompt, timeout=900, profile="default"):
        dispatched.update(
            session_id=session_id,
            prompt=prompt,
            timeout=timeout,
            profile=profile,
        )
        return {"success": True, "job_id": "c" * 32, "status": "running"}

    monkeypatch.setattr(server, "hermes_session_continue", fake_continue)
    result = server.hermes_bot_chat_send(
        "handoff from ChatGPT",
        profile="project-manager",
        timeout=321,
    )

    assert result["success"] is True
    assert dispatched == {
        "session_id": "bot-current",
        "prompt": "handoff from ChatGPT",
        "timeout": 321,
        "profile": "project-manager",
    }
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")
