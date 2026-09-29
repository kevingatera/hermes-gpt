import subprocess

import operator_session_task_runtime as task_runtime


def test_source_root_uses_hermes_install_path_from_cli_version(
    monkeypatch, tmp_path
):
    install_root = tmp_path / "hermes-install"
    (install_root / "hermes_cli").mkdir(parents=True)
    (install_root / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    (install_root / "agent").mkdir()
    wrapper = tmp_path / "bin" / "hermes"
    wrapper.parent.mkdir()
    wrapper.write_text("wrapper", encoding="utf-8")
    monkeypatch.delenv("HERMES_GPT_HERMES_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-version-command")
    call_details = {}

    def run(command, **kwargs):
        call_details.update(command=command, **kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"Hermes Agent v0.21.2\nInstall directory: {install_root}\n",
            stderr="",
        )

    monkeypatch.setattr(task_runtime.subprocess, "run", run)

    assert task_runtime._source_root(str(wrapper), None) == install_root
    assert call_details["command"] == [str(wrapper), "--version"]
    assert call_details["env"].get("OPENAI_API_KEY") is None


def test_browser_bridge_uses_the_hermes_source_virtual_environment(tmp_path):
    source_root = tmp_path / "hermes"
    python = source_root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")

    assert task_runtime._hermes_python("/usr/bin/hermes", source_root) == str(python)
