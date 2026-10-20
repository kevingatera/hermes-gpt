from pathlib import Path

import pytest

from hermes_gpt.sessions import task_mounts


def _mount_paths(candidate: Path, home: Path, task_home: Path) -> tuple[Path, ...]:
    return task_mounts.readonly_profile_runtime_paths(
        (candidate,),
        workspace=home / "workspace",
        task_home=task_home,
        hermes_data_root=home / ".hermes",
    )


def test_profile_runtime_mount_rejects_secret_subpaths(tmp_path):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("a" * 32)
    task_home.mkdir(parents=True)

    protected_paths = (
        home / ".ssh",
        home / ".aws",
        home / ".config" / "gcloud",
        home / "private-mcp-runtime",
        home / "mcp-tokens",
    )
    for path in protected_paths:
        path.mkdir(parents=True)
        with pytest.raises(PermissionError, match="protected runtime path"):
            _mount_paths(path, home, task_home)

    runtime = home / "projects" / "reader" / ".venv"
    runtime.mkdir(parents=True)
    (runtime / ".env").write_text("PROFILE_KEY=private", encoding="utf-8")
    with pytest.raises(PermissionError, match="protected runtime path"):
        _mount_paths(runtime, home, task_home)

    runtime_with_named_secret = home / "projects" / "service" / ".venv"
    runtime_with_named_secret.mkdir(parents=True)
    (runtime_with_named_secret / "provider-token.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(PermissionError, match="protected runtime path"):
        _mount_paths(runtime_with_named_secret, home, task_home)

    runtime_with_secret_link = home / "projects" / "linked" / ".venv"
    runtime_with_secret_link.mkdir(parents=True)
    ssh_directory = home / ".ssh"
    (runtime_with_secret_link / "credentials").symlink_to(
        ssh_directory, target_is_directory=True
    )
    with pytest.raises(PermissionError, match="protected runtime path"):
        _mount_paths(runtime_with_secret_link, home, task_home)


def test_profile_runtime_mount_skips_home_and_hermes_parent_directories(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("b" * 32)
    hermes_data_root = home / ".hermes"
    home.mkdir()
    task_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    workspace = home / "workspace"
    workspace.mkdir()

    for path in (home, home.parent, hermes_data_root):
        assert task_mounts.readonly_profile_runtime_paths(
            (path,), workspace, task_home, hermes_data_root
        ) == ()


def test_profile_runtime_mount_keeps_explicit_nonsecret_mcp_runtime(tmp_path):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("c" * 32)
    runtime = home / "projects" / "reader" / ".venv"
    runtime.mkdir(parents=True)
    task_home.mkdir(parents=True)
    workspace = home / "workspace"
    workspace.mkdir()

    assert task_mounts.readonly_profile_runtime_paths(
        (runtime,), workspace, task_home, home / ".hermes"
    ) == (runtime,)


@pytest.mark.parametrize("name", [".env", "auth.json", "provider-token.json"])
def test_profile_runtime_mount_rejects_secret_named_symlink(tmp_path, name):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("e" * 32)
    task_home.mkdir(parents=True)
    runtime = home / "runtime"
    runtime.mkdir()
    target = runtime / "settings.dat"
    target.write_text("fixture", encoding="utf-8")
    alias = runtime / name
    alias.symlink_to(target)

    # Both a containing directory and a directly configured alias must refuse
    # the secret-looking name, regardless of the target's ordinary basename.
    for candidate in (runtime, alias):
        with pytest.raises(PermissionError, match="protected runtime path"):
            _mount_paths(candidate, home, task_home)


def test_profile_runtime_mount_skips_external_hermes_ancestor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data_root = tmp_path / "storage" / "hermes"
    task_home = data_root / "profiles" / ("f" * 32)
    task_home.mkdir(parents=True)
    (data_root / ".env").write_text("fixture", encoding="utf-8")
    workspace = home / "workspace"
    workspace.mkdir()

    assert task_mounts.readonly_profile_runtime_paths(
        (data_root.parent,), workspace, task_home, data_root
    ) == ()


def test_profile_runtime_mount_allows_standard_library_secret_named_modules(
    tmp_path,
):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("d" * 32)
    runtime = home / "python" / "lib"
    (runtime / "cookiejar0.2").mkdir(parents=True)
    task_home.mkdir(parents=True)
    (runtime / "cookiejar0.2" / "cookiejar.tcl").write_text("", encoding="utf-8")
    for module in ("token.py", "tokenize.py", "secrets.py", "tokens.pyi"):
        (runtime / module).write_text("", encoding="utf-8")
    (runtime / "secrets_introspect.xml").write_text("", encoding="utf-8")
    workspace = home / "workspace"
    workspace.mkdir()

    assert task_mounts.readonly_profile_runtime_paths(
        (runtime,), workspace, task_home, home / ".hermes"
    ) == (runtime,)


@pytest.mark.parametrize("extra", [None, "data", "symlink"])
def test_typeshed_credentials_package_requires_only_regular_stubs(tmp_path, extra):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("a" * 32)
    task_home.mkdir(parents=True)
    runtime = home / "runtime"
    package = runtime / "typeshed" / "stubs" / "docker" / "docker" / "credentials"
    package.mkdir(parents=True)
    (package / "__init__.pyi").write_text("", encoding="utf-8")
    (package / "store.pyi").write_text("", encoding="utf-8")
    if extra == "data":
        (package / "settings.json").write_text("{}", encoding="utf-8")
    elif extra == "symlink":
        (package / "alias.pyi").symlink_to(package / "store.pyi")
    if extra is None:
        assert _mount_paths(runtime, home, task_home) == (runtime,)
    else:
        with pytest.raises(PermissionError, match="protected runtime path"):
            _mount_paths(runtime, home, task_home)


def test_credentials_directory_outside_typeshed_remains_protected(tmp_path):
    home = tmp_path / "home"
    task_home = home / ".hermes" / "profiles" / ("b" * 32)
    task_home.mkdir(parents=True)
    runtime = home / "runtime"
    package = runtime / "credentials"
    package.mkdir(parents=True)
    (package / "__init__.pyi").write_text("", encoding="utf-8")
    with pytest.raises(PermissionError, match="protected runtime path"):
        _mount_paths(runtime, home, task_home)
