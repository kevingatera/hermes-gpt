import yaml

from hermes_gpt.sessions import task_mcp


def test_task_browser_config_adds_bridge_without_replacing_profile_resources(
    monkeypatch, tmp_path
):
    task_home = tmp_path / "task"
    task_home.mkdir()
    config = {
        "model": {"provider": "custom-provider"},
        "mcp_servers": {"existing-server": {"command": "existing"}},
        "platform_toolsets": {"cli": ["hermes-cli", "existing-server"]},
        "tools": {"tool_search": {"enabled": "auto", "threshold_pct": 7}},
    }
    config_path = task_home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(task_mcp, "_browser_state_file", lambda _home: tmp_path / "browser.json")
    task_id = "c" * 32

    server_name = task_mcp.configure_task_browser(
        task_id, task_home, "/opt/hermes/bin/python"
    )
    repeated_name = task_mcp.configure_task_browser(
        task_id, task_home, "/opt/hermes/bin/python"
    )

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    task_browser_servers = [
        name for name in updated["mcp_servers"] if name.startswith("hermes-gpt-browser-")
    ]
    assert updated["model"] == config["model"]
    assert updated["mcp_servers"]["existing-server"] == {"command": "existing"}
    assert updated["mcp_servers"][server_name]["command"] == "/opt/hermes/bin/python"
    assert repeated_name == server_name
    assert task_browser_servers == [server_name]
    assert updated["platform_toolsets"]["cli"] == [
        "hermes-cli", "existing-server", server_name
    ]
    assert updated["tools"]["tool_search"] == {"enabled": "off", "threshold_pct": 7}


def test_task_browser_keeps_implicitly_enabled_mcp_servers_in_cli_toolset(
    monkeypatch, tmp_path
):
    task_home = tmp_path / "task"
    task_home.mkdir()
    config_path = task_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "scrapling": {"command": "scrapling"},
                    "disabled": {"command": "disabled", "enabled": False},
                },
                "platform_toolsets": {"cli": ["hermes-cli"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(task_mcp, "_browser_state_file", lambda _home: tmp_path / "browser.json")

    server_name = task_mcp.configure_task_browser(
        "f" * 32, task_home, "/opt/hermes/bin/python"
    )

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["platform_toolsets"]["cli"] == [
        "hermes-cli", "scrapling", server_name
    ]


def test_task_browser_overrides_only_the_task_copy_of_no_mcp_opt_out(
    monkeypatch, tmp_path
):
    task_home = tmp_path / "task"
    task_home.mkdir()
    config_path = task_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {"scrapling": {"command": "scrapling"}},
                "platform_toolsets": {"cli": ["hermes-cli", "no_mcp", "scrapling"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(task_mcp, "_browser_state_file", lambda _home: tmp_path / "browser.json")

    server_name = task_mcp.configure_task_browser(
        "1" * 32, task_home, "/opt/hermes/bin/python"
    )

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["platform_toolsets"]["cli"] == ["hermes-cli", server_name]


def test_configured_python_mcp_server_mounts_its_virtual_environment(tmp_path):
    task_home = tmp_path / "task"
    task_home.mkdir()
    environment = tmp_path / ".venv"
    base_runtime = tmp_path / "base-python"
    base_bin = base_runtime / "bin"
    base_bin.mkdir(parents=True)
    base_executable = base_bin / "python3.11"
    base_executable.write_text("python", encoding="utf-8")
    executable = environment / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(base_executable)
    (environment / "pyvenv.cfg").write_text(f"home = {base_bin}\n", encoding="utf-8")
    (task_home / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"local": {"command": str(executable)}}}),
        encoding="utf-8",
    )

    paths = task_mcp.configured_mcp_runtime_paths(task_home, None)

    assert environment.resolve() in paths
    assert base_runtime.resolve() in paths


def test_configured_python_mcp_server_mounts_resolved_interpreter_runtime(tmp_path):
    task_home = tmp_path / "task"
    task_home.mkdir()
    runtime = tmp_path / "python-runtime"
    (runtime / "bin").mkdir(parents=True)
    executable = runtime / "bin" / "python3.11"
    executable.write_text("python", encoding="utf-8")
    (runtime / "lib" / "python3.11").mkdir(parents=True)
    (task_home / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"local": {"command": str(executable)}}}),
        encoding="utf-8",
    )

    paths = task_mcp.configured_mcp_runtime_paths(task_home, None)

    assert runtime.resolve() in paths
