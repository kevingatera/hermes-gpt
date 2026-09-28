from __future__ import annotations

import json
import shutil
from uuid import uuid4

import pytest

import operator_browser as browser


def test_browser_state_is_outside_the_writable_task_home(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    monkeypatch.setattr(browser, "_browser_executable", lambda _root: str(executable))
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: {"success": True, "data": {}})

    started = browser.create_browser_session(task_id, task_home, None)
    state_file = browser.browser_state_file(task_home)

    assert started["success"] is True
    assert state_file.parent.name == ".managed-browser"
    assert task_home not in state_file.parents
    assert json.loads(state_file.read_text(encoding="utf-8"))["session_name"] == f"hg_{task_id[:16]}"
    assert browser.browser_state_file_command(state_file, "snapshot")["success"] is True
    assert browser.browser_command(task_home, "close")["success"] is True
    assert browser.browser_session_state(task_home)["browser"]["status"] == "closed"
    browser.delete_browser_state(task_home)
    shutil.rmtree(browser._socket_path(task_id), ignore_errors=True)


def test_browser_rejects_malformed_urls_before_running_command(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    monkeypatch.setattr(browser, "_browser_executable", lambda _root: str(executable))
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: {"success": True, "data": {}})
    browser.create_browser_session(task_id, task_home, None)
    monkeypatch.setattr(browser, "_run", lambda *_args, **_kwargs: pytest.fail("invalid URL reached the browser"))

    result = browser.browser_command(task_home, "navigate", ["http://[broken"])

    assert result["success"] is False
    assert result["code"] == "INVALID_URL"
    browser.delete_browser_state(task_home)
    shutil.rmtree(browser._socket_path(task_id), ignore_errors=True)


def test_browser_restart_reuses_its_socket_directory(monkeypatch, tmp_path):
    task_id = uuid4().hex
    task_home = tmp_path / "profiles" / task_id
    task_home.mkdir(parents=True)
    executable = tmp_path / "bin" / "agent-browser"
    executable.parent.mkdir()
    executable.write_text("browser cli", encoding="utf-8")
    monkeypatch.setattr(browser, "_browser_executable", lambda _root: str(executable))
    commands = []

    def run(_state, command, *_args, **_kwargs):
        commands.append(command)
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser, "_run", run)
    assert browser.create_browser_session(task_id, task_home, None)["success"] is True
    socket_dir = browser._socket_path(task_id)

    restarted = browser.create_browser_session(task_id, task_home, None)

    assert restarted["success"] is True
    assert commands == ["open", "close", "open"]
    assert socket_dir.is_dir()
    browser.delete_browser_state(task_home)
    shutil.rmtree(socket_dir, ignore_errors=True)
