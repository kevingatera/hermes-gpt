"""Create a private Hermes profile for one scoped task."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from hermes_gpt.execution import runner_common
from hermes_gpt.sessions import job_store

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PROFILE_MARKER = ".hermes-gpt-task-profile.json"
_SESSION_STATE_FILES = ("state.db", "state.db-wal", "state.db-shm")


class ProfileCloneError(RuntimeError):
    """A classified clone failure without private CLI output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _profile_paths(task_id: str, hermes_root: Path | None) -> tuple[Path, Path]:
    if not _TASK_ID_RE.fullmatch(task_id or ""):
        raise ValueError("task_id has an invalid format")
    profiles_root = job_store._data_root(hermes_root) / "profiles"
    return profiles_root, profiles_root / task_id


def _create_hermes_profile(
    profile_id: str,
    source_profile: str,
    *,
    profiles_root: Path,
    executable: str,
    source_home: Path,
) -> Path:
    """Use Hermes's own clone-all rules for profile config and resources."""
    root = profiles_root.parent
    child_env = runner_common._minimal_child_env()
    child_env["HERMES_HOME"] = str(root)
    # Profile creation uses the root and explicit source name, not the sticky CLI profile.
    child_env.pop("HERMES_PROFILE", None)
    command = [
        executable,
        "profile",
        "create",
        profile_id,
        "--clone-all",
        "--clone-from",
        source_profile,
        "--no-alias",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=source_home,
            env=child_env,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Hermes profile resources could not be copied before the timeout") from exc
    except OSError as exc:
        raise RuntimeError("Hermes could not create the private task profile") from exc

    task_home = profiles_root / profile_id
    if result.returncode != 0 or not task_home.is_dir():
        # Native copytree errors contain private file paths. Classify the
        # storage error here; never return or log the raw subprocess output.
        output = (result.stdout or "") + (result.stderr or "")
        if "[Errno 28]" in output or "No space left on device" in output:
            raise ProfileCloneError(
                "TASK_PROFILE_STORAGE_FULL",
                "Insufficient disk space to clone the Hermes profile for a confined workspace task.",
            )
        raise ProfileCloneError(
            "TASK_PROFILE_CLONE_FAILED",
            "Hermes could not clone the selected Hermes profile into private task state. This is not a browser authentication check.",
        )
    return task_home


def _restore_task_session_state(old_home: Path, task_home: Path) -> None:
    """Keep a legacy task's own session database while importing full profile resources."""
    for name in _SESSION_STATE_FILES:
        source = old_home / name
        if source.is_symlink():
            raise PermissionError("Existing task session state contains an unsupported symbolic link")
        if source.is_file():
            shutil.copy2(source, task_home / name)

    old_sessions = old_home / "sessions"
    if old_sessions.is_symlink():
        raise PermissionError("Existing task session files contain an unsupported symbolic link")
    if old_sessions.is_dir():
        new_sessions = task_home / "sessions"
        if new_sessions.exists():
            if new_sessions.is_symlink():
                raise PermissionError("Cloned task session files contain an unsupported symbolic link")
            shutil.rmtree(new_sessions)
        for entry in old_sessions.rglob("*"):
            if entry.is_symlink():
                raise PermissionError("Existing task session files contain an unsupported symbolic link")
        shutil.copytree(old_sessions, new_sessions)


def _write_profile_marker(task_home: Path, source_profile: str) -> None:
    marker = task_home / _PROFILE_MARKER
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "source_profile": source_profile}, sort_keys=True),
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(marker)


def _secure_task_home(task_home: Path) -> None:
    try:
        task_home.chmod(0o700)
    except OSError as exc:
        raise PermissionError("The private task profile permissions could not be set") from exc


def _rebase_profile_symlinks(task_home: Path, source_home: Path) -> None:
    """Point cloned links back into the task copy when their target was cloned too."""
    source_home = Path(source_home).resolve(strict=True)
    for link in Path(task_home).rglob("*"):
        if not link.is_symlink():
            continue
        try:
            raw_target = Path(os.readlink(link)).expanduser()
            source_target = raw_target if raw_target.is_absolute() else link.parent / raw_target
            source_target = source_target.resolve(strict=False)
            relative_target = source_target.relative_to(source_home)
        except (OSError, ValueError):
            continue

        task_target = Path(task_home) / relative_target
        if task_target.exists():
            link.unlink()
            link.symlink_to(task_target)


