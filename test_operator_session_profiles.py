"""Profile discovery exposes only authorized, non-secret session defaults."""

import json

import operator_session_profiles as session_profiles


def _profile(root, name, config):
    if name == "default":
        home = root
    else:
        home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    return home


def _enable_profiles(monkeypatch, profiles="chatgpt", operator_profiles="chatgpt"):
    monkeypatch.setenv(session_profiles.sessions.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session_profiles.sessions.SESSION_ALLOWED_PROFILES_ENV, profiles)
    monkeypatch.setenv(
        session_profiles.op.OPERATOR_ALLOWED_PROFILES_ENV, operator_profiles
    )


def test_discovery_returns_only_authorized_profiles_and_safe_defaults(
    monkeypatch, tmp_path
):
    _enable_profiles(monkeypatch, "chatgpt,private", "chatgpt")
    chatgpt = _profile(
        tmp_path,
        "chatgpt",
        "model:\n"
        "  default: opencode-go/deepseek-v4.1-flash\n"
        "  provider: opencode-go\n"
        "  api_key: must-not-leak\n"
        "agent:\n"
        "  reasoning_effort: low\n"
        "mcp_servers:\n"
        "  private-server:\n"
        "    command: /private/path/server\n"
        "    env:\n"
        "      API_TOKEN: also-must-not-leak\n",
    )
    (tmp_path / "profiles" / "private").mkdir()

    result = session_profiles.hermes_session_profiles(tmp_path)

    assert result["success"] is True
    assert result["profiles"] == [
        {
            "profile": "chatgpt",
            "default_model": "opencode-go/deepseek-v4.1-flash",
            "configured_provider": "opencode-go",
            "configured_reasoning_effort": "low",
        }
    ]
    assert "must-not-leak" not in json.dumps(result)
    assert "/private/path" not in json.dumps(result)
    assert chatgpt.is_dir()


def test_discovery_hides_model_values_that_look_like_credentials(monkeypatch, tmp_path):
    _enable_profiles(monkeypatch, "default", "default")
    _profile(
        tmp_path,
        "default",
        "model:\n  default: sk-proj-123456789012345678901234567890\n  provider: https://private.invalid\n",
    )

    result = session_profiles.hermes_session_profiles(tmp_path)

    assert result["success"] is True
    assert result["profiles"] == [{"profile": "default"}]


def test_discovery_omits_defaults_when_profile_config_is_invalid(
    monkeypatch, tmp_path
):
    _enable_profiles(monkeypatch, "default", "default")
    _profile(tmp_path, "default", "model: [unterminated\n")

    result = session_profiles.hermes_session_profiles(tmp_path)

    assert result["success"] is True
    assert result["profiles"] == [{"profile": "default"}]


def test_discovery_rejects_disabled_and_unbounded_allowlists(monkeypatch, tmp_path):
    monkeypatch.delenv(session_profiles.sessions.ENABLE_SESSION_CONTROL_ENV, raising=False)
    monkeypatch.setenv(session_profiles.sessions.SESSION_ALLOWED_PROFILES_ENV, "chatgpt")
    disabled = session_profiles.hermes_session_profiles(tmp_path)
    assert disabled["code"] == "SESSION_CONTROL_DISABLED"

    monkeypatch.setenv(session_profiles.sessions.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session_profiles.sessions.SESSION_ALLOWED_PROFILES_ENV, "*")
    unbounded = session_profiles.hermes_session_profiles(tmp_path)
    assert unbounded["code"] == "SESSION_PROFILE_ALLOWLIST_INVALID"
