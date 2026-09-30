import subprocess

from hermes_gpt.sessions import session
from hermes_gpt.sessions import metadata


def _allow_profile(monkeypatch, root, profile="chatgpt"):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_ALLOWED_PROFILES_ENV, profile)
    monkeypatch.setenv(session.op.OPERATOR_ALLOWED_PROFILES_ENV, profile)
    (root / "profiles" / profile).mkdir(parents=True)


def test_rename_uses_profile_scoped_fixed_hermes_command(monkeypatch, tmp_path):
    _allow_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("TEST_SESSION_METADATA_SECRET", "must-not-reach-child")
    monkeypatch.setattr(
        metadata.sessions,
        "_hermes_executable",
        lambda _agent_root: "/opt/hermes/bin/hermes",
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    result = metadata.hermes_session_rename(
        "20260928_121658_b0cc3e",
        "  Browser QA  ",
        profile="chatgpt",
        hermes_root=tmp_path,
    )

    assert result == {
        "success": True,
        "profile": "chatgpt",
        "session_id": "20260928_121658_b0cc3e",
        "renamed": True,
    }
    argv, kwargs = calls[0]
    assert argv == [
        "/opt/hermes/bin/hermes",
        "sessions",
        "rename",
        "--",
        "20260928_121658_b0cc3e",
        "Browser QA",
    ]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["cwd"] == str(tmp_path / "profiles" / "chatgpt")
    assert kwargs["env"]["HERMES_HOME"] == kwargs["cwd"]
    assert kwargs["env"]["HERMES_PROFILE"] == "chatgpt"
    assert "TEST_SESSION_METADATA_SECRET" not in kwargs["env"]


def test_pin_and_unpin_use_only_fixed_session_commands(monkeypatch, tmp_path):
    _allow_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metadata.sessions,
        "_hermes_executable",
        lambda _agent_root: "/opt/hermes/bin/hermes",
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    pinned = metadata.hermes_session_pin(
        "20260928_121658_b0cc3e", hermes_root=tmp_path, profile="chatgpt"
    )
    unpinned = metadata.hermes_session_pin(
        "20260928_121658_b0cc3e",
        False,
        hermes_root=tmp_path,
        profile="chatgpt",
    )

    assert pinned["pinned"] is True
    assert unpinned["pinned"] is False
    assert calls == [
        ["/opt/hermes/bin/hermes", "sessions", "pin", "--", "20260928_121658_b0cc3e"],
        ["/opt/hermes/bin/hermes", "sessions", "unpin", "--", "20260928_121658_b0cc3e"],
    ]


def test_invalid_or_unauthorized_metadata_updates_do_not_launch_cli(
    monkeypatch, tmp_path
):
    _allow_profile(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(metadata.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    invalid_id = metadata.hermes_session_rename(
        "../outside", "Title", profile="chatgpt", hermes_root=tmp_path
    )
    invalid_title = metadata.hermes_session_rename(
        "session-1", "line\nbreak", profile="chatgpt", hermes_root=tmp_path
    )
    invalid_pin = metadata.hermes_session_pin(
        "session-1", "true", profile="chatgpt", hermes_root=tmp_path
    )
    unauthorized = metadata.hermes_session_pin(
        "session-1", profile="other", hermes_root=tmp_path
    )

    assert invalid_id["code"] == "INVALID_SESSION_ID"
    assert invalid_title["code"] == "INVALID_SESSION_TITLE"
    assert invalid_pin["code"] == "INVALID_PIN_STATE"
    assert unauthorized["code"] == "SESSION_PROFILE_NOT_ALLOWED"
    assert calls == []


def test_metadata_updates_are_disabled_with_session_control(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    calls = []
    monkeypatch.setattr(metadata.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    result = metadata.hermes_session_pin("session-1", hermes_root=tmp_path)

    assert result["code"] == "SESSION_CONTROL_DISABLED"
    assert calls == []
