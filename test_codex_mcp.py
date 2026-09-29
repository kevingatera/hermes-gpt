import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import codex_core
import codex_mcp
from conftest import wire


def test_codex_mcp_registry_is_curated_and_complete():
    core = codex_core.CodexToolCore(
        version="test",
        imports_ready=lambda: True,
        gateway_snapshot=lambda: {"gateway": {"running": False}},
        gateway_diagnostics_callback=dict,
        vision_analyze=lambda path, prompt: {},
        web_search=lambda query, limit: {},
        web_extract=lambda urls, limit: {},
        cron_create_callback=lambda schedule, prompt, dry_run: {},
        skill_create_callback=lambda name, content, dry_run: {},
    )
    server = codex_mcp.build_codex_server(core)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "hermes_status", "hermes_capabilities", "hermes_vision_analyze",
        "hermes_web_search", "hermes_extract_page", "hermes_plan",
        "hermes_author_skill", "hermes_cron_plan", "hermes_cron_create",
        "hermes_gateway_diagnostics",
    }
    for tool in asyncio.run(server.list_tools()):
        assert tool.meta == {"securitySchemes": [{"type": "noauth"}]}


def test_operator_toolset_is_opt_in_and_namespaced(monkeypatch):
    monkeypatch.setenv(codex_core.CODEX_TOOLSET_ENV, "operator")
    core = codex_core.CodexToolCore(
        version="test", imports_ready=lambda: True, gateway_snapshot=dict,
        gateway_diagnostics_callback=dict, vision_analyze=lambda path, prompt: {},
        web_search=lambda query, limit: {}, web_extract=lambda urls, limit: {},
        cron_create_callback=lambda schedule, prompt, dry_run: {},
        skill_create_callback=lambda name, content, dry_run: {},
    )
    def policy() -> str:
        return json.dumps({"success": True, "token": "secret-token-123456789"})

    def skill_view(name: str) -> str:
        return f"# {name}\nPlain skill instructions."

    server = codex_mcp.build_codex_server(core, http=True, operator_tools={
        "hermes_operator_policy": policy, "hermes_operator_skill_view": skill_view,
    })
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "hermes_operator_policy" in names
    for tool in asyncio.run(server.list_tools()):
        if tool.name.startswith("hermes_operator_"):
            assert wire(tool).get("outputSchema") is None
    from starlette.testclient import TestClient

    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:7677") as client:
        for name, arguments in [("hermes_operator_policy", {}), ("hermes_operator_skill_view", {"name": "example"})]:
            response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError", False), result
            text = next(c["text"] for c in result["content"] if c["type"] == "text")
            assert "secret-token-123456789" not in text
            assert result.get("structuredContent") is None
            if name == "hermes_operator_policy":
                assert json.loads(text)["success"] is True
            else:
                assert text == "# example\nPlain skill instructions."


