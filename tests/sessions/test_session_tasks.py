import json

import operator_session_tasks as tasks


def _configure(monkeypatch, tmp_path, workspace):
    root = tmp_path / "hermes-home"
    (root / "profiles" / "default").mkdir(parents=True)

    def prepare_profile(task_id, source_profile, task_home, **_kwargs):
        task_home.mkdir(parents=True, exist_ok=True)
        (task_home / ".hermes-gpt-task-profile.json").write_text(
            json.dumps({"version": 1, "source_profile": source_profile}),
            encoding="utf-8",
        )
        return task_home

    monkeypatch.setattr(tasks.task_profile, "prepare_task_profile", prepare_profile)
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
    monkeypatch.setattr(tasks.job_runtime, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
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

    monkeypatch.setattr(tasks.job_runtime, "start_managed_session_job", start_managed)
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
    assert result["model"] is None
    assert result["toolsets"] == "profile-configured"
    assert result["reasoning_effort"] is None
    task_record = tasks._read_json(tasks._task_path(result["task_id"], root))
    assert task_record["browser_source"] == "disabled"
    assert wrapped["workspace"] == workspace.resolve()
    assert wrapped["writable"] is False
    assert source_root in wrapped["readonly_paths"]
    assert tasks.Path(__file__).resolve().parents[2] in wrapped["readonly_paths"]
    assert wrapped["writable_paths"] == (root / "profiles" / result["task_id"],)
    assert "--toolsets" not in wrapped["argv"]
    assert "--ignore-rules" not in wrapped["argv"]
    assert "--model" not in wrapped["argv"]
    assert "--reasoning" not in wrapped["argv"]
    assert wrapped["argv"][1] == "chat"
    assert "--usage-file" not in wrapped["argv"]
    assert "--safe-mode" not in wrapped["argv"]
    assert "--resume" not in wrapped["argv"]
    assert "hermes-gpt-browser" not in wrapped["argv"]
    assert launched["argv"][0] == "/usr/bin/bwrap"
    assert launched["child_env"]["DEEPSEEK_API_KEY"] == "test-deepseek-key-never-return-this"
    assert "DEEPSEEK_API_KEY" not in json.dumps(result)
    assert "test-deepseek-key" not in json.dumps(launched["metadata"])


def test_task_start_refuses_secret_directory_from_profile_runtime(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    ssh_directory = tmp_path / "host-home" / ".ssh"
    ssh_directory.mkdir(parents=True)
    launched = []

    monkeypatch.setattr(
        tasks.job_runtime,
        "_hermes_executable",
        lambda _root: "/opt/hermes/bin/hermes",
    )
    monkeypatch.setattr(
        tasks.runtime, "_source_root", lambda _executable, _root: source_root
    )
    monkeypatch.setattr(
        tasks.confinement, "confinement_available", lambda *, writable: True
    )
    monkeypatch.setattr(
        tasks.runtime.task_mcp,
        "configured_mcp_runtime_paths",
        lambda *_args: (ssh_directory,),
    )
    monkeypatch.setattr(
        tasks.runtime.task_profile,
        "profile_resource_runtime_paths",
        lambda _home: (),
    )
    monkeypatch.setattr(
        tasks.job_runtime,
        "start_managed_session_job",
        lambda **kwargs: launched.append(kwargs),
    )

    result = tasks.hermes_task_start(
        "Inspect the project.",
        "demo",
        confirm=True,
        dry_run=False,
        browser_enabled=False,
        hermes_root=root,
        agent_root=source_root,
    )

    assert result["success"] is False
    assert result["code"] == "TASK_START_ERROR"
    assert result["safe_message"] == (
        "Selected Hermes profile references a protected runtime path"
    )
    assert launched == []


def test_task_start_dry_run_reports_selection_without_reading_credentials(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        profile="default",
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


def test_task_start_accepts_a_custom_configured_provider_model(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        model="custom-provider/model-name",
        browser_enabled=False,
        hermes_root=root,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["model"] == "custom-provider/model-name"
    assert result["reasoning_effort"] is None
    assert not tasks._task_root(root).exists()


def test_task_start_does_not_require_a_separate_provider_key(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "authorized" / "demo"
    workspace.mkdir(parents=True)
    root = _configure(monkeypatch, tmp_path, workspace)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    monkeypatch.setattr(tasks.job_runtime, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
    monkeypatch.setattr(tasks.runtime, "_source_root", lambda _executable, _root: source_root)
    monkeypatch.setattr(tasks.confinement, "confinement_available", lambda *, writable: True)

    launched = {}

    def start_managed(**kwargs):
        launched.update(kwargs)
        return {
            "success": True,
            "job_id": "9" * 32,
            "task_id": kwargs["metadata"]["task_id"],
            "status": "running",
        }

    monkeypatch.setattr(
        tasks.job_runtime,
        "start_managed_session_job",
        start_managed,
    )

    result = tasks.hermes_task_start(
        "Inspect the readme and summarize the project.",
        "demo",
        profile="default",
        confirm=True,
        dry_run=False,
        browser_enabled=False,
        model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="high",
        hermes_root=root,
        agent_root=source_root,
    )

    assert result["success"] is True
    assert launched["argv"][launched["argv"].index("--model") + 1] == "deepseek/deepseek-v4.1-flash"
    assert launched["argv"][launched["argv"].index("--reasoning") + 1] == "high"
    assert "DEEPSEEK_API_KEY" not in json.dumps(result)
    assert "test-deepseek-key" not in json.dumps(tasks._read_json(tasks._task_path(result["task_id"], root)))


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


def test_task_summary_keeps_profile_defaults_unresolved():
    summary = tasks._public_task_summary({
        "task_id": "e" * 32,
        "model": None,
        "reasoning_effort": None,
    })

    assert summary["model"] is None
    assert summary["reasoning_effort"] is None


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
    monkeypatch.setattr(tasks.job_runtime, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
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
        tasks.job_runtime,
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
        "profile": "default",
        "allow_workspace_write": False,
        "model": None,
        "reasoning_effort": None,
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
        "toolsets": "profile-configured",
        "session_id": "20260927_203010_ab12cd",
        "latest_job_id": "c" * 32,
        "turn_count": 1,
        "status": "completed",
    }
    tasks._write_json(tasks._task_path(task_id, root), task_record)
    source_root = tmp_path / "hermes-agent"
    source_root.mkdir()
    monkeypatch.setattr(tasks.job_runtime, "_hermes_executable", lambda _root: "/opt/hermes/bin/hermes")
    monkeypatch.setattr(tasks.runtime, "_source_root", lambda _executable, _root: source_root)
    monkeypatch.setattr(tasks.confinement, "confinement_available", lambda *, writable: True)
    captured = {}

    def wrap(argv, *_args, **_kwargs):
        captured["argv"] = list(argv)
        return argv

    monkeypatch.setattr(tasks.confinement, "wrap_argv", wrap)
    monkeypatch.setattr(tasks.job_store, "_load", lambda *_args: {"status": "completed", "session_id": "20260927_203010_ab12cd"})
    monkeypatch.setattr(tasks.job_runtime, "start_managed_session_job", lambda **kwargs: {"success": True, "job_id": "d" * 32, "status": "running"})

    result = tasks.hermes_task_continue(task_id, "Now list the main modules.", confirm=True, dry_run=False, hermes_root=root)

    assert result["success"] is True
    assert captured["argv"][captured["argv"].index("--resume") + 1] == "20260927_203010_ab12cd"
    assert "--toolsets" not in captured["argv"]
    assert "--ignore-rules" not in captured["argv"]
    assert "--model" not in captured["argv"]
    assert "--reasoning" not in captured["argv"]


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
        "profile": "default",
        "allow_workspace_write": False,
        "model": "deepseek/deepseek-v4.1-flash",
        "reasoning_effort": "high",
        "browser_enabled": False,
        "toolsets": "profile-configured",
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
    assert result["toolsets"] == "profile-configured"
    assert result["browser_enabled"] is False


def test_task_model_validation_allows_any_hermes_provider_name():
    assert tasks.runtime._validate_model_and_effort("my-provider/model-v3", "high") == (
        "my-provider/model-v3",
        "high",
    )


def test_task_model_and_effort_validation_allows_profile_defaults():
    assert tasks.runtime._validate_model_and_effort(None, None) == (None, None)


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
        profile="default",
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
