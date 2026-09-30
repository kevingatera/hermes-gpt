"""Extraction tests for the server_cli seam.

The historical CLI functions remain on ``server`` and hand the extracted module
an explicit context. These tests pin that seam: the wrapper must resolve
``server`` globals per call (so monkeypatching keeps working), keep the exact
stderr lines and transport defaults, and ``server_cli`` must stay free of a
``server`` import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

import server
import server_cli


class FakeServer:
    def __init__(self) -> None:
        self.runs: list[dict] = []

    def run(self, **kwargs) -> None:
        self.runs.append(kwargs)


def test_server_cli_never_imports_server() -> None:
    source = Path(server_cli.__file__).read_text(encoding="utf-8")
    assert re.search(r"^\s*(import server\b|from server import)", source, re.MULTILINE) is None


def test_legacy_stdio_keeps_default_transport_and_message(monkeypatch, capsys) -> None:
    fake = FakeServer()
    monkeypatch.setattr(server, "build_server", lambda **kwargs: fake)

    server._run_legacy_server([])

    assert fake.runs == [{"transport": "stdio"}]
    assert "hermes-gpt MCP server starting in stdio mode." in capsys.readouterr().err


def test_main_mcp_alias_resolves_server_globals_at_call_time(monkeypatch, capsys) -> None:
    fake = FakeServer()
    monkeypatch.setattr(server, "build_codex_mcp_server", lambda **kwargs: fake)

    server.main(["mcp", "--host", "127.0.0.1", "--port", "7677"])

    assert fake.runs == [{"transport": "stdio"}]
    assert "hermes-gpt Codex MCP server starting in stdio mode." in capsys.readouterr().err


def test_cli_context_is_rebuilt_per_call(monkeypatch) -> None:
    first, second = FakeServer(), FakeServer()
    monkeypatch.setattr(server, "build_server", lambda **kwargs: first)
    server._run_legacy_server([])
    monkeypatch.setattr(server, "build_server", lambda **kwargs: second)
    server._run_legacy_server([])

    assert first.runs and second.runs


def test_legacy_http_branch_keeps_tls_and_fleet_card(monkeypatch, capsys) -> None:
    fake = FakeServer()
    app = object()
    cards: list[int] = []
    captured: dict = {}
    monkeypatch.setattr(server, "build_server", lambda **kwargs: fake)
    monkeypatch.setattr(server, "build_asgi_app", lambda built, *, http: app)
    monkeypatch.setattr(server, "_register_fleet_local_card", lambda: cards.append(1))
    monkeypatch.setattr(uvicorn, "run", lambda app_, **kwargs: captured.update(kwargs, app=app_))

    server._run_legacy_server(["--http", "--cert", "/tmp/cert.pem", "--key", "/tmp/key.pem"])

    assert captured["app"] is app
    assert captured["ssl_certfile"] == "/tmp/cert.pem"
    assert captured["ssl_keyfile"] == "/tmp/key.pem"
    assert "forwarded_allow_ips" in captured
    assert cards == [1]
    assert "hermes-gpt MCP server running at http://127.0.0.1:7677/mcp" in capsys.readouterr().err


def test_conflicting_transports_still_fail_fast() -> None:
    with pytest.raises(SystemExit, match="Choose only one of --http or --sse."):
        server.main(["--http", "--sse"])


class FakeToolListServer:
    """Minimal MCP server exposing ``list_tools`` for the codex installer path."""

    async def list_tools(self):
        return [SimpleNamespace(name=name) for name in ("hermes_read_file", "hermes_run_command")]


def _capture_codex_config(monkeypatch) -> dict:
    import codex_config

    captured: dict = {}
    monkeypatch.setattr(
        codex_config, "main", lambda argv, **kwargs: captured.update(argv=argv, **kwargs)
    )
    return captured


def test_codex_installer_receives_live_tool_list_and_status(monkeypatch) -> None:
    captured = _capture_codex_config(monkeypatch)
    monkeypatch.setattr(server, "build_codex_mcp_server", lambda **kwargs: FakeToolListServer())
    monkeypatch.setattr(
        server,
        "hermes_gateway_status",
        lambda: json.dumps(
            {"success": True, "gateway_running": True, "gateway_pid_source": "gateway.pid"}
        ),
    )

    server.main(["codex", "install", "--toolset", "core"])

    assert captured["argv"] == ["install", "--toolset", "core"]
    assert captured["list_tools"]() == ["hermes_read_file", "hermes_run_command"]
    assert captured["status"]() == {
        "ok": True,
        "gateway": "running",
        "gateway_pid_source": "gateway.pid",
    }


def test_codex_status_callback_fails_soft(monkeypatch) -> None:
    captured = _capture_codex_config(monkeypatch)

    def broken_status() -> str:
        raise RuntimeError("gateway state unavailable")

    monkeypatch.setattr(server, "hermes_gateway_status", broken_status)

    server.main(["codex", "install"])

    assert captured["status"]() == {"ok": False, "gateway": "unknown"}


@pytest.mark.parametrize("argv,runner,expected", [
    (["mcp", "--http"], "_run_codex_mcp", ["--http"]),
    (["codex", "mcp", "--http"], "_run_codex_mcp", ["--http"]),
    (["--sse"], "_run_legacy_server", ["--sse"]),
])
def test_main_preserves_historical_runner_dispatch(monkeypatch, argv, runner, expected):
    calls = []
    monkeypatch.setattr(server, runner, lambda args: calls.append(args))
    server.main(argv)
    assert calls == [expected]