def _rewrite_cloned_profile_paths(task_home: Path, source_home: Path) -> None:
    """Retarget MCP commands stored as absolute paths in the source profile."""
    config_path = Path(task_home) / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(config, dict):
        return
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return

    source = Path(source_home).resolve(strict=True)
    destination = Path(task_home).resolve(strict=True)
    source_prefix = str(source)
    destination_prefix = str(destination)
    changed = False

    def replace_path(value: Any, *, path_list: bool = False) -> Any:
        nonlocal changed
        if not isinstance(value, str):
            return value
        parts = value.split(os.pathsep) if path_list else [value]
        rewritten: list[str] = []
        for part in parts:
            if part == source_prefix or part.startswith(source_prefix + os.sep):
                relative = part[len(source_prefix):].lstrip(os.sep)
                if not relative or (destination / relative).exists():
                    part = destination_prefix + part[len(source_prefix):]
                    changed = True
            rewritten.append(part)
        return os.pathsep.join(rewritten) if path_list else rewritten[0]

    for server in servers.values():
        if not isinstance(server, dict):
            continue
        if "command" in server:
            server["command"] = replace_path(server["command"])
        args = server.get("args")
        if isinstance(args, list):
            server["args"] = [replace_path(arg) for arg in args]
        if "cwd" in server:
            server["cwd"] = replace_path(server["cwd"])
        env = server.get("env")
        if isinstance(env, dict):
            server["env"] = {
                key: replace_path(value, path_list=key in {"PATH", "PYTHONPATH", "NODE_PATH"})
                for key, value in env.items()
            }

    if changed:
        temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
        temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(config_path)


def profile_resource_runtime_paths(task_home: Path) -> tuple[Path, ...]:
    """Return external link targets the selected profile explicitly references."""
    candidates: set[Path] = set()
    for link in Path(task_home).rglob("*"):
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
        except OSError:
            continue
        try:
            target.relative_to(task_home)
        except ValueError:
            candidates.add(target)
    return tuple(sorted(candidates, key=str))


def prepare_task_profile(
    task_id: str,
    source_profile: str,
    task_home: Path,
    *,
    hermes_root: Path | None,
    executable: str,
    source_home: Path,
) -> Path:
    """Create a full profile clone or upgrade an older task without losing its session."""
    profiles_root, expected_home = _profile_paths(task_id, hermes_root)
    requested_home = Path(task_home).expanduser()
    if requested_home.resolve(strict=False) != expected_home.resolve(strict=False):
        raise PermissionError("Hermes task data is outside its private profile path")
    if requested_home.is_symlink():
        raise PermissionError("Hermes task data cannot be a symbolic link")

    marker = requested_home / _PROFILE_MARKER
    if marker.is_symlink():
        raise PermissionError("The private task profile marker cannot be a symbolic link")
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("The private task profile marker is unreadable") from exc
        if data.get("version") != 1 or data.get("source_profile") != source_profile:
            raise PermissionError("This task was created from a different Hermes profile")
        _rebase_profile_symlinks(requested_home, source_home)
        _rewrite_cloned_profile_paths(requested_home, source_home)
        _secure_task_home(requested_home)
        return requested_home.resolve(strict=True)

    profiles_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_home: Path | None = None
    if requested_home.exists() and any(requested_home.iterdir()):
        backup_home = profiles_root / f".{task_id}.legacy-{uuid4().hex}"
        requested_home.rename(backup_home)
    elif requested_home.exists():
        requested_home.rmdir()

    try:
        task_home = _create_hermes_profile(
            task_id,
            source_profile,
            profiles_root=profiles_root,
            executable=executable,
            source_home=source_home,
        )
        if backup_home is not None:
            _restore_task_session_state(backup_home, task_home)
        _rebase_profile_symlinks(task_home, source_home)
        _rewrite_cloned_profile_paths(task_home, source_home)
        _write_profile_marker(task_home, source_profile)
        _secure_task_home(task_home)
    except Exception:
        if requested_home.exists():
            shutil.rmtree(requested_home, ignore_errors=True)
        if backup_home is not None and backup_home.exists():
            backup_home.rename(requested_home)
        raise
    else:
        if backup_home is not None:
            shutil.rmtree(backup_home)
    return task_home.resolve(strict=True)


__all__ = ["prepare_task_profile", "profile_resource_runtime_paths"]
