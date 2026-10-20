"""Installed workers must not import a package supplied by the workspace."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module", ["hermes_gpt.execution.runners", "hermes_gpt.execution.codex"]
)
def test_worker_ignores_workspace_package(module, tmp_path):
    impostor = tmp_path / "hermes_gpt"
    impostor.mkdir()
    (impostor / "__init__.py").write_text(
        'raise SystemExit("UNTRUSTED_WORKSPACE_PACKAGE")\n', encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-m", module, "--worker", "invalid-job-id"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "UNTRUSTED_WORKSPACE_PACKAGE" not in result.stderr