def test_sessions_toolset_registers_history_control_and_browser_tools(monkeypatch, tmp_path):
    import server

    monkeypatch.setenv(codex_core.CODEX_TOOLSET_ENV, "sessions")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(codex_core.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setenv(codex_core.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(codex_core.ENABLE_SCOPED_TASKS_ENV, "1")

    tools = asyncio.run(server.build_codex_mcp_server().list_tools())
    by_name = {tool.name: tool for tool in tools}
    expected = {
        "hermes_session_list",
        "hermes_session_start",
        "hermes_session_continue",
        "hermes_session_rename",
        "hermes_session_pin",
        "hermes_task_start",
        "hermes_task_continue",
        "hermes_task_browser_tabs",
        "hermes_browser_profile_tabs",
        "hermes_browser_profile_navigate",
    }
    assert expected <= set(by_name)

    start_schema = by_name["hermes_task_start"].model_dump(by_alias=True)["inputSchema"]
    assert {"model", "reasoning_effort", "browser_profile"} <= set(
        start_schema["properties"]
    )
    assert not {"model", "reasoning_effort"} & set(start_schema.get("required", []))
    continue_schema = by_name["hermes_session_continue"].model_dump(by_alias=True)["inputSchema"]
    assert {"model", "reasoning_effort"} <= set(continue_schema["properties"])
    start_schema = by_name["hermes_session_start"].model_dump(by_alias=True)["inputSchema"]
    assert {"model", "reasoning_effort", "profile"} <= set(start_schema["properties"])
    rename_schema = by_name["hermes_session_rename"].model_dump(by_alias=True)["inputSchema"]
    assert {"session_id", "title", "profile"} <= set(rename_schema["properties"])
    pin_schema = by_name["hermes_session_pin"].model_dump(by_alias=True)["inputSchema"]
    assert {"session_id", "pinned", "profile"} <= set(pin_schema["properties"])
    assert by_name["hermes_session_list"].annotations.read_only_hint is True
    assert by_name["hermes_session_start"].annotations.destructive_hint is True
    assert by_name["hermes_session_rename"].annotations.destructive_hint is False
    assert by_name["hermes_session_pin"].annotations.idempotent_hint is True
    assert by_name["hermes_session_job_status"].annotations.read_only_hint is True
    assert by_name["hermes_session_job_result"].annotations.read_only_hint is True
    assert by_name["hermes_task_start"].annotations.destructive_hint is True
    assert by_name["hermes_session_list"].meta == codex_mcp.NOAUTH_META

    monkeypatch.setenv(codex_core.ENABLE_SESSION_SEARCH_ENV, "0")
    monkeypatch.setenv(codex_core.ENABLE_SESSION_CONTROL_ENV, "0")
    monkeypatch.setenv(codex_core.ENABLE_SCOPED_TASKS_ENV, "0")
    gated_names = {
        tool.name for tool in asyncio.run(server.build_codex_mcp_server().list_tools())
    }
    assert not {
        "hermes_session_list",
        "hermes_session_start",
        "hermes_session_continue",
        "hermes_session_rename",
        "hermes_session_pin",
        "hermes_task_start",
        "hermes_browser_profile_tabs",
    } & gated_names

    monkeypatch.setenv(codex_core.CODEX_TOOLSET_ENV, "core")
    core_names = {
        tool.name for tool in asyncio.run(server.build_codex_mcp_server().list_tools())
    }
    assert "hermes_task_start" not in core_names
    assert "hermes_session_start" not in core_names
    assert "hermes_session_list" not in core_names


def test_invalid_toolset_fails_safely(monkeypatch):
    monkeypatch.setenv(codex_core.CODEX_TOOLSET_ENV, "everything")
    try:
        codex_core.codex_toolset()
    except ValueError as exc:
        assert "expected core, operator, or sessions" in str(exc)
    else:
        raise AssertionError("invalid toolset was accepted")


def _readline_with_timeout(stream, seconds: float = 30.0) -> str:
    # 30s deadline: the stdio server subprocess performs the full Hermes agent
    # import at startup (heavy on cold caches/loaded CI runners); 8s raced
    # real startup latency and flaked under load. This is an I/O wait
    # deadline, not a safety assertion — a hang still fails the test.
    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(stream.readline()), daemon=True)
    worker.start()
    worker.join(seconds)
    if not result:
        raise TimeoutError("No MCP stdio response arrived in time.")
    return result[0]


@pytest.mark.skipif(sys.platform == "win32", reason="Subprocess stdio test hangs on Windows CI runners")
def test_codex_stdio_initialize_list_and_safe_tool_call():
    root = Path(__file__).resolve().parent
    proc = subprocess.Popen(
        [sys.executable, "server.py", "mcp"],
        cwd=root,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "HERMES_GPT_ENABLE_CODEX": "1", "HERMES_GPT_ENABLE_MCP": "1"},
    )

    def send(payload):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}})
        initialized = json.loads(_readline_with_timeout(proc.stdout))
        assert initialized["result"]["serverInfo"]["name"] == "hermes-gpt"
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = json.loads(_readline_with_timeout(proc.stdout))
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"hermes_status", "hermes_capabilities", "hermes_plan"}.issubset(names)
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hermes_capabilities", "arguments": {}}})
        called = json.loads(_readline_with_timeout(proc.stdout))
        assert called["result"]["isError"] is False
        assert called["result"]["structuredContent"]["ok"] is True
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hermes_status", "arguments": {}}})
        status = json.loads(_readline_with_timeout(proc.stdout))
        assert status["result"]["isError"] is False
        assert "gateway" in status["result"]["structuredContent"]
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
