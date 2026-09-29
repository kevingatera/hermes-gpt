import json
import shutil
import subprocess

import yaml

import operator_session_task_profile as task_profile


def _mock_clone(monkeypatch, root, source_profile):
    source = root if source_profile == "default" else root / "profiles" / source_profile

    def run(command, **_kwargs):
        assert command[1:3] == ["profile", "create"]
        assert "--clone-all" in command
        assert "--no-alias" in command
        task_home = root / "profiles" / command[3]
        task_home.mkdir(parents=True)
        (task_home / "config.yaml").write_text(
            (source / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (task_home / ".env").write_text(
            (source / ".env").read_text(encoding="utf-8"), encoding="utf-8"
        )
        if (source / "mcp").is_dir():
            shutil.copytree(source / "mcp", task_home / "mcp")
        (task_home / "sessions").mkdir()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(task_profile.subprocess, "run", run)


def test_prepare_task_profile_clones_profile_resources_and_private_state(
    monkeypatch, tmp_path
):
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "profiles").mkdir()
    (root / "config.yaml").write_text(
        "model:\n  provider: custom-provider\n  default: custom-provider/model-v3\n"
        f"mcp_servers:\n  local-server:\n    command: {root}/mcp/server.py\n"
        f"    args: [{root}/mcp/settings.json]\n",
        encoding="utf-8",
    )
    (root / "mcp").mkdir()
    (root / "mcp" / "server.py").write_text("server", encoding="utf-8")
    (root / "mcp" / "settings.json").write_text("{}", encoding="utf-8")
    (root / ".env").write_text("CUSTOM_PROVIDER_KEY=profile-secret\n", encoding="utf-8")
    task_id = "a" * 32
    task_home = root / "profiles" / task_id
    _mock_clone(monkeypatch, root, "default")

    result = task_profile.prepare_task_profile(
        task_id,
        "default",
        task_home,
        hermes_root=root,
        executable="/opt/hermes/bin/hermes",
        source_home=root,
    )

    assert result == task_home
    assert "custom-provider" in (task_home / "config.yaml").read_text(encoding="utf-8")
    assert "profile-secret" in (task_home / ".env").read_text(encoding="utf-8")
    task_config = yaml.safe_load((task_home / "config.yaml").read_text(encoding="utf-8"))
    assert task_config["mcp_servers"]["local-server"]["command"] == str(task_home / "mcp" / "server.py")
    assert task_config["mcp_servers"]["local-server"]["args"] == [str(task_home / "mcp" / "settings.json")]
    assert (task_home / "sessions").is_dir()
    assert json.loads((task_home / task_profile._PROFILE_MARKER).read_text()) == {
        "version": 1,
        "source_profile": "default",
    }
    assert task_home.stat().st_mode & 0o777 == 0o700


def test_legacy_task_upgrade_preserves_its_session_database_and_transcripts(
    monkeypatch, tmp_path
):
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "profiles").mkdir()
    (root / "config.yaml").write_text("providers: {}\n", encoding="utf-8")
    (root / ".env").write_text("PROFILE_KEY=secret\n", encoding="utf-8")
    task_id = "b" * 32
    task_home = root / "profiles" / task_id
    task_home.mkdir()
    (task_home / "state.db").write_text("task database", encoding="utf-8")
    (task_home / "sessions").mkdir()
    (task_home / "sessions" / "turn.jsonl").write_text("task transcript", encoding="utf-8")
    _mock_clone(monkeypatch, root, "default")

    task_profile.prepare_task_profile(
        task_id,
        "default",
        task_home,
        hermes_root=root,
        executable="/opt/hermes/bin/hermes",
        source_home=root,
    )

    assert (task_home / "state.db").read_text(encoding="utf-8") == "task database"
    assert (task_home / "sessions" / "turn.jsonl").read_text(encoding="utf-8") == "task transcript"
    assert (task_home / ".env").read_text(encoding="utf-8") == "PROFILE_KEY=secret\n"
    assert not list((root / "profiles").glob(".*.legacy-*"))


def test_legacy_task_upgrade_rejects_symlinked_session_files(monkeypatch, tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "profiles").mkdir()
    (root / "config.yaml").write_text("providers: {}\n", encoding="utf-8")
    (root / ".env").write_text("PROFILE_KEY=secret\n", encoding="utf-8")
    task_id = "e" * 32
    task_home = root / "profiles" / task_id
    task_home.mkdir()
    (task_home / "sessions").mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside", encoding="utf-8")
    (task_home / "sessions" / "outside.jsonl").symlink_to(outside)
    _mock_clone(monkeypatch, root, "default")

    try:
        task_profile.prepare_task_profile(
            task_id,
            "default",
            task_home,
            hermes_root=root,
            executable="/opt/hermes/bin/hermes",
            source_home=root,
        )
    except PermissionError as exc:
        assert "symbolic link" in str(exc)
    else:
        raise AssertionError("legacy task session symlink should be rejected")

    assert (task_home / "sessions" / "outside.jsonl").is_symlink()


def test_task_browser_config_adds_bridge_without_replacing_profile_resources(
    monkeypatch, tmp_path
):
    task_home = tmp_path / "task"
    task_home.mkdir()
    config = {
        "model": {"provider": "custom-provider"},
        "mcp_servers": {"existing-server": {"command": "existing"}},
        "platform_toolsets": {"cli": ["hermes-cli", "existing-server"]},
    }
    config_path = task_home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(task_profile, "_browser_state_file", lambda _home: tmp_path / "browser.json")
    task_id = "c" * 32

    server_name = task_profile.configure_task_browser(
        task_id, task_home, "/opt/hermes/bin/python"
    )
    repeated_name = task_profile.configure_task_browser(
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
    assert server_name in updated["platform_toolsets"]["cli"]


def test_cloned_absolute_symlinks_point_to_the_task_copy(tmp_path):
    source_home = tmp_path / "hermes"
    task_home = source_home / "profiles" / ("d" * 32)
    (source_home / "skills").mkdir(parents=True)
    (task_home / "skills").mkdir(parents=True)
    source_file = source_home / "skills" / "shared.md"
    task_file = task_home / "skills" / "shared.md"
    source_file.write_text("source skill", encoding="utf-8")
    task_file.write_text("cloned skill", encoding="utf-8")
    link = task_home / "shared-skill.md"
    link.symlink_to(source_file)

    task_profile._rebase_profile_symlinks(task_home, source_home)

    assert link.resolve() == task_file
    assert link.read_text(encoding="utf-8") == "cloned skill"
