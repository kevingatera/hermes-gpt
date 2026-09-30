"""MCP browser configuration and runtime mounts for scoped Hermes tasks."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from hermes_gpt import paths

_BROWSER_SERVER_PREFIX = "hermes-gpt-browser-"
_SYSTEM_RUNTIME_ROOTS = {
    Path("/usr"),
    Path("/lib"),
    Path("/lib64"),
    Path("/bin"),
    Path("/sbin"),
    Path("/nix"),
}


def _mcp_server_is_enabled(config: dict[str, Any]) -> bool:
    """Match Hermes's default-on handling for MCP server enable flags."""
    value = config.get("enabled", True)
    if isinstance(value, (bool, int)):
        return bool(value)
    return not (
        isinstance(value, str)
        and value.strip().lower() in {"false", "0", "no", "off"}
    )


def configure_task_browser(
    task_id: str,
    task_home: Path,
    agent_python: str,
) -> str:
    """Add the task browser bridge to a cloned profile without replacing its MCP config."""
    config_path = Path(task_home) / "config.yaml"
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("The selected Hermes profile has an unreadable config.yaml") from exc
    else:
        config = {}
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise TypeError("The selected Hermes profile config.yaml must contain a mapping")

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise TypeError("The selected Hermes profile mcp_servers setting must contain a mapping")

    package_root = paths.import_root()
    state_file = str(_browser_state_file(task_home))
    own_names = [
        name
        for name, server in servers.items()
        if isinstance(name, str)
        and isinstance(server, dict)
        and server.get("command") == agent_python
        and isinstance(server.get("args"), list)
        and server["args"] in (["-m", "hermes_gpt.browser.bridge"], ["-m", "hermes_gpt_browser_mcp"])
        and isinstance(server.get("env"), dict)
        and server["env"].get("HERMES_GPT_BROWSER_STATE_FILE") == state_file
    ]
    short_name = f"{_BROWSER_SERVER_PREFIX}{task_id[:8]}"
    full_name = f"{_BROWSER_SERVER_PREFIX}{task_id}"
    server_name = next(
        (name for name in (short_name, full_name) if name in own_names),
        short_name if short_name not in servers else full_name,
    )
    if server_name in servers and server_name not in own_names:
        if full_name in servers and full_name not in own_names:
            raise ValueError("The selected Hermes profile already uses both task browser server names")
        server_name = full_name
    if server_name in servers and server_name not in own_names:
        raise ValueError("The selected Hermes profile already uses the task browser server name")

    server_config = {
        "command": agent_python,
        "args": ["-m", "hermes_gpt.browser.bridge"],
        "env": {
            "PYTHONPATH": str(package_root),
            "HERMES_GPT_BROWSER_STATE_FILE": state_file,
        },
        "enabled": True,
        "connect_timeout": 15,
        "timeout": 60,
        "supports_parallel_tool_calls": False,
        "tools": {"resources": False, "prompts": False},
    }
    for old_name in own_names:
        if old_name != server_name:
            servers.pop(old_name, None)
    servers[server_name] = server_config

    tools = config.setdefault("tools", {})
    if not isinstance(tools, dict):
        raise TypeError("The selected Hermes profile tools setting must contain a mapping")
    tool_search = tools.get("tool_search")
    if not isinstance(tool_search, dict):
        tool_search = {}
    # Hermes auto-defers MCP schemas behind tool_search. This private task
    # clone must expose its injected browser bridge and cloned MCP tools directly.
    tool_search["enabled"] = "off"
    tools["tool_search"] = tool_search

    platform_toolsets = config.get("platform_toolsets")
    cli_toolsets = platform_toolsets.get("cli") if isinstance(platform_toolsets, dict) else None
    if isinstance(cli_toolsets, list):
        configured_server_names = [name for name in servers if isinstance(name, str)]
        configured_servers = set(configured_server_names)
        enabled_server_names = [
            name
            for name, server in servers.items()
            if isinstance(name, str) and isinstance(server, dict) and _mcp_server_is_enabled(server)
        ]
        selected_servers = set(enabled_server_names).intersection(cli_toolsets)
        if "no_mcp" in cli_toolsets:
            # Keep an explicit MCP opt-out except for the task browser the caller enabled.
            cli_toolsets[:] = [
                name for name in cli_toolsets
                if name != "no_mcp" and name not in configured_servers
            ]
        elif not selected_servers:
            # Once one MCP server is named, Hermes treats the list as an allowlist.
            # Add only enabled servers that were implicit in the source profile.
            cli_toolsets.extend(
                name for name in enabled_server_names if name not in cli_toolsets
            )
        if server_name not in cli_toolsets:
            cli_toolsets.append(server_name)

    temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(config_path)
    return server_name


def configured_mcp_runtime_paths(task_home: Path, path_value: str | None) -> tuple[Path, ...]:
    """Find local executables and working directories used by cloned MCP servers."""
    config_path = Path(task_home) / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(config, dict):
        return ()

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return ()
    candidates: set[Path] = set()
    for server in servers.values():
        if not isinstance(server, dict) or not _mcp_server_is_enabled(server):
            continue
        command = server.get("command")
        if isinstance(command, list) and command:
            command = command[0]
        if isinstance(command, str) and command.strip():
            executable = Path(command).expanduser()
            if not executable.is_absolute():
                located = shutil.which(command, path=path_value)
                executable = Path(located) if located else Path()
            if executable.is_file():
                candidates.add(executable.parent)
                candidates.update(_python_environment_paths(executable))
                try:
                    resolved_executable = executable.resolve(strict=True)
                    candidates.add(resolved_executable.parent)
                    candidates.update(_python_environment_paths(resolved_executable))
                except OSError:
                    pass

        args = server.get("args")
        if isinstance(args, list):
            for value in args:
                if not isinstance(value, str) or not value.startswith("/"):
                    continue
                target = Path(value)
                if target.exists():
                    candidates.add(target if target.is_dir() else target.parent)

        cwd = server.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            directory = Path(cwd).expanduser()
            if directory.is_absolute() and directory.is_dir():
                candidates.add(directory.resolve())

    return tuple(sorted(candidates, key=str))


def _python_environment_paths(executable: Path) -> set[Path]:
    """Expose a configured Python environment and the base runtime behind its symlink."""
    if executable.parent.name not in {"bin", "Scripts"}:
        return set()

    environment = executable.parent.parent
    config_path = environment / "pyvenv.cfg"
    if not config_path.is_file():
        # MCP commands may use the resolved interpreter path rather than the
        # venv symlink. Mount that interpreter's runtime so its standard
        # library remains available inside the task sandbox.
        is_python = re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?",
            executable.name,
            re.IGNORECASE,
        )
        if is_python and any((environment / "lib").glob("python*")):
            return {environment.resolve(strict=True)}
        return set()

    paths: set[Path] = set()
    try:
        paths.add(environment.resolve(strict=True))
        config = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return paths

    for line in config.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip().lower() != "home":
            continue
        home = Path(value.strip()).expanduser()
        runtime = home.parent if home.name in {"bin", "Scripts"} else home
        if any(runtime == root or root in runtime.parents for root in _SYSTEM_RUNTIME_ROOTS):
            continue
        if not runtime.is_dir():
            continue
        try:
            paths.add(runtime.resolve(strict=True))
        except OSError:
            pass
        break
    return paths


def _browser_state_file(task_home: Path) -> Path:
    """Import lazily so profile cloning remains usable when browser support is disabled."""
    from hermes_gpt.browser import session as browser

    return browser.browser_state_file(task_home)


__all__ = ["configure_task_browser", "configured_mcp_runtime_paths"]
