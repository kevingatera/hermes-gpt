"""Resolve checkout files without treating installed code as writable state."""

import pytest

from hermes_gpt import paths, versioning


@pytest.fixture(autouse=True)
def clear_root_cache():
    paths.source_root.cache_clear()
    yield
    paths.source_root.cache_clear()


def test_checkout_paths_and_version_follow_the_package(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    package = root / "src" / "hermes_gpt"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-gpt"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    monkeypatch.setattr(paths, "PACKAGE_DIR", package)
    assert paths.source_root() == root
    assert paths.project_root() == root
    assert paths.import_root() == root / "src"
    assert paths.web_dist_dir() == root / "web" / "dist"
    assert versioning.get_version() == "9.9.9"


def test_installed_paths_do_not_use_an_unrelated_ancestor(monkeypatch, tmp_path):
    package = tmp_path / "venv" / "lib" / "site-packages" / "hermes_gpt"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-gpt"\n', encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(paths, "PACKAGE_DIR", package)
    assert paths.source_root() is None
    assert paths.project_root() == workspace
    assert paths.import_root() == package.parent
    assert paths.web_dist_dir() is None


@pytest.mark.skipif(paths.os.name == "nt", reason="POSIX state-directory convention")
def test_installed_state_is_outside_the_package(monkeypatch, tmp_path):
    package = tmp_path / "site-packages" / "hermes_gpt"
    package.mkdir(parents=True)
    state = tmp_path / "state"
    monkeypatch.setattr(paths, "PACKAGE_DIR", package)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    assert paths.runtime_state_dir() == state / "hermes-gpt"
    assert package not in paths.runtime_state_dir().parents


def test_checkout_with_missing_version_does_not_use_installed_metadata(
    monkeypatch, tmp_path
):
    package = tmp_path / "src" / "hermes_gpt"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-gpt"\n', encoding="utf-8"
    )
    monkeypatch.setattr(paths, "PACKAGE_DIR", package)
    monkeypatch.setattr(versioning, "distribution_version", lambda _name: "stale")
    assert versioning.get_version() == versioning.UNKNOWN_VERSION
