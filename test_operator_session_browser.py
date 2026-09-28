from __future__ import annotations

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
