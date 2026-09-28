from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import inspect
import ipaddress
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, List

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

import oauth_auth
import operator_capability_manifest as op_capability_manifest
import operator_codex as op_codex
import operator_contract as op_contract
import operator_delegations as op_delegations
import operator_diagnostics as op_diagnostics
import operator_events as op_events
import operator_export as op_export
import operator_finance as op_finance
import operator_fleet as op_fleet
import operator_job_supervisor as op_jobs
import operator_live_events as op_live_events
import operator_mission_ledger as op_mission_ledger
import operator_oauth as op_oauth
import operator_policy as op_policy
import operator_recovery as op_recovery
import operator_review as op_review
import operator_runners as op_runners
import operator_session as op_session
import operator_session_browser as op_session_browser
import operator_session_tasks as op_session_tasks
import operator_swarm as op_swarm
import operator_workspace as op_workspace
from hermes_session_history import (
    INTERNAL_CONTENT_ENV as ENABLE_SESSION_INTERNAL_CONTENT_ENV,
    MAX_EXPORT_MESSAGES,
    MAX_ID_LENGTH,
    MAX_LIST_LIMIT,
    MAX_MESSAGE_SCAN_ROWS,
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    MAX_QUERY_LENGTH,
    ReadOnlySessionStore,
    SessionSearchUnavailable as _SessionSearchUnavailable,
    allowed_message_roles as _allowed_message_roles,
    redact_error as _redact_error,
    redact_text as _redact_text,
    redact_value as _redact_value,
    safe_message as _safe_message,
    safe_search_message as _safe_search_message,
    safe_session_metadata as _safe_session_metadata,
    utf8_response_bytes as _utf8_response_bytes,
    validate_bool as _validate_bool,
    validate_limit as _validate_limit,
    validate_offset as _validate_offset,
    validate_profile as _validate_profile,
    validate_query as _validate_query,
    validate_session_id as _validate_session_id,
)
from server_hermes_profile_tools import HermesProfileTools
from server_mission_tools import MissionTools
from server_session_control_tools import SessionControlTools
from server_session_tools import SessionHistoryTools, SessionToolContext
from versioning import VERSION

LOCAL_DEV_PROFILE = "local-dev"
REMOTE_PROFILE = "remote"
UNSAFE_REMOTE_ACK = "--i-understand-this-is-unsafe"
UNSAFE_REMOTE_ENV = "HERMES_GPT_UNSAFE_REMOTE_NOAUTH"
TRUSTED_PROXY_IPS_ENV = "HERMES_GPT_TRUSTED_PROXY_IPS"
ALLOWED_HOSTS_ENV = "HERMES_GPT_ALLOWED_HOSTS"
ENABLE_WRITE_ENV = "HERMES_GPT_ENABLE_WRITE"
ENABLE_MEMORY_WRITE_ENV = "HERMES_GPT_ENABLE_MEMORY_WRITE"
ENABLE_SESSION_SEARCH_ENV = "HERMES_GPT_ENABLE_SESSION_SEARCH"
ENABLE_SESSION_CONTROL_ENV = op_session.ENABLE_SESSION_CONTROL_ENV
SESSION_ALLOWED_PROFILES_ENV = op_session.SESSION_ALLOWED_PROFILES_ENV
ENABLE_SCOPED_TASKS_ENV = op_session_tasks.ENABLE_SCOPED_TASKS_ENV
ENABLE_TERMINAL_ENV = "HERMES_GPT_ENABLE_TERMINAL"
ENABLE_VISION_ENV = "HERMES_GPT_ENABLE_VISION"
ENABLE_WEB_ENV = "HERMES_GPT_ENABLE_WEB"
CODEX_BATCH_VERSION = VERSION
NOAUTH_META = {"securitySchemes": [{"type": "noauth"}]}

MAX_RESPONSE_BYTES = 262_144
DEFAULT_SESSION_OFFSET = 0
DEFAULT_SESSION_TIMEOUT = 900

HERMES_ROOT: Path | None = None
IMPORT_ERROR: str | None = None
file_tools: Any = None
terminal_tool: Any = None
memory_tool: Any = None
skill_manager_tool: Any = None
SessionDB: Any = None
get_hermes_home: Any = None
vision_tool: Any = None
web_tool: Any = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def is_hermes_root(path: Path) -> bool:
    """Return True when ``path`` looks like a Hermes agent SOURCE root.

    Requires a regular ``tools`` package (``tools/__init__.py``) or a top-level
    ``hermes_state.py``. A bare ``tools/`` directory is not enough: stray
    namespace ``tools/`` dirs at the Hermes DATA root (e.g. an unrelated tool
    install under ``~/.hermes/tools``) must not make the data root masquerade
    as a source root — that poisoned ``sys.path`` and broke ``import tools``
    (audit t_9d200636 Class C).
    """
    if not path.exists():
        return False
    tools_dir = path / "tools"
    if tools_dir.is_dir() and (tools_dir / "__init__.py").is_file():
        return True
    return (path / "hermes_state.py").is_file()


def candidate_roots() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        env_path = Path(env_home).expanduser()
        candidates.extend([env_path, env_path / "hermes-agent"])

    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "hermes" / "hermes-agent",
            home / ".hermes" / "hermes-agent",
        ]
    )

    for package in ("hermes-agent", "hermes_agent"):
        try:
            dist = importlib.metadata.distribution(package)
            base = Path(dist.locate_file("")).resolve()
        except Exception:
            continue
        for parent in [base, *base.parents]:
            if parent.name == "hermes-agent":
                candidates.append(parent)
                break

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def find_hermes_root() -> Path:
    for candidate in candidate_roots():
        if is_hermes_root(candidate):
            return candidate
    raise RuntimeError("Could not find a Hermes Agent source root with a tools directory.")


def add_path_once(path: Path, *, prepend: bool = True) -> None:
    value = str(path)
    existing = {str(Path(p).resolve()).lower() for p in sys.path if p}
    if str(path.resolve()).lower() not in existing:
        if prepend:
            sys.path.insert(0, value)
        else:
            sys.path.append(value)


def add_hermes_to_syspath(root: Path) -> None:
    add_path_once(root)
    if os.name == "nt":
        site_packages = root / "venv" / "Lib" / "site-packages"
    else:
        candidates = sorted((root / "venv" / "lib").glob("python*/site-packages")) if (root / "venv" / "lib").exists() else []
        site_packages = candidates[0] if candidates else root / "venv" / "lib" / "site-packages"
    if site_packages.exists():
        # Keep Hermes' bundled dependencies available for Hermes internals, but do
        # not let them shadow the MCP SDK used to run this sidecar.
        add_path_once(site_packages, prepend=False)


def import_hermes() -> None:
    global HERMES_ROOT, IMPORT_ERROR, file_tools, terminal_tool, memory_tool
    global skill_manager_tool, SessionDB, get_hermes_home
    global vision_tool, web_tool
    try:
        HERMES_ROOT = find_hermes_root()
        add_hermes_to_syspath(HERMES_ROOT)
        from tools import file_tools as ft
        from tools import memory_tool as mt
        from tools import terminal_tool as tt

        file_tools = ft
        terminal_tool = tt
        memory_tool = mt

        try:
            from tools import vision_tools as vt

            vision_tool = vt
        except Exception as exc:
            eprint(f"hermes-gpt: vision tool unavailable: {exc}")

        try:
            from tools import web_tools as wt

            web_tool = wt
        except Exception as exc:
            eprint(f"hermes-gpt: web tool unavailable: {exc}")

        try:
            from tools import skill_manager_tool as smt

            skill_manager_tool = smt
        except Exception as exc:
            eprint(f"hermes-gpt: skill manager unavailable: {exc}")

        try:
            from hermes_state import SessionDB as SDB
            from hermes_state import get_hermes_home as ghh

            SessionDB = SDB
            get_hermes_home = ghh
        except Exception as exc:
            eprint(f"hermes-gpt: session search unavailable: {exc}")
    except Exception as exc:
        IMPORT_ERROR = str(exc)
        eprint(f"hermes-gpt: Hermes imports failed: {exc}")


def call_with_supported_kwargs(func: Any, **kwargs: Any) -> Any:
    params = inspect.signature(func).parameters
    supported = {key: value for key, value in kwargs.items() if key in params}
    return func(**supported)


def expand_path(value: str | None) -> str | None:
    if value is None:
        return None
    return str(Path(value).expanduser())


def require_imports() -> None:
    if IMPORT_ERROR:
        raise RuntimeError(f"Hermes imports are unavailable: {IMPORT_ERROR}")
    missing = [
        name
        for name, module in {
            "file_tools": file_tools,
            "terminal_tool": terminal_tool,
            "memory_tool": memory_tool,
        }.items()
        if module is None
    ]
    if missing:
        raise RuntimeError(f"Hermes imports are unavailable: missing {', '.join(missing)}")


def _validate_session_profile(profile: str = "default") -> str:
    """Validate a session-history profile against Hermes/operator policy."""
    return _validate_profile(profile, _default_hermes_root())


def _session_profile_db_path(profile: str = "default") -> Path:
    """Return the authorized Hermes profile's state database path."""
    canon = _validate_session_profile(profile)
    profile_home = op_policy.resolve_profile_home(canon, _default_hermes_root())
    return profile_home / "state.db"


class ReadOnlySessionAdapter(ReadOnlySessionStore):
    """Bind the reusable session store to this server's Hermes runtime."""

    def __init__(
        self,
        db_factory: Any = None,
        connection_type: type = sqlite3.Connection,
        profile: str = "default",
    ):
        super().__init__(
            db_factory=SessionDB if db_factory is None else db_factory,
            connection_type=connection_type,
            profile=profile,
            hermes_root=_default_hermes_root(),
            profile_validator=_validate_session_profile,
            profile_db_resolver=_session_profile_db_path,
        )


