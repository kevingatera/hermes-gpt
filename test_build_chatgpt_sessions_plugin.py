from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
BUILDER = ROOT / "tools" / "build_chatgpt_sessions_plugin.py"


def _build(app_id: str, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), "--app-id", app_id, "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_builder_creates_plugin_and_normalizes_chatgpt_app_id(tmp_path: Path) -> None:
    output = tmp_path / "hermes-sessions"

    result = _build("plugin_asdk_app_test-123", output)

    assert result.returncode == 0, result.stderr
    app_manifest = json.loads((output / ".app.json").read_text(encoding="utf-8"))
    plugin_manifest = json.loads((output / "plugin.json").read_text(encoding="utf-8"))
    openai_extension = plugin_manifest["extensions"]["com.openai"]

    assert app_manifest == {"apps": {"hermes-gpt": {"id": "asdk_app_test-123"}}}
    assert openai_extension["apps"] == "./.app.json"
    skill = output / "skills" / "hermes-control" / "SKILL.md"
    assert skill.is_file()
    skill_text = skill.read_text(encoding="utf-8")
    assert "Neither workflow needs a separate provider key." in skill_text
    assert "keeping\n  session history separate" in skill_text
    assert not (ROOT / "plugins" / "hermes-sessions" / ".app.json").exists()


@pytest.mark.parametrize("app_id", ["", "connector_bad id", "plugin_not-an-app-id"])
def test_builder_rejects_invalid_app_ids_without_creating_output(
    tmp_path: Path, app_id: str
) -> None:
    output = tmp_path / "hermes-sessions"

    result = _build(app_id, output)

    assert result.returncode != 0
    assert not output.exists()


def test_builder_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "hermes-sessions"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    result = _build("plugin_asdk_app_test-123", output)

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep"
