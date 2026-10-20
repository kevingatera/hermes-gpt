from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "openai-secure-mcp-tunnel.md"
SESSIONS_GUIDE = ROOT / "docs" / "chatgpt-sessions-plugin.md"
LAUNCHER = ROOT / "examples" / "start-openai-secure-mcp-tunnel.example.ps1"
STATUS = ROOT / "examples" / "status-hermes-gpt.example.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_secure_tunnel_guide_preserves_loopback_and_security_boundaries() -> None:
    text = _read(GUIDE)

    assert "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels" in text
    assert "http://127.0.0.1:4750/mcp" in text
    assert "tunnel-client.exe doctor --profile hermes-gpt --explain" in text
    assert "tunnel-client.exe run --profile hermes-gpt" in text
    assert "HERMES_GPT_ALLOWED_HOSTS" in text
    assert "authorization server itself is not automatically tunneled" in text
    assert "Operator or Owner authorization" in text
    assert "public plugin distribution endpoint" in text


def test_chatgpt_sessions_guide_uses_curated_loopback_mcp_and_explicit_gates() -> None:
    text = _read(SESSIONS_GUIDE)

    assert "python -m hermes_gpt mcp --http --host 127.0.0.1 --port 4751" in text
    assert "http://127.0.0.1:4751/mcp" in text
    for env_name in (
        "HERMES_GPT_CODEX_TOOLSET=sessions",
        "HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES",
        "HERMES_GPT_OPERATOR_ALLOWED_PROFILES",
        "HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES",
        "HERMES_GPT_TASK_WORKSPACES",
        "HERMES_GPT_OPERATOR_APPLY_MODE=direct",
    ):
        assert env_name in text
    assert "reasoning_effort" in text
    assert "profile's configured defaults" in text
    assert "does not require a separate provider API key" in text
    assert "selected profile's Hermes configuration" in text
    assert "clone the selected" in text
    assert "enabled tool and MCP settings" in text
    assert "plugin_asdk_app" in text
    assert "tools/build_chatgpt_sessions_plugin.py" in text
    assert "dist-plugin/hermes-sessions" in text
    assert "not inspect another process's environment" in text
    assert "browser.use_real_profile" in text
    assert "same loopback CDP endpoint" in text
    assert "curated `sessions` connection does not register peer-routing tools" in text
    assert "hermes_fleet_*" in text
    assert "separately authorized Operator" in text


def test_session_control_docs_use_selected_profile_resources_by_default() -> None:
    text = _read(ROOT / "docs" / "session-control.md")

    text = " ".join(text.split())
    assert "Each run sets `HERMES_HOME` to that profile." in text
    assert "Hermes GPT does not copy or parse its provider credentials" in text
    assert "Omit `model` and `reasoning_effort`" in text


def test_secure_tunnel_launcher_supervises_only_owned_children() -> None:
    text = _read(LAUNCHER)

    assert "$ListenHost = '127.0.0.1'" in text
    assert "CONTROL_PLANE_API_KEY" in text
    assert "doctor', '--profile', $TunnelProfile, '--explain'" in text
    assert "run', '--profile', $TunnelProfile" in text
    assert "Where-Object { $_.OwningProcess -eq $Process.Id }" in text
    assert "Stop-Process -Id $Process.Id" in text
    assert "Get-Process python" not in text
    assert "taskkill" not in text.lower()
    assert "sk-" not in text


def test_status_example_does_not_assume_a_public_tunnel_hostname() -> None:
    text = _read(STATUS)

    assert "https://your-domain.example/mcp" not in text
    assert "not public" in text
    assert "--profile $TunnelProfile --explain" in text


def test_secure_tunnel_docs_are_wired_into_current_entry_points() -> None:
    root_readme = _read(ROOT / "README.md")
    docs_readme = _read(ROOT / "docs" / "README.md")
    windows_guide = _read(ROOT / "docs" / "windows-chatgpt-codex.md")
    codex_guide = _read(ROOT / "docs" / "codex.md")
    cloudflare_guide = _read(ROOT / "docs" / "cloudflare-tunnel.md")
    compatibility = _read(ROOT / "docs" / "mcp-compatibility.md")

    for text in (root_readme, docs_readme, windows_guide, cloudflare_guide, compatibility):
        assert "openai-secure-mcp-tunnel.md" in text
    assert "chatgpt-sessions-plugin.md" in root_readme
    assert "chatgpt-sessions-plugin.md" in docs_readme
    assert "chatgpt-sessions-plugin.md" in codex_guide


def test_secure_tunnel_docs_and_launcher_ship_in_package_data() -> None:
    data = tomllib.loads(_read(ROOT / "pyproject.toml"))
    package_data = data["tool"]["setuptools"]["data-files"]
    shipped_docs = package_data["share/hermes-gpt/docs"]
    shipped_examples = package_data["share/hermes-gpt/examples"]

    assert "docs/openai-secure-mcp-tunnel.md" in shipped_docs
    assert "docs/chatgpt-sessions-plugin.md" in shipped_docs
    assert "docs/cloudflare-tunnel.md" in shipped_docs
    assert "examples/start-openai-secure-mcp-tunnel.example.ps1" in shipped_examples
