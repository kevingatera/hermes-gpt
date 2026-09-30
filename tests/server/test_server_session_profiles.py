from __future__ import annotations

import asyncio

from hermes_gpt.server import app as server
from tests.conftest import wire


def test_profile_discovery_tool_is_gated_and_returns_only_safe_defaults(
    monkeypatch, tmp_path
):
    profile_home = tmp_path / "profiles" / "chatgpt"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  default: opencode-go/deepseek-v4.1-flash\n"
        "  provider: opencode-go\n"
        "agent:\n"
        "  reasoning_effort: low\n"
        "api_key: sk-test-12345678901234567890\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(server.SESSION_ALLOWED_PROFILES_ENV, "chatgpt")
    monkeypatch.setenv(server.op_policy.OPERATOR_ALLOWED_PROFILES_ENV, "chatgpt")
    monkeypatch.setattr(server, "_default_hermes_root", lambda: tmp_path)

    mcp = server.build_server()
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    profile_tool = tools["hermes_session_profiles"]

    assert wire(profile_tool.annotations)["readOnlyHint"] is True

    result = asyncio.run(mcp.call_tool("hermes_session_profiles", {}))
    response = wire(result)

    assert response["isError"] is False
    assert response["structuredContent"]["profiles"] == [
        {
            "profile": "chatgpt",
            "default_model": "opencode-go/deepseek-v4.1-flash",
            "configured_provider": "opencode-go",
            "configured_reasoning_effort": "low",
        }
    ]
    assert "sk-test" not in str(response)


def test_profile_discovery_tool_is_absent_when_session_control_is_disabled(
    monkeypatch,
):
    monkeypatch.delenv(server.ENABLE_SESSION_CONTROL_ENV, raising=False)

    tools = {
        tool.name for tool in asyncio.run(server.build_server().list_tools())
    }

    assert "hermes_session_profiles" not in tools
