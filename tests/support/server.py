"""Shared MCP test environment helpers."""

from __future__ import annotations

import asyncio

import pytest

from hermes_gpt.auth import oauth as oauth_auth
from hermes_gpt.server import app as server

GATE_ENVS = [
    server.ENABLE_WRITE_ENV,
    server.ENABLE_MEMORY_WRITE_ENV,
    server.ENABLE_SESSION_SEARCH_ENV,
    server.ENABLE_SESSION_CONTROL_ENV,
    server.ENABLE_SCOPED_TASKS_ENV,
    server.SESSION_ALLOWED_PROFILES_ENV,
    server.ENABLE_TERMINAL_ENV,
    server.ENABLE_VISION_ENV,
    server.ENABLE_WEB_ENV,
    server.UNSAFE_REMOTE_ENV,
    oauth_auth.AUTH_TOKEN_ENV,
    oauth_auth.OAUTH_ENABLE_ENV,
    oauth_auth.OAUTH_ISSUER_ENV,
    oauth_auth.OAUTH_CLIENT_ID_ENV,
    oauth_auth.OAUTH_CLIENT_SECRET_ENV,
    oauth_auth.OAUTH_REDIRECT_URI_ENV,
    oauth_auth.OAUTH_SCOPE_ENV,
    server.TRUSTED_PROXY_IPS_ENV,
    server.ALLOWED_HOSTS_ENV,
]


def clear_gate_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GATE_ENVS:
        monkeypatch.delenv(name, raising=False)


def tool_names(mcp_server) -> list[str]:
    tools = asyncio.run(mcp_server.list_tools())
    return sorted(tool.name for tool in tools)


def tools_by_name(mcp_server):
    tools = asyncio.run(mcp_server.list_tools())
    return {tool.name: tool for tool in tools}