def skill_roots() -> list[Path]:
    roots: list[Path] = []
    hermes_home = None
    if callable(get_hermes_home):
        try:
            hermes_home = Path(get_hermes_home())
        except Exception:
            hermes_home = None
    if hermes_home is None:
        env_home = os.environ.get("HERMES_HOME")
        hermes_home = Path(env_home).expanduser() if env_home else Path.home() / ".hermes"

    roots.append(hermes_home / "skills")
    profiles = hermes_home / "profiles"
    if profiles.exists():
        roots.extend(path / "skills" for path in profiles.iterdir() if path.is_dir())
    if HERMES_ROOT:
        roots.append(HERMES_ROOT / "skills")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if resolved.exists() and key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def parse_skill_doc(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
            for line in parts[1].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip("'\"")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
    if not description:
        for line in body.splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean:
                description = clean[:180]
                break
    return {"name": name, "description": description, "path": str(path)}


def discover_skills() -> list[dict[str, str]]:
    skills: list[dict[str, str]] = []
    for root in skill_roots():
        for skill_md in root.rglob("SKILL.md"):
            try:
                skills.append(parse_skill_doc(skill_md))
            except Exception as exc:
                eprint(f"hermes-gpt: could not read skill {skill_md}: {exc}")
    return sorted(skills, key=lambda item: (item["name"].lower(), item["path"].lower()))


def clean_error(tool_name: str, exc: Exception) -> RuntimeError:
    eprint(f"hermes-gpt: {tool_name} failed: {exc}")
    return RuntimeError(f"{tool_name} failed: {exc}")


from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ToolAnnotations

from mcp_compat import HermesMCP as FastMCP

import_hermes()


def tool_meta(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    oauth_config = oauth_auth.config_from_env()
    if oauth_config is not None:
        meta: dict[str, Any] = {
            "securitySchemes": [{"type": "oauth2", "scopes": [oauth_config.scope]}]
        }
    elif oauth_auth.static_bearer_from_env() is not None:
        meta = {"securitySchemes": [{"type": "http", "scheme": "bearer"}]}
    else:
        meta = dict(NOAUTH_META)
    if extra:
        meta.update(extra)
    return meta


def hermes_read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    try:
        require_imports()
        return file_tools.read_file_tool(path=expand_path(path), offset=offset, limit=limit)
    except Exception as exc:
        raise clean_error("hermes_read_file", exc) from exc


def hermes_write_file(path: str, content: str) -> str:
    try:
        require_imports()
        return file_tools.write_file_tool(path=expand_path(path), content=content)
    except Exception as exc:
        raise clean_error("hermes_write_file", exc) from exc


def hermes_patch(
    path: str,
    old_string: str,
    new_string: str,
    mode: str = "replace",
    replace_all: bool = False,
) -> str:
    try:
        require_imports()
        return call_with_supported_kwargs(
            file_tools.patch_tool,
            mode=mode,
            path=expand_path(path),
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )
    except Exception as exc:
        raise clean_error("hermes_patch", exc) from exc


def hermes_search_files(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
) -> str:
    try:
        require_imports()
        return call_with_supported_kwargs(
            file_tools.search_tool,
            pattern=pattern,
            target=target,
            path=expand_path(path),
            file_glob=file_glob,
            limit=limit,
        )
    except Exception as exc:
        raise clean_error("hermes_search_files", exc) from exc


def hermes_run_command(command: str, timeout: int = 30, workdir: str | None = None) -> str:
    try:
        require_imports()
        if not env_enabled(ENABLE_TERMINAL_ENV):
            raise RuntimeError(f"Terminal execution is disabled. Set {ENABLE_TERMINAL_ENV}=1 to enable it.")
        capped_timeout = max(1, min(int(timeout), 120))
        return call_with_supported_kwargs(
            terminal_tool.terminal_tool,
            command=command,
            timeout=capped_timeout,
            workdir=expand_path(workdir),
        )
    except Exception as exc:
        raise clean_error("hermes_run_command", exc) from exc


def hermes_memory(
    action: str,
    target: str = "memory",
    content: str | None = None,
    old_text: str | None = None,
) -> str:
    try:
        require_imports()
        if action not in {"add", "replace", "remove", "search"}:
            raise RuntimeError("Unsupported memory action. Use add, replace, remove, or search.")
        if action in {"add", "replace", "remove"} and not env_enabled(ENABLE_MEMORY_WRITE_ENV):
            raise RuntimeError(f"Memory write actions are disabled. Set {ENABLE_MEMORY_WRITE_ENV}=1 to enable them.")
        return memory_tool.memory_tool(action=action, target=target, content=content, old_text=old_text)
    except Exception as exc:
        raise clean_error("hermes_memory", exc) from exc


def hermes_skill_list() -> str:
    try:
        require_imports()
        skills = discover_skills()
        if not skills:
            return "No Hermes skills found."
        # Deduplicate by name, keeping the first (user-level skills take priority)
        seen_names: set[str] = set()
        unique_skills: list[dict[str, str]] = []
        for skill in skills:
            if skill["name"].lower() not in seen_names:
                seen_names.add(skill["name"].lower())
                unique_skills.append(skill)
        lines = []
        for skill in unique_skills:
            desc = f" - {skill['description']}" if skill["description"] else ""
            lines.append(f"- {skill['name']}{desc}\n  {skill['path']}")
        return "\n".join(lines)
    except Exception as exc:
        raise clean_error("hermes_skill_list", exc) from exc


def hermes_skill_view(name: str) -> str:
    try:
        require_imports()
        query = name.strip().lower()
        matches = [
            skill for skill in discover_skills()
            if skill["name"].lower() == query or Path(skill["path"]).parent.name.lower() == query
        ]
        if not matches:
            return f"No skill matched {name!r}."
        if len(matches) > 1:
            return "Multiple skills matched:\n" + "\n".join(f"- {m['name']}: {m['path']}" for m in matches)
        skill_path = Path(matches[0]["path"])
        # Size guard: if file > 80KB, return bounded chunk with guidance
        MAX_VIEW_BYTES = 80_000
        file_size = skill_path.stat().st_size
        if file_size > MAX_VIEW_BYTES:
            text = skill_path.read_text(encoding="utf-8", errors="replace")
            return text[:MAX_VIEW_BYTES] + f"\n\n--- TRUNCATED (showing {MAX_VIEW_BYTES} of {file_size} bytes). Use hermes_read_file for specific sections. ---"
        return skill_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise clean_error("hermes_skill_view", exc) from exc


_session_tool_context = SessionToolContext(
    make_adapter=lambda **kwargs: ReadOnlySessionAdapter(**kwargs),
    require_imports=require_imports,
    session_history_enabled=lambda: env_enabled(ENABLE_SESSION_SEARCH_ENV),
    session_history_env_name=ENABLE_SESSION_SEARCH_ENV,
    session_control_enabled=lambda: env_enabled(ENABLE_SESSION_CONTROL_ENV),
    session_control_env_name=ENABLE_SESSION_CONTROL_ENV,
    validate_profile=lambda profile: _validate_session_profile(profile),
    get_hermes_root=lambda: _default_hermes_root(),
    get_agent_root=lambda: HERMES_ROOT,
    session_db_available=lambda: SessionDB is not None,
    continue_session=lambda *args, **kwargs: hermes_session_continue(*args, **kwargs),
    eprint=eprint,
    policy=op_policy,
    session_control=op_session,
    managed_tasks=op_session_tasks,
)
_session_history_tools = SessionHistoryTools(_session_tool_context)
_session_control_tools = SessionControlTools(_session_tool_context)

_session_error = _session_history_tools._session_error
_session_page_response = _session_history_tools._session_page_response
_session_markdown_response = _session_history_tools._session_markdown_response
hermes_bot_chat_get = _session_history_tools.hermes_bot_chat_get
hermes_bot_chat_send = _session_history_tools.hermes_bot_chat_send
hermes_session_list = _session_history_tools.hermes_session_list
hermes_session_read = _session_history_tools.hermes_session_read
hermes_session_export = _session_history_tools.hermes_session_export
hermes_session_search = _session_history_tools.hermes_session_search
hermes_session_continue = _session_control_tools.hermes_session_continue
hermes_session_send = _session_control_tools.hermes_session_send
hermes_session_job_status = _session_control_tools.hermes_session_job_status
hermes_session_job_cancel = _session_control_tools.hermes_session_job_cancel
hermes_session_job_result = _session_control_tools.hermes_session_job_result
hermes_task_list = _session_control_tools.hermes_task_list
hermes_task_workspaces = _session_control_tools.hermes_task_workspaces
hermes_task_start = _session_control_tools.hermes_task_start
hermes_task_continue = _session_control_tools.hermes_task_continue
hermes_task_status = _session_control_tools.hermes_task_status
hermes_task_result = _session_control_tools.hermes_task_result
hermes_task_cancel = _session_control_tools.hermes_task_cancel


# ---------------------------------------------------------------------------
# Hermes tool wrappers (env-gated)
# ---------------------------------------------------------------------------


def hermes_vision_analyze(image_url: str, question: str = "") -> str:
    """Analyze an image using Hermes Agent vision. Env-gated."""
    try:
        require_imports()
        if not env_enabled(ENABLE_VISION_ENV):
            raise RuntimeError(
                f"Vision analysis is disabled. Set {ENABLE_VISION_ENV}=1 to enable it."
            )
        if vision_tool is None:
            raise RuntimeError(
                "Vision tool is not available (import failed at startup)."
            )
        import asyncio

        user_prompt = question if question else "Describe this image in detail."
        result = asyncio.run(
            vision_tool.vision_analyze_tool(
                image_url=image_url, user_prompt=user_prompt
            )
        )
        return result
    except Exception as exc:
        raise clean_error("hermes_vision_analyze", exc) from exc


def hermes_web_search(query: str, limit: int = 5) -> str:
    """Search the web using Hermes Agent web_search. Env-gated."""
    try:
        require_imports()
        if not env_enabled(ENABLE_WEB_ENV):
            raise RuntimeError(
                f"Web search is disabled. Set {ENABLE_WEB_ENV}=1 to enable it."
            )
        if web_tool is None:
            raise RuntimeError(
                "Web tool is not available (import failed at startup)."
            )
        return web_tool.web_search_tool(query=query, limit=limit)
    except Exception as exc:
        raise clean_error("hermes_web_search", exc) from exc


def hermes_web_extract(
    urls: List[str],
    char_limit: int | None = None,
) -> str:
    """Extract content from web pages using Hermes Agent web_extract. Env-gated."""
    try:
        require_imports()
        if not env_enabled(ENABLE_WEB_ENV):
            raise RuntimeError(
                f"Web extract is disabled. Set {ENABLE_WEB_ENV}=1 to enable it."
            )
        if web_tool is None:
            raise RuntimeError(
                "Web tool is not available (import failed at startup)."
            )
        import asyncio

        kwargs = {}
        if char_limit is not None:
            kwargs["char_limit"] = char_limit
        result = asyncio.run(
            web_tool.web_extract_tool(urls=urls, **kwargs)
        )
        return result
    except Exception as exc:
        raise clean_error("hermes_web_extract", exc) from exc


def hermes_finance_analyze(evidence_json: str, timeout: int = 120) -> str:
    """Analyze a bounded finance.evidence/v1 packet with the local Finance profile."""
    return op_finance.hermes_finance_analyze(
        evidence_json=evidence_json,
        timeout=timeout,
        hermes_root=_default_hermes_root(),
        agent_root=HERMES_ROOT,
    )


# ---------------------------------------------------------------------------
# Operator / Owner Mode tools
# ---------------------------------------------------------------------------
#
# These wrap the operator_* modules. They are registered unconditionally
# (so MCP clients can see them and understand why they refuse), but
# mutating tools refuse unless the operator policy is explicitly enabled.
#
# Read-only tools (policy/status/audit_tail, cron list/status, skill diff,
# config get, env status, gateway status, git status/diff) work at any
# enabled level. Mutating tools refuse without sufficient level + apply_mode.


def _hermes_root_for_operator() -> Path | None:
    """Return the Hermes data root for operator operations.

    This intentionally normalizes profile-scoped HERMES_HOME values back to the
    shared Hermes data root so operator/profile tools never treat a profile
    directory or the hermes-agent source checkout as the global root.
    """
    return _default_hermes_root()


def _default_hermes_root() -> Path | None:
    """Return the default Hermes root path (the data root, not the agent source)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op_policy.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    # The Hermes data root is ~/.hermes (Windows: ~/AppData/Local/hermes).
    # The agent source root lives next to it under hermes-agent/ and is not
    # the same path.
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    # Final fallback: ~/.hermes even if it doesn't exist (so tests that
    # monkeypatch this can still pass profile_root into the operator tools).
    return Path.home() / ".hermes"


def _active_profile_name() -> str:
    """Return the active Hermes profile name, or 'default'."""
    try:
        env_home = os.environ.get("HERMES_HOME")
        if env_home:
            p = Path(env_home).expanduser().resolve()
            parts = p.parts
            if "profiles" in parts:
                idx = parts.index("profiles")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        return "default"
    except Exception:
        return "default"


# --- Policy / status / audit (always registered, read-only) ---------------


def hermes_operator_policy() -> str:
    """Return the current operator policy summary. Read-only. Never secrets."""
    try:
        policy = op_policy.OperatorPolicy()
        summary = policy.to_summary()
        summary["success"] = True
        return json.dumps(summary, indent=2)
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="operator",
                code="POLICY_SUMMARY_ERROR",
                suggested_action="Check operator environment variables.",
            ),
            indent=2,
        )


def hermes_operator_status() -> str:
    """Return operator runtime status. Read-only. Never secrets."""
    try:
        policy = op_policy.OperatorPolicy()
        project_path = str(Path(__file__).resolve().parent)
        agent_root = str(HERMES_ROOT) if HERMES_ROOT else None
        default_root = str(_default_hermes_root()) if _default_hermes_root() else None
        active_profile = _active_profile_name()

        # Discover registered operator tools by checking this module's
        # attributes. We list the names we explicitly register below.
        registered = [
            "hermes_operator_policy",
            "hermes_operator_status",
            "hermes_operator_audit_tail",
            "hermes_operator_doctor",
            "hermes_operator_snapshot",
            "hermes_release_doctor",
            "hermes_operator_recover",
            "hermes_cron_list",
            "hermes_cron_status",
            "hermes_skill_diff",
            "hermes_config_get",
            "hermes_env_status",
            "hermes_gateway_status",
            "hermes_git_status",
            "hermes_git_diff",
            "hermes_cron_run",
            "hermes_cron_pause",
            "hermes_cron_create",
            "hermes_cron_copy",
            "hermes_cron_move",
            "hermes_skill_create",
            "hermes_skill_edit",
            "hermes_skill_patch",
            "hermes_skill_write_file",
            "hermes_skill_copy",
            "hermes_skill_sync_to_default",
            "hermes_skill_delete",
            "hermes_config_set",
            "hermes_config_patch",
            "hermes_env_set_nonsecret",
            "hermes_env_copy_nonsecret",
            "hermes_gateway_restart",
            "hermes_workspace_read",
            "hermes_export_file",
            "hermes_workspace_patch",
            "hermes_workspace_write_file",
            "hermes_workspace_run_test",
            "hermes_owner_run_command",
            "hermes_owner_patch",
            "hermes_owner_write_file",
            "hermes_fleet_list",
            "hermes_fleet_status",
            "hermes_fleet_dispatch",
            "hermes_fleet_dispatch_work_order",
            "hermes_fleet_task",
            "hermes_fleet_result",
            "hermes_fleet_authority_drift",
            "hermes_mission_overview",
            "hermes_mission_health",
            "hermes_mission_profiles",
            "hermes_mission_fleet",
            "hermes_mission_codex",
            "hermes_mission_cron",
            "hermes_mission_delegations",
            "hermes_mission_failures",
            "hermes_mission_approvals",
            "hermes_mission_vault",
            "hermes_mission_usage",
            "hermes_mission_audit",
            "hermes_mission_create",
            "hermes_mission_get",
            "hermes_mission_list",
            "hermes_mission_update",
            "hermes_mission_attach",
            "hermes_mission_reconcile",
            "hermes_mission_transition",
            "hermes_mission_approve",
            "hermes_plan_create",
            "hermes_plan_get",
            "hermes_plan_list",
            "hermes_plan_validate",
            "hermes_plan_decompose",
            "hermes_plan_review",
            "hermes_plan_node_transition",
            "hermes_plan_set_status",
            "hermes_budget_set",
            "hermes_budget_get",
            "hermes_budget_check",
            "hermes_budget_record",
            "hermes_placement_score",
            "hermes_placement_candidates",
            "hermes_placement_get",
            "hermes_placement_list",
            "hermes_failure_classify",
            "hermes_failure_taxonomy",
            "hermes_recovery_matrix",
            "hermes_controller_plan_list",
            "hermes_live_events_cursor",
            "hermes_live_events_since",
            "hermes_contract_define",
            "hermes_contract_dispatch",
            "hermes_contract_validate",
            "hermes_contract_status",
            "hermes_runner_list",
            "hermes_runner_status",
            "hermes_runner_cancel",
            "hermes_delegation_dispatch",
            "hermes_delegation_get",
            "hermes_delegation_list",
            "hermes_delegation_reconcile",
            "hermes_delegation_cancel",
        ]
        result = {
            "success": True,
            "hermes_gpt_project_path": project_path,
            "hermes_agent_root": agent_root,
            "default_hermes_root": default_root,
            "active_profile": active_profile,
            "enabled": policy.enabled,
            "level": policy.level,
            "apply_mode": policy.apply_mode,
            "owner_active": policy.owner_active,
            "owner_mode_ready": policy.owner_mode_ready,
            "registered_operator_tools": registered,
            "audit_log_path": str(op_policy.audit_log_path()),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="operator",
                code="OPERATOR_STATUS_ERROR",
                suggested_action="Check HERMES_HOME and operator environment variables.",
            ),
            indent=2,
        )


def hermes_operator_audit_tail(limit: int = 20) -> str:
    """Return the last ``limit`` audit records. Read-only."""
    try:
        records = op_policy.audit_tail(limit=limit)
        return json.dumps(
            {"success": True, "count": len(records), "records": records}, indent=2
        )
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="audit",
                code="AUDIT_TAIL_ERROR",
                suggested_action="Check audit log path and permissions.",
            ),
            indent=2,
        )


def hermes_operator_doctor(profile: str = "default") -> str:
    """Run a read-only health check across operator surfaces."""
    return op_diagnostics.hermes_operator_doctor(
        profile=profile, hermes_root=_default_hermes_root()
    )


def hermes_operator_snapshot(profile: str = "default") -> str:
    """Return a single current-state summary of the operator."""
    return op_diagnostics.hermes_operator_snapshot(
        profile=profile, hermes_root=_default_hermes_root()
    )


def hermes_release_doctor(workdir: str | None = None, full_tests: bool = False, timeout: int = 180) -> str:
    """Check whether the repo/operator is safe to ship."""
    return op_diagnostics.hermes_release_doctor(
        workdir=workdir, full_tests=full_tests, timeout=timeout
    )


def hermes_operator_recover(profile: str = "default", apply: bool = False) -> str:
    """Conservative recovery sequence. Dry-run by default."""
    return op_diagnostics.hermes_operator_recover(
        profile=profile, apply=apply, hermes_root=_default_hermes_root()
    )


def hermes_swarm_reconcile(apply: bool = False) -> str:
    """Reconcile state after a restart (ADR-007): mark interrupted swarm
    stages blocked (never auto-advance) and reload the durable token store.
    Dry-run by default; apply requires workspace+direct."""
    return op_recovery.hermes_operator_reconcile(
        apply=apply, hermes_root=_default_hermes_root()
    )


def hermes_events_query(
    source: str = "",
    subject_id: str = "",
    kind: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
) -> str:
    """Query the normalized event timeline (read-only, redacted, bounded)."""
    return op_events.hermes_events_query(
        source=source,
        subject_id=subject_id,
        kind=kind,
        since=since,
        until=until,
        limit=limit,
        hermes_root=_default_hermes_root(),
    )


def hermes_events_tail(limit: int = 20) -> str:
    """Recent events across all allowed sources (read-only, redacted)."""
    return op_events.hermes_events_tail(
        limit=limit, hermes_root=_default_hermes_root()
    )


def hermes_capability_manifest(
    source: str = "",
    include_cache: bool = True,
    limit: int = 100,
) -> str:
    """Query the derived capability manifest (read-only, INV-9, bounded)."""
    return op_capability_manifest.hermes_capability_manifest(
        source=source,
        include_cache=include_cache,
        limit=limit,
        hermes_root=_default_hermes_root(),
    )


def hermes_mission_ledger(
    mission_id: str,
    source: str = "",
    cursor: int | str = 0,
    limit: int = 100,
    replay: bool = False,
) -> str:
    """Query the merged, replayable per-mission ledger (read-only, INV-9).

    ``cursor`` is an opaque watermark token (the ``next_cursor`` value from a
    previous page) or 0 to start from the beginning.
    """
    return op_mission_ledger.hermes_mission_ledger(
        mission_id=mission_id,
        source=source,
        cursor=cursor,
        limit=limit,
        replay=replay,
        hermes_root=_default_hermes_root(),
    )


def hermes_mission_ledger_replay(
    mission_id: str,
    limit: int = 500,
) -> str:
    """Replay a mission's full event history (read-only, INV-9)."""
    return op_mission_ledger.hermes_mission_ledger_replay(
        mission_id=mission_id,
        limit=limit,
        hermes_root=_default_hermes_root(),
    )


def hermes_live_events_cursor() -> str:
    """Return the durable v0.9 live-event high-water cursor."""
    return op_live_events.hermes_live_events_cursor(hermes_root=_default_hermes_root())


def hermes_live_events_since(
    cursor: int = 0,
    mission_id: str = "",
    topic: str = "",
    kind: str = "",
    limit: int = 100,
    wait_ms: int = 0,
) -> str:
    """Read or wait for durable v0.9 live events after a cursor."""
    return op_live_events.hermes_live_events_since(
        cursor=cursor,
        mission_id=mission_id,
        topic=topic,
        kind=kind,
        limit=limit,
        wait_ms=wait_ms,
        hermes_root=_default_hermes_root(),
    )


def hermes_oauth_status() -> str:
    """Durable token store status: presence/expiry only (read-only)."""
    return op_oauth.hermes_oauth_status(hermes_root=_default_hermes_root())


def hermes_oauth_revoke(
    confirm: bool = False, dry_run: bool = True, rotate_key: bool = True
) -> str:
    """Revoke durable OAuth tokens (owner + direct + confirm, pending legal)."""
    return op_oauth.hermes_oauth_revoke(
        confirm=confirm,
        dry_run=dry_run,
        rotate_key=rotate_key,
        hermes_root=_default_hermes_root(),
    )


# --- Fleet wrappers (named A2A peers only) ---------------------------------


def hermes_fleet_list() -> str:
    """List locally registered A2A fleet peers without exposing tokens."""
    return op_fleet.hermes_fleet_list()


def hermes_fleet_status(agent: str, timeout: int = 10) -> str:
    """Check metadata-only compatibility status for one registered fleet peer."""
    return op_fleet.hermes_fleet_status(agent=agent, timeout=timeout)


def hermes_fleet_dispatch(
    agent: str,
    message: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
) -> str:
    """Submit a confirmed bounded task to one registered A2A fleet peer."""
    return op_fleet.hermes_fleet_dispatch(
        agent=agent, message=message, confirm=confirm, dry_run=dry_run, timeout=timeout,
    )


def hermes_fleet_task(agent: str, task_id: str, timeout: int = 15) -> str:
    """Return a safe status summary for one task on a registered fleet peer."""
    return op_fleet.hermes_fleet_task(agent=agent, task_id=task_id, timeout=timeout)


def hermes_fleet_dispatch_work_order(
    agent: str,
    task_id: str,
    target_profile: str,
    objective: str,
    workspace: str,
    inputs: list[str],
    constraints: list[str],
    acceptance_checks: list[str],
    deliverables: list[str],
    authorization: dict[str, Any],
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
) -> str:
    """Dispatch a canonical, profile-authorized work order to a registered peer."""
    return op_fleet.hermes_fleet_dispatch_work_order(
        agent=agent, task_id=task_id, target_profile=target_profile,
        objective=objective, workspace=workspace, inputs=inputs,
        constraints=constraints, acceptance_checks=acceptance_checks,
        deliverables=deliverables, authorization=authorization,
        confirm=confirm, dry_run=dry_run, timeout=timeout,
    )


def hermes_fleet_result(agent: str, task_id: str, timeout: int = 15) -> str:
    """Return a schema-filtered safe completion bundle."""
    return op_fleet.hermes_fleet_result(agent=agent, task_id=task_id, timeout=timeout)


def hermes_fleet_authority_drift() -> str:
    """Compare registered peers, authority, roles, profiles, and Agent Cards."""
    return op_fleet.hermes_fleet_authority_drift()


# Profile administration adapters are kept separate from server registration
# and transport setup. Their bound methods retain the existing MCP tool names.
_profile_tools = HermesProfileTools(get_hermes_root=_default_hermes_root)
hermes_cron_list = _profile_tools.hermes_cron_list
hermes_cron_status = _profile_tools.hermes_cron_status
hermes_cron_run = _profile_tools.hermes_cron_run
hermes_cron_pause = _profile_tools.hermes_cron_pause
hermes_cron_copy = _profile_tools.hermes_cron_copy
hermes_cron_create = _profile_tools.hermes_cron_create
hermes_cron_move = _profile_tools.hermes_cron_move
hermes_skill_diff = _profile_tools.hermes_skill_diff
hermes_skill_create = _profile_tools.hermes_skill_create
hermes_skill_edit = _profile_tools.hermes_skill_edit
hermes_skill_patch = _profile_tools.hermes_skill_patch
hermes_skill_write_file = _profile_tools.hermes_skill_write_file
hermes_skill_copy = _profile_tools.hermes_skill_copy
hermes_skill_sync_to_default = _profile_tools.hermes_skill_sync_to_default
hermes_skill_delete = _profile_tools.hermes_skill_delete
hermes_config_get = _profile_tools.hermes_config_get
hermes_config_set = _profile_tools.hermes_config_set
hermes_config_patch = _profile_tools.hermes_config_patch
hermes_env_status = _profile_tools.hermes_env_status
hermes_env_set_nonsecret = _profile_tools.hermes_env_set_nonsecret
hermes_env_copy_nonsecret = _profile_tools.hermes_env_copy_nonsecret


# --- Gateway / workspace / git / owner wrappers --------------------------


def hermes_gateway_status(profile: str = "default") -> str:
    return op_workspace.hermes_gateway_status(
        profile=profile, hermes_root=_default_hermes_root(),
    )


def hermes_gateway_restart(profile: str = "default", dry_run: bool = True) -> str:
    return op_workspace.hermes_gateway_restart(
        profile=profile, dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


def hermes_workspace_read(path: str, offset: int = 1, limit: int = 500) -> str:
    return op_workspace.hermes_workspace_read(path=path, offset=offset, limit=limit)


def hermes_export_file(path: str) -> CallToolResult:
    return op_export.hermes_export_file(path=path)


def hermes_workspace_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    return op_workspace.hermes_workspace_patch(
        path=path, old_string=old_string, new_string=new_string,
        replace_all=replace_all, dry_run=dry_run,
    )


def hermes_workspace_write_file(path: str, content: str, dry_run: bool = True) -> str:
    return op_workspace.hermes_workspace_write_file(
        path=path, content=content, dry_run=dry_run,
    )


def hermes_workspace_run_test(command: str, workdir: str | None = None, timeout: int = 120, dry_run: bool = True) -> str:
    return op_workspace.hermes_workspace_run_test(
        command=command, workdir=workdir, timeout=timeout, dry_run=dry_run,
    )


def hermes_git_status(workdir: str) -> str:
    return op_workspace.hermes_git_status(workdir=workdir)


def hermes_git_diff(workdir: str, pathspec: str | None = None, stat: bool = False) -> str:
    return op_workspace.hermes_git_diff(workdir=workdir, pathspec=pathspec, stat=stat)


def hermes_owner_run_command(command: str, timeout: int = 120, workdir: str | None = None, dry_run: bool = True) -> str:
    return op_workspace.hermes_owner_run_command(
        command=command, timeout=timeout, workdir=workdir, dry_run=dry_run,
    )


def hermes_owner_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    return op_workspace.hermes_owner_patch(
        path=path, old_string=old_string, new_string=new_string,
        replace_all=replace_all, dry_run=dry_run,
    )


def hermes_owner_write_file(path: str, content: str, dry_run: bool = True) -> str:
    return op_workspace.hermes_owner_write_file(path=path, content=content, dry_run=dry_run)


# --- Codex background jobs ------------------------------------------------

def hermes_codex_status() -> dict[str, Any]:
    return op_codex.hermes_codex_status(_default_hermes_root())


def hermes_codex_plan(prompt: str, workdir: str, sandbox: str = "read-only", model: str | None = None,
                      ignore_user_config: bool = False, timeout: int = 900, execution_mode: str = "normal") -> dict[str, Any]:
    return op_codex.hermes_codex_plan(prompt, workdir, sandbox, model, ignore_user_config, timeout, execution_mode=execution_mode)


def hermes_codex_start(prompt: str, workdir: str, sandbox: str = "read-only", model: str | None = None,
                       ignore_user_config: bool = False, timeout: int = 900, confirm: bool = False,
                       dry_run: bool = True, execution_mode: str = "normal") -> dict[str, Any]:
    return op_codex.hermes_codex_start(prompt, workdir, sandbox, model, ignore_user_config, timeout, confirm, dry_run,
                                       _default_hermes_root(), execution_mode=execution_mode)


def hermes_codex_review_start(workdir: str, target: str = "uncommitted", instructions: str = "", model: str | None = None,
                              ignore_user_config: bool = False, timeout: int = 900, confirm: bool = False,
                              dry_run: bool = True) -> dict[str, Any]:
    return op_codex.hermes_codex_review_start(workdir, target, instructions, model, ignore_user_config, timeout, confirm, dry_run, _default_hermes_root())


def hermes_codex_jobs(limit: int = 50) -> dict[str, Any]:
    return op_codex.hermes_codex_jobs(limit, _default_hermes_root())


def hermes_codex_job_status(job_id: str) -> dict[str, Any]:
    return op_codex.hermes_codex_job_status(job_id, _default_hermes_root())


def hermes_codex_job_result(job_id: str, max_chars: int = op_codex.MAX_RESULT_CHARS) -> dict[str, Any]:
    return op_codex.hermes_codex_job_result(job_id, max_chars, _default_hermes_root())


def hermes_codex_cancel(job_id: str, confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
    return op_codex.hermes_codex_cancel(job_id, confirm, dry_run, _default_hermes_root())


# --- Durable background-job lifecycle ------------------------------------


def hermes_job_status(job_id: str, cursor: int = 0, max_lines: int = 50) -> str:
    """Read durable runner-neutral job state and a cursor-based log tail."""
    return op_jobs.hermes_job_status(
        job_id,
        cursor=cursor,
        max_lines=max_lines,
        hermes_root=_default_hermes_root(),
    )


def hermes_job_wait(
    job_id: str,
    cursor: int = 0,
    wait_seconds: int = op_jobs.MAX_WAIT_SECONDS,
    max_lines: int = 50,
) -> str:
    """Long-poll durable job state for up to 120 seconds and return early on terminal state."""
    return op_jobs.hermes_job_wait(
        job_id,
        cursor=cursor,
        wait_seconds=wait_seconds,
        max_lines=max_lines,
        hermes_root=_default_hermes_root(),
    )


_mission_tools = MissionTools(_hermes_root_for_operator)

hermes_mission_overview = _mission_tools.hermes_mission_overview
hermes_mission_health = _mission_tools.hermes_mission_health
hermes_mission_cron = _mission_tools.hermes_mission_cron
hermes_mission_fleet = _mission_tools.hermes_mission_fleet
hermes_mission_audit = _mission_tools.hermes_mission_audit
hermes_mission_profiles = _mission_tools.hermes_mission_profiles
hermes_mission_delegations = _mission_tools.hermes_mission_delegations
hermes_mission_failures = _mission_tools.hermes_mission_failures
hermes_mission_approvals = _mission_tools.hermes_mission_approvals
hermes_mission_codex = _mission_tools.hermes_mission_codex
hermes_mission_vault = _mission_tools.hermes_mission_vault
hermes_mission_usage = _mission_tools.hermes_mission_usage
hermes_mission_create = _mission_tools.hermes_mission_create
hermes_mission_get = _mission_tools.hermes_mission_get
hermes_mission_list = _mission_tools.hermes_mission_list
hermes_mission_update = _mission_tools.hermes_mission_update
hermes_mission_attach = _mission_tools.hermes_mission_attach
hermes_mission_reconcile = _mission_tools.hermes_mission_reconcile
hermes_mission_transition = _mission_tools.hermes_mission_transition
hermes_mission_approve = _mission_tools.hermes_mission_approve
hermes_plan_create = _mission_tools.hermes_plan_create
hermes_plan_get = _mission_tools.hermes_plan_get
hermes_plan_list = _mission_tools.hermes_plan_list
hermes_plan_validate = _mission_tools.hermes_plan_validate
hermes_plan_decompose = _mission_tools.hermes_plan_decompose
hermes_plan_review = _mission_tools.hermes_plan_review
hermes_plan_node_transition = _mission_tools.hermes_plan_node_transition
hermes_plan_set_status = _mission_tools.hermes_plan_set_status
hermes_budget_set = _mission_tools.hermes_budget_set
hermes_budget_get = _mission_tools.hermes_budget_get
hermes_budget_check = _mission_tools.hermes_budget_check
hermes_budget_record = _mission_tools.hermes_budget_record
hermes_placement_score = _mission_tools.hermes_placement_score
hermes_placement_candidates = _mission_tools.hermes_placement_candidates
hermes_placement_get = _mission_tools.hermes_placement_get
hermes_placement_list = _mission_tools.hermes_placement_list
hermes_failure_classify = _mission_tools.hermes_failure_classify
hermes_failure_taxonomy = _mission_tools.hermes_failure_taxonomy
hermes_recovery_matrix = _mission_tools.hermes_recovery_matrix
hermes_controller_plan_list = _mission_tools.hermes_controller_plan_list
hermes_controller_reconcile = _mission_tools.hermes_controller_reconcile
hermes_controller_status = _mission_tools.hermes_controller_status
hermes_controller_lease_list = _mission_tools.hermes_controller_lease_list
hermes_controller_trigger = _mission_tools.hermes_controller_trigger


# --- Work Contracts (v0.6 M1) ----------------------------------------------


def hermes_contract_define(contract_json: str) -> str:
    return op_contract.hermes_contract_define(contract_json=contract_json, hermes_root=_default_hermes_root())


def hermes_contract_dispatch(
    contract_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
) -> str:
    return op_contract.hermes_contract_dispatch(
        contract_json=contract_json,
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        hermes_root=_default_hermes_root(),
    )


def hermes_contract_validate(contract_json: str) -> str:
    return op_contract.hermes_contract_validate(contract_json=contract_json, hermes_root=_default_hermes_root())


def hermes_contract_status(contract_json: str) -> str:
    return op_contract.hermes_contract_status(contract_json=contract_json, hermes_root=_default_hermes_root())


# --- Pluggable Runner Backends ---------------------------------------------


def hermes_runner_list() -> str:
    return op_runners.hermes_runner_list(hermes_root=_default_hermes_root())


def hermes_runner_status(task_id: str) -> str:
    return op_runners.hermes_runner_status(task_id=task_id, hermes_root=_default_hermes_root())


def hermes_runner_cancel(
    task_id: str,
    backend: str = "",
    confirm: bool = False,
    dry_run: bool = True,
) -> str:
    return op_runners.hermes_runner_cancel(
        task_id=task_id,
        backend=backend,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


# --- Unified Delegation Lifecycle (v0.9) -----------------------------------


def hermes_delegation_dispatch(
    contract_json: str,
    mission_id: str = "",
    delegation_id: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
) -> str:
    return op_delegations.hermes_delegation_dispatch(
        contract_json=contract_json,
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        hermes_root=_default_hermes_root(),
    )


def hermes_delegation_get(delegation_id: str) -> str:
    return op_delegations.hermes_delegation_get(
        delegation_id=delegation_id,
        hermes_root=_default_hermes_root(),
    )


def hermes_delegation_list(
    mission_id: str = "",
    state: str = "",
    limit: int = 50,
) -> str:
    return op_delegations.hermes_delegation_list(
        mission_id=mission_id,
        state=state,
        limit=limit,
        hermes_root=_default_hermes_root(),
    )


def hermes_delegation_reconcile(
    delegation_id: str,
    contract_json: str = "",
    apply: bool = False,
) -> str:
    return op_delegations.hermes_delegation_reconcile(
        delegation_id=delegation_id,
        contract_json=contract_json,
        apply=apply,
        hermes_root=_default_hermes_root(),
    )


def hermes_delegation_cancel(
    delegation_id: str,
    confirm: bool = False,
    dry_run: bool = True,
) -> str:
    return op_delegations.hermes_delegation_cancel(
        delegation_id=delegation_id,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_review_accept(
    contract_sha256: str,
    task_id: str,
    assignee: str,
    reviewer: str,
    verdict: str,
    evidence_refs: list[str] | None = None,
    approval_reference: str = "",
    dry_run: bool = True,
    confirm: bool = False,
) -> str:
    """Write a review-acceptance record for a contract (owner-gated, distinct
    reviewer enforced, audited). Evidence is referenced, never copied."""
    return op_review.hermes_review_accept(
        contract_sha256=contract_sha256,
        task_id=task_id,
        assignee=assignee,
        reviewer=reviewer,
        verdict=verdict,
        evidence_refs=evidence_refs,
        approval_reference=approval_reference,
        dry_run=dry_run,
        confirm=confirm,
        hermes_root=_default_hermes_root(),
    )


# --- Swarm Orchestration (v0.6 M2) ----------------------------------------


def hermes_swarm_workflow_create(workflow_json: str, confirm: bool = False, dry_run: bool = True) -> str:
    return op_swarm.hermes_swarm_workflow_create(
        workflow_json=workflow_json,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_swarm_workflow_list() -> str:
    return op_swarm.hermes_swarm_workflow_list(hermes_root=_default_hermes_root())


def hermes_swarm_workflow_status(workflow_id: str) -> str:
    return op_swarm.hermes_swarm_workflow_status(workflow_id=workflow_id, hermes_root=_default_hermes_root())


def hermes_swarm_workflow_validate(workflow_json: str) -> str:
    return op_swarm.hermes_swarm_workflow_validate(workflow_json=workflow_json, hermes_root=_default_hermes_root())


def hermes_swarm_stage_dispatch(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
) -> str:
    return op_swarm.hermes_swarm_stage_dispatch(
        workflow_id=workflow_id,
        stage_id=stage_id,
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        hermes_root=_default_hermes_root(),
    )


def hermes_swarm_stage_advance(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
) -> str:
    return op_swarm.hermes_swarm_stage_advance(
        workflow_id=workflow_id,
        stage_id=stage_id,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_swarm_approve(workflow_id: str, confirm: bool = False, dry_run: bool = True) -> str:
    return op_swarm.hermes_swarm_approve(
        workflow_id=workflow_id,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def oauth_state_from_env() -> oauth_auth.OAuthState | None:
    config = oauth_auth.config_from_env()
    if config is None:
        return None
    state = oauth_auth.OAuthState(config)
    # v0.7 S5: restore durable tokens from the encrypted envelope so a
    # restart does not invalidate issued credentials (ADR-001). Best-effort:
    # a missing/corrupt envelope fails closed to empty stores.
    try:
        state.restore_tokens(_default_hermes_root())
    except Exception:
        pass
    return state


def auth_enabled() -> bool:
    return oauth_auth.static_bearer_from_env() is not None or oauth_auth.config_from_env() is not None


def trusted_proxy_ips_from_env() -> str:
    raw_value = os.environ.get(TRUSTED_PROXY_IPS_ENV, "").strip()
    if not raw_value:
        return ""
    addresses: list[str] = []
    for value in raw_value.split(","):
        candidate = value.strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError(f"{TRUSTED_PROXY_IPS_ENV} must contain only comma-separated IP addresses.") from exc
        if not address.is_loopback:
            raise ValueError(f"{TRUSTED_PROXY_IPS_ENV} accepts loopback proxy addresses only.")
        addresses.append(str(address))
    return ",".join(dict.fromkeys(addresses))


def authenticated_http_security_options(
    *,
    profile: str,
    host: str,
    cert: str | None,
    key: str | None,
    configured_auth: bool,
) -> tuple[bool, str]:
    if bool(cert) != bool(key):
        raise SystemExit("TLS requires both --cert and --key.")
    if profile != REMOTE_PROFILE or not configured_auth:
        return False, ""
    if cert and key:
        return False, ""
    try:
        trusted_proxies = trusted_proxy_ips_from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not trusted_proxies or not is_loopback_host(host):
        raise SystemExit(
            "Authenticated remote mode requires direct TLS (--cert and --key), or a loopback bind behind an "
            f"explicit trusted HTTPS proxy configured with {TRUSTED_PROXY_IPS_ENV}."
        )
    return True, trusted_proxies


async def health_root(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "hermes-gpt", "mcp_path": "/mcp"})


def _fleet_peer_name() -> str:
    return os.environ.get("HERMES_GPT_FLEET_PEER_NAME", "").strip() or "hermes-peer"


def _fleet_peer_url() -> str:
    return (
        os.environ.get("HERMES_GPT_FLEET_PEER_URL", "").strip()
        or f"http://{os.environ.get('HERMES_GPT_HOST', '127.0.0.1')}:{os.environ.get('HERMES_GPT_PORT', '4750')}"
    )


async def _fleet_agent_card(_request: Request) -> JSONResponse:
    """Agent Card for this Fleet peer. The fleet authority manifest pins
    expected_card_identity to HERMES_GPT_FLEET_PEER_NAME; this route attests
    that identity. No secrets, tokens, or credentials are returned."""
    peer_name = _fleet_peer_name()
    peer_url = _fleet_peer_url()
    return JSONResponse(
        {
            "protocolVersion": "1.0",
            "name": peer_name,
            "description": "Hermes Fleet peer",
            "version": os.environ.get("HERMES_GPT_FLEET_PEER_VERSION", "0.20.5"),
            "url": peer_url,
            "capabilities": {},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "hermes-agent-v1",
                    "name": "Hermes Agent",
                    "description": "Local-first Hermes Agent",
                    "tags": ["hermes", "fleet"],
                }
            ],
            "supportedInterfaces": [
                {
                    "url": peer_url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
        }
    )


def _register_fleet_local_card() -> None:
    """Publish this peer's own Agent Card for in-process loopback verification.

    Keeps the fleet drift/status tools from deadlocking when they fetch the
    card of a co-located peer over 127.0.0.1. The identity here must match the
    fleet authority manifest entry (expected_card_identity = HERMES_GPT_FLEET_PEER_NAME).
    """
    try:
        import operator_fleet as _op

        peer_name = _fleet_peer_name()
        peer_url = _fleet_peer_url()
        _op.register_local_agent_card(
            peer_url,
            {
                "protocolVersion": "1.0",
                "name": peer_name,
                "description": "Hermes Fleet peer",
                "version": os.environ.get("HERMES_GPT_FLEET_PEER_VERSION", "0.20.5"),
                "url": peer_url,
                "capabilities": {},
                "defaultInputModes": ["application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [
                    {
                        "id": "hermes-agent-v1",
                        "name": "Hermes Agent",
                        "description": "Local-first Hermes Agent",
                        "tags": ["hermes", "fleet"],
                    }
                ],
                "supportedInterfaces": [
                    {
                        "url": peer_url,
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
            },
        )
    except Exception:  # noqa: BLE001
        pass


def build_asgi_app(server: FastMCP, *, http: bool) -> Any:
    oauth_state = getattr(server, "_hermes_oauth_state", None)
    if oauth_state is not None and not http:
        raise ValueError("Built-in OAuth is supported only with streamable HTTP (--http).")
    raw_mcp_app = server.streamable_http_app() if http else server.sse_app()
    mcp_app = oauth_auth.DefaultMcpAcceptMiddleware(raw_mcp_app)
    static_bearer = oauth_auth.static_bearer_from_env() or ""

    async def live_websocket_authorized(websocket: Any) -> bool:
        """Reuse the normal Hermes HTTP auth authority for WebSocket handshakes."""
        if oauth_state is None and not static_bearer:
            return True
        admitted = False

        async def admitted_app(_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
            nonlocal admitted
            admitted = True

        auth_middleware = oauth_auth.BearerAuthMiddleware(
            admitted_app,
            oauth_state,
            static_token=static_bearer,
        )
        scope = dict(websocket.scope)
        scope.update(
            {
                "type": "http",
                "http_version": scope.get("http_version", "1.1"),
                "scheme": "http",
                "method": "POST",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
            }
        )
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: dict[str, Any]) -> None:
            return None

        await auth_middleware(scope, receive, send)
        return admitted

    routes: list[BaseRoute] = [Route("/", health_root, methods=["GET", "POST", "OPTIONS"])]
    # Fleet Agent Card (env-driven identity; generic across peers). Behind the
    # same outer Bearer/OAuth middleware as MCP. No secret material returned.
    routes.extend(
        [
            Route("/.well-known/agent-card.json", _fleet_agent_card, methods=["GET"]),
            Route("/.well-known/agent.json", _fleet_agent_card, methods=["GET"]),
        ]
    )
    if oauth_state is not None:
        async def resource_metadata(request: Request) -> JSONResponse:
            return oauth_auth.protected_resource_metadata(request, oauth_state)

        async def authorization_server_metadata(request: Request) -> JSONResponse:
            return oauth_auth.authorization_metadata(request, oauth_state)

        async def authorize(request: Request) -> Response:
            return oauth_auth.authorize(request, oauth_state)

        async def token(request: Request) -> JSONResponse:
            return await oauth_auth.token(request, oauth_state)

        routes.extend(
            [
                Route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"]),
                Route("/.well-known/oauth-protected-resource/mcp", resource_metadata, methods=["GET"]),
                Route("/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]),
                Route("/oauth/authorize", authorize, methods=["GET"]),
                Route("/oauth/token", token, methods=["POST"]),
            ]
        )
    # Mount browser UI routes before the MCP catch-all. The UI remains opt-in
    # and a missing optional UI module must not change the MCP-only server.
    ui_enabled = False
    try:
        import ui_security as _ui_security

        ui_enabled = _ui_security.ui_enabled()
    except Exception:  # noqa: BLE001
        ui_enabled = os.environ.get("HERMES_GPT_UI_ENABLED") == "1"
    if ui_enabled:
        try:
            import ui_api

            routes.extend(ui_api.routes())
        except Exception as exc:  # noqa: BLE001
            eprint(f"UI mount skipped: {exc.__class__.__name__}: {exc}")
    # v0.9 live-event delivery is read-only and remains behind the same outer
    # Bearer/OAuth middleware as MCP and the browser UI.
    routes.extend(
        op_live_events.websocket_routes(
            _default_hermes_root,
            auth_check=live_websocket_authorized,
        )
    )
    routes.append(Mount("/", app=mcp_app))
    app = Starlette(routes=routes, lifespan=raw_mcp_app.router.lifespan_context)
    issuer = oauth_state.config.issuer if oauth_state is not None else ""
    parsed_issuer = urllib.parse.urlparse(issuer)
    issuer_origin = f"{parsed_issuer.scheme}://{parsed_issuer.netloc}" if parsed_issuer.netloc else ""
    origins = [origin for origin in ("https://chatgpt.com", issuer_origin) if origin]
    return CORSMiddleware(
        oauth_auth.BearerAuthMiddleware(app, oauth_state, static_token=static_bearer),
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=86400,
    )


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 7677,
    http: bool = False,
    include_local_settings: bool = False,
) -> FastMCP:
    oauth_state = oauth_state_from_env()
    allowed_hosts = [host, f"{host}:{port}", "127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"]
    extra_allowed_hosts = [
        item.strip()
        for item in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")
        if item.strip()
    ]
    allowed_hosts.extend(extra_allowed_hosts)
    allowed_origins = ["https://chatgpt.com"]
    if oauth_state is not None:
        issuer = urllib.parse.urlparse(oauth_state.config.issuer)
        if issuer.hostname:
            allowed_hosts.append(issuer.hostname)
            if issuer.port:
                allowed_hosts.append(f"{issuer.hostname}:{issuer.port}")
        allowed_origins.append(f"{issuer.scheme}://{issuer.netloc}")
    server = FastMCP(
        "hermes-gpt",
        version=VERSION,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        sse_path="/sse",
        message_path="/messages/",
        stateless_http=http,
        json_response=http,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(dict.fromkeys(allowed_hosts)),
            allowed_origins=list(dict.fromkeys(allowed_origins)),
        ),
    )
    setattr(server, "_hermes_oauth_state", oauth_state)
    if oauth_state is not None:
        # v0.7 S5: persist every token issuance/refresh through token_store.
        # Persistence failures PROPAGATE: the strict exchange path turns them
        # into OAuth errors instead of handing out uncommitted credentials.
        def _persist(state, kind: str) -> None:
            state.persist_tokens(_default_hermes_root())

        oauth_auth.set_persist_hook(_persist)
        # Durable revocation (hermes_oauth_revoke) must also drop this
        # process's in-memory token caches, or the next issuance would
        # re-persist the revoked tokens through _persist.
        oauth_auth.set_revocation_hook(oauth_state.clear_live_tokens)
    register_tools(server)
    return server


def register_tools(server: FastMCP) -> None:
    server.add_tool(hermes_read_file, meta=tool_meta())
    server.add_tool(hermes_search_files, meta=tool_meta())
    server.add_tool(hermes_memory, meta=tool_meta())
    server.add_tool(hermes_skill_list, meta=tool_meta())
    server.add_tool(hermes_skill_view, meta=tool_meta())

    if env_enabled(ENABLE_WRITE_ENV):
        server.add_tool(hermes_write_file, meta=tool_meta())
        server.add_tool(hermes_patch, meta=tool_meta())
    if env_enabled(ENABLE_TERMINAL_ENV):
        server.add_tool(hermes_run_command, meta=tool_meta())
    if env_enabled(ENABLE_SESSION_SEARCH_ENV):
        server.add_tool(hermes_session_search, meta=tool_meta())
        server.add_tool(hermes_session_list, meta=tool_meta())
        server.add_tool(hermes_session_read, meta=tool_meta())
        server.add_tool(hermes_session_export, meta=tool_meta())
        server.add_tool(hermes_bot_chat_get, meta=tool_meta())
    if env_enabled(ENABLE_SESSION_CONTROL_ENV):
        server.add_tool(hermes_session_continue, meta=tool_meta())
        server.add_tool(hermes_session_send, meta=tool_meta())
        if env_enabled(ENABLE_SESSION_SEARCH_ENV):
            server.add_tool(hermes_bot_chat_send, meta=tool_meta())
        server.add_tool(hermes_session_job_status, meta=tool_meta())
        server.add_tool(hermes_session_job_result, meta=tool_meta())
        server.add_tool(hermes_session_job_cancel, meta=tool_meta())
    if env_enabled(ENABLE_SCOPED_TASKS_ENV):
        server.add_tool(
            hermes_task_list,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="List managed Hermes sessions for resuming", readOnlyHint=True),
        )
        server.add_tool(
            hermes_task_workspaces,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="List configured workspaces for scoped Hermes tasks", readOnlyHint=True),
        )
        server.add_tool(
            hermes_task_start,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="Start a scoped Hermes session", destructiveHint=True),
        )
        server.add_tool(
            hermes_task_continue,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="Continue a scoped Hermes session", destructiveHint=True),
        )
        server.add_tool(
            hermes_task_cancel,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="Cancel a running Hermes session turn", destructiveHint=True),
        )
        server.add_tool(
            hermes_task_status,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="Read scoped Hermes session status", readOnlyHint=True),
        )
        server.add_tool(
            hermes_task_result,
            meta=tool_meta(),
            annotations=ToolAnnotations(title="Read the latest scoped Hermes session result", readOnlyHint=True),
        )
        for tool in (
            op_session_browser.hermes_task_browser_status,
            op_session_browser.hermes_task_browser_snapshot,
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=tool.__name__.replace("_", " "), readOnlyHint=True),
            )
        for tool in (
            op_session_browser.hermes_task_browser_navigate,
            op_session_browser.hermes_task_browser_click,
            op_session_browser.hermes_task_browser_type,
            op_session_browser.hermes_task_browser_scroll,
            op_session_browser.hermes_task_browser_back,
            op_session_browser.hermes_task_browser_press,
            op_session_browser.hermes_task_browser_close,
            op_session_browser.hermes_task_browser_restart,
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=tool.__name__.replace("_", " "), destructiveHint=True),
            )
    if env_enabled(ENABLE_VISION_ENV):
        server.add_tool(hermes_vision_analyze, meta=tool_meta())
    if env_enabled(ENABLE_WEB_ENV):
        server.add_tool(hermes_web_search, meta=tool_meta())
        server.add_tool(hermes_web_extract, meta=tool_meta())
    if op_finance.finance_enabled(_default_hermes_root()):
        server.add_tool(hermes_finance_analyze, meta=tool_meta())

    # --- Operator / Owner Mode tools -----------------------------------
    #
    # Read-only tools are always registered. Mutating tools are registered
    # unconditionally too (per spec: "register with refusal so the user can
    # see why unavailable") — the wrappers above return a JSON error string
    # when the operator policy is not enabled / level is insufficient /
    # apply_mode is dry_run / owner ack is missing.
    server.add_tool(hermes_operator_policy, meta=tool_meta())
    server.add_tool(hermes_operator_status, meta=tool_meta())
    server.add_tool(hermes_operator_audit_tail, meta=tool_meta())
    server.add_tool(hermes_operator_doctor, meta=tool_meta())
    server.add_tool(hermes_operator_snapshot, meta=tool_meta())
    server.add_tool(hermes_release_doctor, meta=tool_meta())
    server.add_tool(hermes_operator_recover, meta=tool_meta())
    server.add_tool(
        hermes_swarm_reconcile,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Reconcile state after a restart (dry-run by default; apply requires workspace + direct)"
        ),
    )

    # Fleet routing: named peers in the local authenticated A2A registry only.
    server.add_tool(hermes_fleet_list, meta=tool_meta())
    server.add_tool(hermes_fleet_status, meta=tool_meta())
    server.add_tool(hermes_fleet_dispatch, meta=tool_meta())
    server.add_tool(hermes_fleet_dispatch_work_order, meta=tool_meta())
    server.add_tool(hermes_fleet_task, meta=tool_meta())
    server.add_tool(hermes_fleet_result, meta=tool_meta())
    server.add_tool(hermes_fleet_authority_drift, meta=tool_meta())

    # Cron
    server.add_tool(hermes_cron_list, meta=tool_meta())
    server.add_tool(hermes_cron_status, meta=tool_meta())
    server.add_tool(hermes_cron_create, meta=tool_meta())
    server.add_tool(hermes_cron_run, meta=tool_meta())
    server.add_tool(hermes_cron_pause, meta=tool_meta())
    server.add_tool(hermes_cron_copy, meta=tool_meta())
    server.add_tool(hermes_cron_move, meta=tool_meta())

    # Mission Control (v0.6 M0): read-only operational view. Registered
    # unconditionally; each surface enforces the per-client allowlist
    # (HERMES_GPT_MISSION_ALLOWED_SURFACES) and audits every call.
    for _mission_tool in (
        hermes_mission_overview,
        hermes_mission_health,
        hermes_mission_profiles,
        hermes_mission_fleet,
        hermes_mission_codex,
        hermes_mission_cron,
        hermes_mission_delegations,
        hermes_mission_failures,
        hermes_mission_approvals,
        hermes_mission_vault,
        hermes_mission_usage,
        hermes_mission_audit,
    ):
        server.add_tool(_mission_tool, meta=tool_meta())

    # First-class durable Mission runtime (v0.9). Authority is enforced by
    # operator_mission_runtime; list/get are read-only, mutations are
    # workspace/direct and final approval is Owner-gated.
    for _mission_runtime_tool in (
        hermes_mission_create,
        hermes_mission_get,
        hermes_mission_list,
        hermes_mission_update,
        hermes_mission_attach,
        hermes_mission_reconcile,
        hermes_mission_transition,
        hermes_mission_approve,
    ):
        server.add_tool(_mission_runtime_tool, meta=tool_meta())

    # MissionPlan (decomposition DAG, additive; read-only re missions).
    # Get/list/review/validate/decompose are read-only; create/node-transition/
    # set-status mutate only the plan store and never dispatch or change a
    # Mission's lifecycle (Phase-1 read-only slice).
    for _plan_tool in (
        hermes_plan_create,
        hermes_plan_get,
        hermes_plan_list,
        hermes_plan_validate,
        hermes_plan_decompose,
        hermes_plan_review,
        hermes_plan_node_transition,
        hermes_plan_set_status,
    ):
        server.add_tool(_plan_tool, meta=tool_meta())

    # Mission budget envelope (spend envelope + budget_check; dry-run).
    # Get/check are read-only; set/record mutate only the budget store and never
    # pause a Mission or change its lifecycle (Phase-2 dry-run slice). D3 hard-
    # block semantics are designed (flag default off) but wired in Phase 4/5.
    for _budget_tool in (
        hermes_budget_set,
        hermes_budget_get,
        hermes_budget_check,
        hermes_budget_record,
    ):
        server.add_tool(_budget_tool, meta=tool_meta())

    # Deterministic placement scoring (filter-and-score, D7): dry-run only.
    # Score/candidates/get/list never dispatch work; score records a placement
    # decision only under workspace+direct (dry-run-first) and always returns
    # would_assign=False. The controller proposes; dispatch goes through the
    # existing contract/fleet/delegation authority surfaces.
    for _placement_tool in (
        hermes_placement_score,
        hermes_placement_candidates,
        hermes_placement_get,
        hermes_placement_list,
    ):
        server.add_tool(_placement_tool, meta=tool_meta())

    # Semantic failure classification + deterministic recovery matrix (vNext
    # slice-1, phase 3, §17 item 7 / D8 / §11): decision output only. The
    # classifier proposes the smallest action and never executes anything;
    # taxonomy/matrix/plan-list are read-only.
    for _failure_tool in (
        hermes_failure_classify,
        hermes_failure_taxonomy,
        hermes_recovery_matrix,
        hermes_controller_plan_list,
    ):
        server.add_tool(_failure_tool, meta=tool_meta())

    # Supervised mission controller — shadow/observe reconciler loop (§17 item 6 /
    # §7, D2, D10). Read-only surfaces + a single-pass shadow reconcile. The
    # reconcile surface observes authoritative mission/plan/delegation/runner
    # state, classifies it, and emits the smallest recovery action as decision
    # output ONLY (would_execute always False); its only durable writes are the
    # controller's own controller_plan + controller_telemetry + pass lease. It
    # never dispatches work, never completes, never approves — the loop is not
    # started by this slice (no deploy / no process mutation).
    for _controller_tool in (
        hermes_controller_reconcile,
        hermes_controller_status,
        hermes_controller_lease_list,
        hermes_controller_trigger,
    ):
        server.add_tool(_controller_tool, meta=tool_meta())

    # Event history (v0.7 S4): read-only normalized timeline over durable
    # stores. Registered unconditionally; each tool enforces the per-client
    # allowlist (HERMES_GPT_EVENTS_ALLOWED_SOURCES) and audits every call.
    # readOnlyHint is advisory for client-side filtering (Cursor/Claude
    # Desktop); it is not authority and never gates a call.
    server.add_tool(
        hermes_events_query,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Query the normalized Hermes GPT event timeline",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_events_tail,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Tail recent Hermes GPT events across allowed sources",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_live_events_cursor,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Read the durable v0.9 live-event cursor",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_live_events_since,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Read or wait for durable v0.9 live events",
            readOnlyHint=True,
        ),
    )

    # Derived capability-manifest + per-mission ledger (vNext slice-1, phase 1):
    # read-only derived views over authoritative registries. Registered
    # unconditionally; each enforces a per-client allowlist env, audits every
    # call, and carries readOnlyHint (advisory, not authority). No mutation
    # path exists in either module.
    server.add_tool(
        hermes_capability_manifest,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Query the derived capability manifest (read-only)",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_mission_ledger,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Query the merged, replayable per-mission ledger (read-only)",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_mission_ledger_replay,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Replay a mission's full ledger event history (read-only)",
            readOnlyHint=True,
        ),
    )

    # Trusted-client OAuth (v0.7 S5): durable token store surfaces. Status is
    # read-only; revoke is owner-gated (pending legal scope decision).
    server.add_tool(
        hermes_oauth_status,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Durable OAuth token store status",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_oauth_revoke,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Revoke durable OAuth tokens",
            destructiveHint=True,
        ),
    )

    # Work Contracts (v0.6 M1): define/dispatch/validate/status. Registered
    # unconditionally; dispatch enforces workspace level + dry-run-first +
    # confirm gates; validate enforces D6 test gating internally.
    for _contract_tool in (
        hermes_contract_define,
        hermes_contract_dispatch,
        hermes_contract_validate,
        hermes_contract_status,
    ):
        server.add_tool(_contract_tool, meta=tool_meta())

    # Pluggable execution backends: list/status are read-only; cancellation is
    # workspace/direct gated internally and dry-run-first.
    for _runner_tool in (
        hermes_runner_list,
        hermes_runner_status,
        hermes_runner_cancel,
    ):
        server.add_tool(_runner_tool, meta=tool_meta())

    # Runner-neutral durable job status. The wait tool carries the polling
    # contract so chat clients can make one bounded decisive call per turn.
    server.add_tool(
        hermes_job_status,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Read durable background-job status and log cursor",
            readOnlyHint=True,
        ),
    )
    server.add_tool(
        hermes_job_wait,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Wait up to 120 seconds for a background job to finish",
            readOnlyHint=True,
        ),
    )

    # Unified delegation lifecycle (v0.9): normalized durable lineage over
    # runner/Fabric execution. Get/list are read-only; dispatch/cancel/reconcile
    # preserve the underlying authority and dry-run gates.
    for _delegation_tool in (
        hermes_delegation_dispatch,
        hermes_delegation_get,
        hermes_delegation_list,
        hermes_delegation_reconcile,
        hermes_delegation_cancel,
    ):
        server.add_tool(_delegation_tool, meta=tool_meta())

    # Review-evidence writer (v0.7 S3): owner-gated, distinct reviewer.
    server.add_tool(
        hermes_review_accept,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Accept a review verdict for a Work Contract"
        ),
    )

    # Swarm Orchestration (v0.6 M2): workflow engine on contracts. Registered
    # unconditionally; each tool enforces its own level/apply/dry-run gates
    # and audits every call (D-SW9/D-SW10).
    for _swarm_tool in (
        hermes_swarm_workflow_create,
        hermes_swarm_workflow_list,
        hermes_swarm_workflow_status,
        hermes_swarm_workflow_validate,
        hermes_swarm_stage_dispatch,
        hermes_swarm_stage_advance,
        hermes_swarm_approve,
    ):
        server.add_tool(_swarm_tool, meta=tool_meta())

    # Skills
    server.add_tool(hermes_skill_diff, meta=tool_meta())
    server.add_tool(hermes_skill_create, meta=tool_meta())
    server.add_tool(hermes_skill_edit, meta=tool_meta())
    server.add_tool(hermes_skill_patch, meta=tool_meta())
    server.add_tool(hermes_skill_write_file, meta=tool_meta())
    server.add_tool(hermes_skill_copy, meta=tool_meta())
    server.add_tool(hermes_skill_sync_to_default, meta=tool_meta())
    server.add_tool(hermes_skill_delete, meta=tool_meta())

    # Config / env
    server.add_tool(hermes_config_get, meta=tool_meta())
    server.add_tool(hermes_config_set, meta=tool_meta())
    server.add_tool(hermes_config_patch, meta=tool_meta())
    server.add_tool(hermes_env_status, meta=tool_meta())
    server.add_tool(hermes_env_set_nonsecret, meta=tool_meta())
    server.add_tool(hermes_env_copy_nonsecret, meta=tool_meta())

    # Gateway / workspace / git / owner
    server.add_tool(hermes_gateway_status, meta=tool_meta())
    server.add_tool(hermes_gateway_restart, meta=tool_meta())
    server.add_tool(hermes_workspace_read, meta=tool_meta())
    server.add_tool(
        hermes_export_file,
        meta=tool_meta(),
        annotations=ToolAnnotations(
            title="Export an authorized local file as an MCP embedded resource",
            readOnlyHint=True,
        ),
    )
    server.add_tool(hermes_workspace_patch, meta=tool_meta())
    server.add_tool(hermes_workspace_write_file, meta=tool_meta())
    server.add_tool(hermes_workspace_run_test, meta=tool_meta())
    server.add_tool(hermes_git_status, meta=tool_meta())
    server.add_tool(hermes_git_diff, meta=tool_meta())
    server.add_tool(hermes_owner_run_command, meta=tool_meta())
    server.add_tool(hermes_owner_patch, meta=tool_meta())
    server.add_tool(hermes_owner_write_file, meta=tool_meta())

    for tool in (
        hermes_codex_status, hermes_codex_plan, hermes_codex_start,
        hermes_codex_review_start, hermes_codex_jobs, hermes_codex_job_status,
        hermes_codex_job_result, hermes_codex_cancel,
    ):
        server.add_tool(tool, meta=tool_meta())


