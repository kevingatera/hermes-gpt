"""Workspace code must not replace the trusted task browser bridge."""

import os
import subprocess
import sys

import yaml

from hermes_gpt.sessions import task_mcp


def test_workspace_package_cannot_shadow_browser_bridge(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    impostor = workspace / "hermes_gpt"
    impostor.mkdir(parents=True)
    (impostor / "__init__.py").write_text(
        'raise SystemExit("UNTRUSTED_WORKSPACE_PACKAGE")\n', encoding="utf-8"
    )
    task_home = tmp_path / "task-home"
    task_home.mkdir()
    monkeypatch.setattr(
        task_mcp, "_browser_state_file", lambda _home: tmp_path / "missing-state.json"
    )
    name = task_mcp.configure_task_browser("d" * 32, task_home, sys.executable)
    config = yaml.safe_load((task_home / "config.yaml").read_text())
    server = config["mcp_servers"][name]
    result = subprocess.run(
        [server["command"], *server["args"]],
        cwd=workspace,
        env={**os.environ, **server["env"]},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "UNTRUSTED_WORKSPACE_PACKAGE" not in result.stderr
    assert "Browser session state is unavailable" in result.stderr
