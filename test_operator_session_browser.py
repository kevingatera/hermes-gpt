from __future__ import annotations

import pytest

import operator_browser_profiles as browser_profiles
import operator_profile_browser as profile_controls
import operator_session_browser as controls
import operator_session_tasks as tasks


def test_browser_actions_require_dry_run_or_explicit_confirmation(monkeypatch, tmp_path):
    task_id = "a" * 32
    data_root = tmp_path / "hermes-home"
    task_home = data_root / "profiles" / task_id
    task_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(data_root))
    monkeypatch.setenv(tasks.ENABLE_SCOPED_TASKS_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(tasks.op.OPERATOR_APPLY_MODE_ENV, "direct")
    tasks._write_json(tasks._task_path(task_id, data_root), {
        "task_id": task_id,
        "task_home": str(task_home),
        "browser_enabled": True,
    })
    calls = []
    monkeypatch.setattr(
        controls.browser,
        "browser_command",
        lambda home, command, args: calls.append((home, command, args)) or {"success": True},
    )

    dry_run = controls.hermes_task_browser_navigate(task_id, "https://example.com")
    no_confirmation = controls.hermes_task_browser_navigate(
        task_id, "https://example.com", confirm=False, dry_run=False
    )
    applied = controls.hermes_task_browser_navigate(
        task_id, "https://example.com", confirm=True, dry_run=False
    )

    assert dry_run["success"] is True and dry_run["dry_run"] is True
    assert no_confirmation["code"] == "CONFIRMATION_REQUIRED"
    assert applied["success"] is True
    assert calls == [(task_home, "navigate", ["https://example.com"])]


def test_attached_profile_browser_cannot_be_restarted(monkeypatch, tmp_path):
    task_id = "b" * 32
    data_root = tmp_path / "hermes-home"
    task_home = data_root / "profiles" / task_id
    task_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(data_root))
    monkeypatch.setenv(tasks.ENABLE_SCOPED_TASKS_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_LEVEL_ENV, "read_only")
    tasks._write_json(tasks._task_path(task_id, data_root), {
        "task_id": task_id,
        "task_home": str(task_home),
        "browser_enabled": True,
        "browser_source": "hermes_profile",
    })
    monkeypatch.setattr(
        controls.browser,
        "create_browser_session",
        lambda *_args, **_kwargs: pytest.fail("profile browser must not be restarted"),
    )

    result = controls.hermes_task_browser_restart(
        task_id, confirm=True, dry_run=False
    )

    assert result["success"] is False
    assert result["code"] == "SHARED_BROWSER_RESTART_UNAVAILABLE"


def _configure_direct_profile_browser(monkeypatch, tmp_path):
    data_root = tmp_path / "hermes-home"
    profile_home = data_root / "profiles" / "browser"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "browser:\n  cdp_url: http://127.0.0.1:9222\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(data_root))
    monkeypatch.setenv(tasks.ENABLE_SCOPED_TASKS_ENV, "1")
    monkeypatch.setenv(tasks.sessions.SESSION_ALLOWED_PROFILES_ENV, "browser")
    monkeypatch.setenv(tasks.op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(tasks.op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(tasks.op.OPERATOR_ALLOWED_PROFILES_ENV, "browser")
    monkeypatch.setenv(browser_profiles.BROWSER_ALLOWED_PROFILES_ENV, "browser")
    return data_root


def test_direct_browser_profile_list_hides_endpoint_and_attach_defaults_to_dry_run(
    monkeypatch, tmp_path
):
    data_root = _configure_direct_profile_browser(monkeypatch, tmp_path)

    listed = profile_controls.hermes_browser_profile_list()
    attached = profile_controls.hermes_browser_profile_attach(
        "browser", confirm=True, dry_run=True
    )

    assert listed == {
        "success": True,
        "profiles": [{"profile": "browser", "attached": False}],
    }
    assert "9222" not in str(listed)
    assert attached["success"] is True
    assert attached["dry_run"] is True
    assert attached["changed"] is False
    assert not (data_root / ".hermes-gpt" / "browser-profiles").exists()


def test_direct_browser_profile_actions_require_confirmation(monkeypatch, tmp_path):
    _configure_direct_profile_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        profile_controls.browser,
        "browser_session_state",
        lambda _home: {"success": True, "browser": {"status": "running"}},
    )
    calls = []
    monkeypatch.setattr(
        profile_controls.browser,
        "browser_command",
        lambda home, command, args: calls.append((home, command, args))
        or {"success": True},
    )

    dry_run = profile_controls.hermes_browser_profile_navigate(
        "browser", "https://example.com", confirm=False, dry_run=True
    )
    unconfirmed = profile_controls.hermes_browser_profile_navigate(
        "browser", "https://example.com", confirm=False, dry_run=False
    )
    applied = profile_controls.hermes_browser_profile_navigate(
        "browser", "https://example.com", confirm=True, dry_run=False
    )

    assert dry_run["success"] is True and dry_run["dry_run"] is True
    assert unconfirmed["code"] == "CONFIRMATION_REQUIRED"
    assert applied["success"] is True
    assert len(calls) == 1
    assert calls[0][1:] == ("navigate", ["https://example.com"])


def test_profile_attach_does_not_return_internal_browser_task_id(
    monkeypatch, tmp_path
):
    _configure_direct_profile_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        profile_controls.browser,
        "browser_session_state",
        lambda _home: {"success": False, "code": "BROWSER_SESSION_NOT_FOUND"},
    )
    monkeypatch.setattr(
        profile_controls.browser,
        "create_profile_browser_session",
        lambda *_args: {
            "success": True,
            "browser": {
                "task_id": "internal-browser-task-id",
                "status": "running",
                "source": "hermes_profile",
            },
        },
    )

    result = profile_controls.hermes_browser_profile_attach(
        "browser", confirm=True, dry_run=False
    )

    assert result["success"] is True
    assert "task_id" not in result
    assert "task_id" not in result["browser"]


def test_direct_profile_mutations_keep_operator_workspace_gate(monkeypatch, tmp_path):
    _configure_direct_profile_browser(monkeypatch, tmp_path)
    monkeypatch.setenv(tasks.op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setattr(
        profile_controls.browser,
        "browser_session_state",
        lambda _home: pytest.fail("browser state must not be read without workspace level"),
    )

    result = profile_controls.hermes_browser_profile_navigate(
        "browser", "https://example.com", confirm=True, dry_run=True
    )

    assert result["success"] is False
    assert result["code"] == "BROWSER_PROFILE_POLICY_DENIED"


def test_direct_profile_attach_requires_all_three_profile_allowlists(
    monkeypatch, tmp_path
):
    _configure_direct_profile_browser(monkeypatch, tmp_path)
    monkeypatch.setenv(browser_profiles.BROWSER_ALLOWED_PROFILES_ENV, "another-profile")

    result = profile_controls.hermes_browser_profile_attach(
        "browser", confirm=True, dry_run=False
    )

    assert result["success"] is False
    assert result["code"] == "BROWSER_PROFILE_ACCESS_DENIED"
