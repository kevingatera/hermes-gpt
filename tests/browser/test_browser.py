from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hermes_gpt.browser import session as browser
from hermes_gpt.browser import state as browser_state


def _use_test_browser_executable(monkeypatch, executable):
    def resolver(_root):
        return str(executable)

    monkeypatch.setattr(browser, "_browser_executable", resolver)
    monkeypatch.setattr(browser_state, "_browser_executable", resolver)


def test_browser_state_is_outside_the_writable_task_home(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    _use_test_browser_executable(monkeypatch, executable)
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: {"success": True, "data": {}})

    started = browser.create_browser_session(task_id, task_home, None)
    state_file = browser.browser_state_file(task_home)

    assert started["success"] is True
    assert state_file.parent.name == task_id
    assert state_file.parent.parent.name == ".managed-browser"
    assert state_file.parent.stat().st_mode & 0o777 == 0o700
    assert state_file.parent.parent.stat().st_mode & 0o777 == 0o700
    assert task_home not in state_file.parents
    assert json.loads(state_file.read_text(encoding="utf-8"))["session_name"] == f"hg_{task_id[:16]}"
    assert browser.browser_state_file_command(state_file, "snapshot")["success"] is True
    assert browser.browser_command(task_home, "close")["success"] is True
    assert browser.browser_session_state(task_home)["browser"]["status"] == "closed"
    browser.delete_browser_state(task_home)
    shutil.rmtree(browser_state._socket_path(task_id), ignore_errors=True)


def test_browser_state_migrates_from_shared_descriptor_directory(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    _use_test_browser_executable(monkeypatch, executable)
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: {"success": True, "data": {}})
    browser.create_browser_session(task_id, task_home, None)

    current_path = browser_state._state_path(task_home)
    legacy_path = browser_state._legacy_state_path(task_home)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.replace(legacy_path)
    current_path.parent.rmdir()

    migrated_path = browser.browser_state_file(task_home)

    assert migrated_path == current_path
    assert migrated_path.is_file()
    assert not legacy_path.exists()
    assert browser.browser_state_file_command(migrated_path, "snapshot")["success"] is True
    browser.delete_browser_state(task_home)
    shutil.rmtree(browser_state._socket_path(task_id), ignore_errors=True)


def test_browser_rejects_malformed_urls_before_running_command(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    _use_test_browser_executable(monkeypatch, executable)
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: {"success": True, "data": {}})
    browser.create_browser_session(task_id, task_home, None)
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: pytest.fail("invalid URL reached the browser"))

    result = browser.browser_command(task_home, "navigate", ["http://[broken"])

    assert result["success"] is False
    assert result["code"] == "INVALID_URL"
    browser.delete_browser_state(task_home)
    shutil.rmtree(browser_state._socket_path(task_id), ignore_errors=True)


def test_browser_session_status_redacts_current_url_secrets(monkeypatch, tmp_path):
    task_home = tmp_path / "task"
    task_home.mkdir()
    monkeypatch.setattr(
        browser,
        "_read_task_state",
        lambda _home: {"status": "running", "browser_source": "hermes_profile"},
    )

    def run(_state, command, _args):
        if command == "session":
            return {"success": True, "data": {"active": True, "pageCount": 1}}
        return {
            "success": True,
            "data": {
                "url": (
                    "https://alice:password@example.test/path?code=oauth-code"
                    "&q=public-search#access_token=fragment-token"
                )
            },
        }

    monkeypatch.setattr(browser, "_run", run)

    result = browser.browser_session_state(task_home)
    current_url = result["browser"]["current_url"]

    assert "alice:password" not in current_url
    assert "oauth-code" not in current_url
    assert "fragment-token" not in current_url
    assert "q=public-search" in current_url


def test_browser_restart_reuses_its_socket_directory(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    _use_test_browser_executable(monkeypatch, executable)
    commands = []

    def run(_state, command, *_args, **_kwargs):
        commands.append(command)
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser, "_run", run)
    assert browser.create_browser_session(task_id, task_home, None)["success"] is True
    socket_dir = browser_state._socket_path(task_id)

    restarted = browser.create_browser_session(task_id, task_home, None)

    assert restarted["success"] is True
    assert commands == ["open", "close", "open"]
    assert socket_dir.is_dir()
    browser.delete_browser_state(task_home)
    shutil.rmtree(socket_dir, ignore_errors=True)


def test_profile_browser_uses_port_only_and_cannot_be_closed(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    _use_test_browser_executable(monkeypatch, executable)
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["env"]))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True, "data": {"snapshot": "current page"}}),
            stderr="",
        )

    monkeypatch.setattr(browser.subprocess, "run", run)

    attached = browser.create_profile_browser_session(task_id, task_home, tmp_path, 9222)
    closed = browser.browser_command(task_home, "close")

    assert attached["success"] is True
    assert attached["browser"]["source"] == "hermes_profile"
    argv, env = calls[0]
    assert argv[1:3] == ["--cdp", "9222"]
    assert "--cdp" not in env
    assert "9222" not in json.dumps(attached)
    assert closed["code"] == "SHARED_BROWSER_CLOSE_UNAVAILABLE"
    assert len(calls) == 1

    browser.delete_browser_state(task_home)
    shutil.rmtree(browser_state._socket_path(task_id), ignore_errors=True)