def _codex_gateway_diagnostics() -> dict[str, Any]:
    """Combine the general doctor with the state-file-aware gateway status."""
    def decoded(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    return {
        "operator_doctor": decoded(hermes_operator_doctor()),
        # operator_workspace has the gateway_state.json fallback required for
        # macOS installs when gateway.pid is stale or absent.
        "gateway_status": decoded(hermes_gateway_status()),
    }


def build_codex_mcp_server(
    *,
    host: str = "127.0.0.1",
    port: int = 7677,
    http: bool = False,
) -> FastMCP:
    """Build the deliberately compact Codex-facing MCP registry.

    The existing ``build_server`` remains the backwards-compatible legacy
    surface.  Codex gets only the high-leverage tools defined in codex_mcp.
    """
    from codex_core import CodexToolCore
    from codex_mcp import build_codex_server

    def imports_ready() -> bool:
        return IMPORT_ERROR is None and HERMES_ROOT is not None

    core = CodexToolCore(
        version=CODEX_BATCH_VERSION,
        imports_ready=imports_ready,
        gateway_snapshot=lambda: hermes_gateway_status(),
        gateway_diagnostics_callback=_codex_gateway_diagnostics,
        vision_analyze=lambda image_path, prompt: hermes_vision_analyze(image_url=image_path, question=prompt),
        web_search=lambda query, limit: hermes_web_search(query=query, limit=limit),
        web_extract=lambda urls, limit: hermes_web_extract(urls=urls, char_limit=limit),
        cron_create_callback=lambda schedule, prompt, dry_run: hermes_cron_create(schedule=schedule, prompt=prompt, dry_run=dry_run),
        skill_create_callback=lambda name, content, dry_run: hermes_skill_create(name=name, content=content, dry_run=dry_run),
    )
    operator_tools = {
        "hermes_operator_policy": hermes_operator_policy,
        "hermes_operator_status": hermes_operator_status,
        "hermes_operator_audit_tail": hermes_operator_audit_tail,
        "hermes_operator_doctor": hermes_operator_doctor,
        "hermes_operator_snapshot": hermes_operator_snapshot,
        "hermes_release_doctor": hermes_release_doctor,
        "hermes_operator_recover": hermes_operator_recover,
        "hermes_operator_cron_list": hermes_cron_list,
        "hermes_operator_cron_status": hermes_cron_status,
        "hermes_operator_cron_run": hermes_cron_run,
        "hermes_operator_cron_pause": hermes_cron_pause,
        "hermes_operator_cron_create": hermes_cron_create,
        "hermes_operator_cron_copy": hermes_cron_copy,
        "hermes_operator_cron_move": hermes_cron_move,
        "hermes_operator_skill_list": hermes_skill_list,
        "hermes_operator_skill_view": hermes_skill_view,
        "hermes_operator_skill_diff": hermes_skill_diff,
        "hermes_operator_skill_create": hermes_skill_create,
        "hermes_operator_skill_edit": hermes_skill_edit,
        "hermes_operator_skill_patch": hermes_skill_patch,
        "hermes_operator_skill_write_file": hermes_skill_write_file,
        "hermes_operator_skill_copy": hermes_skill_copy,
        "hermes_operator_skill_sync_to_default": hermes_skill_sync_to_default,
        "hermes_operator_skill_delete": hermes_skill_delete,
        "hermes_operator_config_get": hermes_config_get,
        "hermes_operator_config_set": hermes_config_set,
        "hermes_operator_config_patch": hermes_config_patch,
        "hermes_operator_env_status": hermes_env_status,
        "hermes_operator_env_set_nonsecret": hermes_env_set_nonsecret,
        "hermes_operator_env_copy_nonsecret": hermes_env_copy_nonsecret,
        "hermes_operator_gateway_status": hermes_gateway_status,
        "hermes_operator_gateway_restart": hermes_gateway_restart,
    }
    return build_codex_server(core, host=host, port=port, http=http, operator_tools=operator_tools)


mcp = build_server()


def _run_codex_mcp(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="hermes-gpt mcp", description="Run the Hermes GPT Codex MCP server.")
    parser.add_argument("--http", action="store_true", help="Run streamable HTTP instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7677)
    args = parser.parse_args(argv)
    server = build_codex_mcp_server(host=args.host, port=args.port, http=args.http)
    if not args.http:
        eprint("hermes-gpt Codex MCP server starting in stdio mode.")
        server.run(transport="stdio")
        return
    eprint(f"hermes-gpt Codex MCP server running at http://{args.host}:{args.port}/mcp")
    import uvicorn

    # No forwarded_allow_ips override: uvicorn defaults to loopback-only
    # proxy trust (or the operator-set FORWARDED_ALLOW_IPS env). A wildcard
    # here would trust client-supplied X-Forwarded-For from any peer
    # (security review t_f9925699 hardening note).
    uvicorn.run(server.streamable_http_app(), host=args.host, port=args.port, proxy_headers=True)


def _run_legacy_server(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent MCP sidecar.")
    parser.add_argument("--http", action="store_true", help="Run streamable HTTP transport instead of stdio.")
    parser.add_argument("--sse", action="store_true", help="Run legacy SSE transport instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7677)
    parser.add_argument("--cert", help="Path to SSL certificate file (enables HTTPS)")
    parser.add_argument("--key", help="Path to SSL key file (enables HTTPS)")
    parser.add_argument(
        "--profile",
        choices=[LOCAL_DEV_PROFILE, REMOTE_PROFILE],
        default=LOCAL_DEV_PROFILE,
        help="Release safety profile. Remote mode requires authentication unless unsafe no-auth is explicitly acknowledged.",
    )
    parser.add_argument(
        UNSAFE_REMOTE_ACK,
        action="store_true",
        dest="unsafe_remote_ack",
        help="Allow remote profile without auth. For experiments only; not release-safe.",
    )
    args = parser.parse_args(argv)

    if args.http and args.sse:
        raise SystemExit("Choose only one of --http or --sse.")
    configured_auth = auth_enabled()
    proxy_headers, forwarded_allow_ips = authenticated_http_security_options(
        profile=args.profile,
        host=args.host,
        cert=args.cert,
        key=args.key,
        configured_auth=configured_auth,
    )
    remote_unsafe_noauth = args.unsafe_remote_ack and env_enabled(UNSAFE_REMOTE_ENV)
    if args.profile == REMOTE_PROFILE and not (configured_auth or remote_unsafe_noauth):
        raise SystemExit(
            "Remote profile requires real authentication. Configure a static bearer token or confidential-client OAuth. "
            f"For temporary experiments only, pass {UNSAFE_REMOTE_ACK} and set {UNSAFE_REMOTE_ENV}=1."
        )
    if args.profile == LOCAL_DEV_PROFILE and not is_loopback_host(args.host) and not configured_auth:
        eprint(
            "WARNING: local-dev profile is bound to a non-loopback host. "
            "Do not expose hermes-gpt without real authentication."
        )
    if args.profile == REMOTE_PROFILE and remote_unsafe_noauth and not configured_auth:
        eprint("WARNING: remote no-auth mode is explicitly unsafe and intended only for temporary experiments.")

    transport = "streamable-http" if args.http else "sse" if args.sse else "stdio"
    server = build_server(host=args.host, port=args.port, http=args.http)
    if transport == "stdio":
        eprint("hermes-gpt MCP server starting in stdio mode.")
        server.run(transport="stdio")
    else:
        path = "/mcp" if args.http else "/sse"
        eprint(f"hermes-gpt MCP server running at http://{args.host}:{args.port}{path}")

        # Run with uvicorn instead of FastMCP.run() so TLS can be enabled for
        # local-only testing when cert/key are provided.
        import uvicorn
        app = build_asgi_app(server, http=args.http)
        _register_fleet_local_card()

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=args.cert if args.cert else None,
            ssl_keyfile=args.key if args.key else None,
            proxy_headers=proxy_headers,
            forwarded_allow_ips=forwarded_allow_ips,
        )


def main(argv: list[str] | None = None) -> None:
    """Run legacy MCP, the Codex MCP alias, or the Codex installer helpers."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "mcp":
        _run_codex_mcp(args[1:])
        return
    if args and args[0] == "update":
        import updater

        updater.main(args[1:])
        return
    if args and args[0] == "codex":
        if len(args) > 1 and args[1] == "mcp":
            _run_codex_mcp(args[2:])
            return
        import codex_config

        def list_tools() -> list[str]:
            return [tool.name for tool in asyncio.run(build_codex_mcp_server().list_tools())]

        def status() -> dict[str, Any]:
            try:
                data = json.loads(hermes_gateway_status())
                return {
                    "ok": bool(data.get("success")),
                    "gateway": "running" if data.get("gateway_running") else "not_running",
                    "gateway_pid_source": data.get("gateway_pid_source"),
                }
            except Exception:
                return {"ok": False, "gateway": "unknown"}

        codex_config.main(args[1:], list_tools=list_tools, status=status)
        return
    _run_legacy_server(args)


if __name__ == "__main__":
    main()
