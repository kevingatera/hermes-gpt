import json

import pytest

import operator_session_tasks as tasks


def _configure(monkeypatch, tmp_path, workspace):
    root = tmp_path / "hermes-home"
    (root / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv(tasks.ENABLE_SCOPED_TASKS_ENV, "1")
    monkeypatch.setenv(tasks.TASK_WORKSPACES_ENV, json.dumps({"demo": str(workspace)}))
    monkeypatch.setenv(tasks.sessions.SESSION_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setenv(tasks.op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(tasks.op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(tasks.op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(tasks.op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace.parent))
    monkeypatch.setenv(tasks.op.OPERATOR_ALLOWED_PROFILES_ENV, "default")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key-never-return-this")
    return root


def test_file_only_task_uses_selected_workspace_and_model(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    monkeypatch.setattr(tasks.sessions, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
    monkeypatch.setattr(tasks.runtime, "_source_root", lambda _executable, _root: source_root)
    monkeypatch.setattr(tasks.confinement, "confinement_available", lambda *, writable: True)
    wrapped = {}

    def wrap(argv, selected_workspace, *, writable, readonly_paths=(), writable_paths=()):
        wrapped.update({
            "argv": list(argv),
            "workspace": selected_workspace,
            "writable": writable,
            "readonly_paths": readonly_paths,
            "writable_paths": writable_paths,
        })
        return ["/usr/bin/bwrap", *argv]

    monkeypatch.setattr(tasks.confinement, "wrap_argv", wrap)
    launched = {}

    def start_managed(**kwargs):
        launched.update(kwargs)
        return {"success": True, "job_id": "a" * 32, "task_id": kwargs["metadata"]["task_id"], "status": "running"}

    monkeypatch.setattr(tasks.sessions, "start_managed_session_job", start_managed)
    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        confirm=True,
        dry_run=False,
        browser_enabled=False,
        hermes_root=root,
        agent_root=source_root,
    )

    assert result["success"] is True
    assert result["model"] == "deepseek/deepseek-v4.1-flash"
    assert result["toolsets"] == "file"
    assert result["reasoning_effort"] == "high"
    task_record = tasks._read_json(tasks._task_path(result["task_id"], root))
    assert task_record["browser_source"] == "disabled"
    assert wrapped["workspace"] == workspace.resolve()
    assert wrapped["writable"] is False
    assert source_root in wrapped["readonly_paths"]
    assert tasks.Path(__file__).resolve().parent in wrapped["readonly_paths"]
    assert wrapped["writable_paths"] == (root / "profiles" / result["task_id"],)
    assert "--toolsets" in wrapped["argv"]
    assert wrapped["argv"][wrapped["argv"].index("--toolsets") + 1] == "file"
    assert wrapped["argv"][1] == "chat"
    assert "--usage-file" not in wrapped["argv"]
    assert "--safe-mode" not in wrapped["argv"]
    assert "--resume" not in wrapped["argv"]
    assert "hermes-gpt-browser" not in wrapped["argv"]
    assert launched["argv"][0] == "/usr/bin/bwrap"
    assert launched["child_env"]["DEEPSEEK_API_KEY"] == "test-deepseek-key-never-return-this"
    assert "DEEPSEEK_API_KEY" not in json.dumps(result)
    assert "test-deepseek-key" not in json.dumps(launched["metadata"])


def test_task_start_dry_run_reports_selection_without_provider_credentials(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setattr(
        tasks.runtime,
        "_model_credentials",
        lambda *_args: pytest.fail("a dry run must not read provider credentials"),
    )

    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        credential_profile="default",
        browser_enabled=False,
        model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="high",
        hermes_root=root,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["changed"] is False
    assert result["model"] == "deepseek/deepseek-v4.1-flash"
    assert result["reasoning_effort"] == "high"
    assert result["browser_source"] == "disabled"
    assert not tasks._task_root(root).exists()


def test_task_start_requires_provider_credentials_before_creating_state(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        credential_profile="default",
        confirm=True,
        dry_run=False,
        browser_enabled=False,
        model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="high",
        hermes_root=root,
    )

    assert result["success"] is False
    assert result["code"] == "TASK_START_ERROR"
    assert "No deepseek API key" in result["safe_message"]
    assert not tasks._task_root(root).exists()


def test_task_list_is_paginated_and_projects_only_resumable_metadata(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    records = [
        {
            "task_id": task_id,
            "workspace_id": "demo",
            "workspace": str(workspace),
            "task_home": str(root / "profiles" / task_id),
            "credential_profile": "chatgpt-task",
            "session_id": "private-session-id",
            "prompt": "private prompt text",
            "provider_api_key": "must-never-appear",
            "status": "completed",
            "model": "deepseek/deepseek-v4.1-flash",
            "reasoning_effort": "high",
            "browser_enabled": True,
            "turn_count": 2,
            "created_at": created_at,
            "updated_at": created_at,
        }
        for task_id, created_at in (
            ("a" * 32, "2026-09-26T10:00:00+00:00"),
            ("b" * 32, "2026-09-27T10:00:00+00:00"),
            ("c" * 32, "2026-09-28T10:00:00+00:00"),
        )
    ]
    for record in records:
        tasks._write_json(tasks._task_path(record["task_id"], root), record)

    first = tasks.hermes_task_list(limit=2, hermes_root=root)
    second = tasks.hermes_task_list(limit=2, offset=2, hermes_root=root)

    assert first["success"] is True
    assert [item["task_id"] for item in first["tasks"]] == ["c" * 32, "b" * 32]
    assert first["returned_count"] == 2
    assert first["total_count"] == 3
    assert first["next_offset"] == 2
    assert first["has_more"] is True
    assert second["success"] is True
    assert [item["task_id"] for item in second["tasks"]] == ["a" * 32]
    assert second["has_more"] is False
    assert first["tasks"][0] == {
        "task_id": "c" * 32,
        "workspace_id": "demo",
        "status": "completed",
        "model": "deepseek/deepseek-v4.1-flash",
        "reasoning_effort": "high",
        "browser_enabled": True,
        "browser_source": "isolated",
        "headed_browser": False,
        "turn_count": 2,
        "created_at": "2026-09-28T10:00:00+00:00",
        "updated_at": "2026-09-28T10:00:00+00:00",
    }
    serialized = json.dumps(first)
    for private_value in (
        str(workspace),
        "task_home",
        "credential_profile",
        "chatgpt-task",
        "private-session-id",
        "private prompt text",
        "must-never-appear",
    ):
        assert private_value not in serialized


def test_task_summary_reports_browser_source_without_profile_name():
    records = [
        {
            "task_id": "a" * 32,
            "browser_enabled": False,
            "browser_source": "isolated",
        },
        {
            "task_id": "b" * 32,
            "browser_enabled": True,
            "browser_source": "hermes_profile",
            "browser_profile": "private-browser-profile",
        },
        {
            "task_id": "c" * 32,
            "browser_enabled": True,
            "browser_source": "isolated",
            "headed_browser": True,
        },
    ]
    summaries = [tasks._public_task_summary(record) for record in records]

    assert [summary["browser_source"] for summary in summaries] == [
        "disabled",
        "hermes_profile",
        "isolated",
    ]
    assert [summary["headed_browser"] for summary in summaries] == [
        False,
        False,
        True,
    ]
    serialized = json.dumps(summaries)
    assert "browser_profile" not in serialized
    assert "private-browser-profile" not in serialized


def test_task_list_validates_pagination_and_scoped_task_gate(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)

    assert tasks.hermes_task_list(limit=True, hermes_root=root)["code"] == "TASK_LIST_ERROR"
    assert tasks.hermes_task_list(offset=-1, hermes_root=root)["code"] == "TASK_LIST_ERROR"
    monkeypatch.delenv(tasks.ENABLE_SCOPED_TASKS_ENV)
    assert tasks.hermes_task_list(hermes_root=root)["success"] is False


def test_browser_task_mounts_private_state_dir_and_browser_symlink(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    task_id = "e" * 32
    task_home = root / "profiles" / task_id
    task_home.mkdir()

    cli_dir = tmp_path / "node" / "bin"
    target_dir = tmp_path / "node_modules" / "agent-browser" / "bin"
    cli_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    target = target_dir / "agent-browser-linux-x64"
    target.write_text("browser cli", encoding="utf-8")
    cli_path = cli_dir / "agent-browser"
    cli_path.symlink_to(target)

    state_dir = root / "profiles" / ".managed-browser" / task_id
    state_dir.mkdir(parents=True, mode=0o700)
    state_file = state_dir / f"{task_id}.json"
    socket_dir = tmp_path / "hgpt-test-socket"
    socket_dir.mkdir(mode=0o700)
    state_file.write_text(
        json.dumps({"executable": str(cli_path), "socket_dir": str(socket_dir)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tasks.runtime.browser, "browser_state_file", lambda _home: state_file)

    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    monkeypatch.setattr(tasks.sessions, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
    monkeypatch.setattr(tasks.runtime, "_source_root", lambda _executable, _root: source_root)
    monkeypatch.setattr(tasks.confinement, "confinement_available", lambda *, writable: True)
    wrapped = {}

    def wrap(argv, selected_workspace, *, writable, readonly_paths=(), writable_paths=()):
        wrapped.update({
            "argv": list(argv),
            "workspace": selected_workspace,
            "readonly_paths": readonly_paths,
            "writable_paths": writable_paths,
        })
        return ["/usr/bin/bwrap", *argv]

    monkeypatch.setattr(tasks.confinement, "wrap_argv", wrap)
    monkeypatch.setattr(
        tasks.sessions,
        "start_managed_session_job",
        lambda **kwargs: {
            "success": True,
            "job_id": "f" * 32,
            "task_id": kwargs["metadata"]["task_id"],
            "status": "running",
        },
    )
    task = {
        "task_id": task_id,
        "workspace_id": "demo",
        "workspace": str(workspace),
        "task_home": str(task_home),
        "credential_profile": "default",
        "allow_workspace_write": False,
        "model": tasks.MODEL_ID,
        "reasoning_effort": "high",
        "browser_enabled": True,
        "session_id": "",
        "turn_count": 0,
    }

    result = tasks.runtime.start_turn(
        task=task,
        prompt="Inspect the current page.",
        timeout=10,
        confirm=True,
        dry_run=False,
        hermes_root=root,
        agent_root=source_root,
        save_task=lambda _record: None,
    )

    readonly = set(wrapped["readonly_paths"])
    writable = set(wrapped["writable_paths"])
    assert result["success"] is True
    assert state_dir in readonly
    assert state_file not in readonly
    assert cli_dir in readonly
    assert target_dir in readonly
    assert task_home not in readonly
    assert task_home in writable
    assert socket_dir in writable


def test_task_continue_resumes_recorded_session(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    task_id = "b" * 32
    task_home = root / "profiles" / task_id
    task_home.mkdir(parents=True)
    task_record = {
        "task_id": task_id,
        "workspace_id": "demo",
        "workspace": str(workspace),
        "task_home": str(task_home),
        "hermes_root": str(root),
        "credential_profile": "default",
        "allow_workspace_write": True,
        "model": "deepseek/deepseek-v4.1-flash",
        "reasoning_effort": "high",
        "browser_enabled": False,
        "toolsets": "file",
        "session_id": "20260927_203010_ab12cd",
        "latest_job_id": "c" * 32,
        "turn_count": 1,
        "status": "completed",
    }
    tasks._write_json(tasks._task_path(task_id, root), task_record)
    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    monkeypatch.setattr(tasks.sessions, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
    monkeypatch.setattr(tasks.runtime, "_source_root", lambda _executable, _root: source_root)
    monkeypatch.setattr(tasks.confinement, "confinement_available", lambda *, writable: True)
    captured = {}

    def wrap(argv, *_args, **_kwargs):
        captured["argv"] = list(argv)
        return argv

    monkeypatch.setattr(tasks.confinement, "wrap_argv", wrap)
    monkeypatch.setattr(tasks.sessions, "_load", lambda *_args: {"status": "completed", "session_id": "20260927_203010_ab12cd"})
    monkeypatch.setattr(tasks.sessions, "start_managed_session_job", lambda **kwargs: {"success": True, "job_id": "d" * 32, "status": "running"})

    result = tasks.hermes_task_continue(task_id, "Now list the main modules.", confirm=True, dry_run=False, hermes_root=root)

    assert result["success"] is True
    assert captured["argv"][captured["argv"].index("--resume") + 1] == "20260927_203010_ab12cd"
    assert "--toolsets" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--toolsets") + 1] == "file"


def test_task_continue_dry_run_reports_selected_model_and_effort(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    task_id = "d" * 32
    task_home = root / "profiles" / task_id
    task_home.mkdir(parents=True)
    tasks._write_json(tasks._task_path(task_id, root), {
        "task_id": task_id,
        "workspace_id": "demo",
        "workspace": str(workspace),
        "task_home": str(task_home),
        "hermes_root": str(root),
        "credential_profile": "default",
        "allow_workspace_write": False,
        "model": tasks.MODEL_ID,
        "reasoning_effort": "high",
        "browser_enabled": False,
        "toolsets": "file",
        "session_id": "20260927_203010_ab12cd",
        "latest_job_id": "",
        "turn_count": 1,
        "status": "completed",
    })

    result = tasks.hermes_task_continue(
        task_id,
        "Show me the selected continuation settings.",
        model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="xhigh",
        hermes_root=root,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["model"] == "deepseek/deepseek-v4.1-flash"
    assert result["reasoning_effort"] == "xhigh"
    assert result["toolsets"] == "file"
    assert result["browser_enabled"] is False


def test_openrouter_model_never_uses_an_openai_key(monkeypatch, tmp_path):
    root = tmp_path / "hermes-home"
    profile_home = root / "profiles" / "default"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("OPENAI_API_KEY=openai-secret-for-test\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(PermissionError, match="No openrouter API key"):
        tasks.runtime._model_credentials("openrouter/deepseek/deepseek-v4.1-flash", "default", root)


def test_task_start_attaches_an_explicitly_allowed_hermes_browser(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    browser_profile = root / "profiles" / "browser"
    browser_profile.mkdir(parents=True)
    (browser_profile / "config.yaml").write_text(
        "browser:\n  cdp_url: ws://127.0.0.1:9222/devtools/browser/example-id\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(tasks.sessions.SESSION_ALLOWED_PROFILES_ENV, "default,browser")
    monkeypatch.setenv(tasks.op.OPERATOR_ALLOWED_PROFILES_ENV, "default,browser")
    monkeypatch.setenv(
        tasks.browser_profiles.BROWSER_ALLOWED_PROFILES_ENV, "browser"
    )
    attached = {}

    def create_profile_browser(task_id, task_home, hermes_root, cdp_port):
        attached.update(
            task_id=task_id,
            task_home=task_home,
            hermes_root=hermes_root,
            cdp_port=cdp_port,
        )
        return {"success": True}

    monkeypatch.setattr(
        tasks.browser, "create_profile_browser_session", create_profile_browser
    )
    monkeypatch.setattr(
        tasks.runtime,
        "start_turn",
        lambda **kwargs: {
            "success": True,
            "task_id": kwargs["task"]["task_id"],
            "job_id": "e" * 32,
            "status": "running",
        },
    )

    result = tasks.hermes_task_start(
        "Inspect the current browser page.",
        "demo",
        credential_profile="default",
        browser_profile="browser",
        confirm=True,
        dry_run=False,
        hermes_root=root,
    )

    assert result["success"] is True
    assert attached["cdp_port"] == 9222
    task = tasks._read_json(tasks._task_path(attached["task_id"], root))
    assert task["browser_source"] == "hermes_profile"
    assert "browser_profile" not in task
