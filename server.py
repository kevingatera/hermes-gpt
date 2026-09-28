from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import oauth_auth
import operator_codex as op_codex
import operator_finance as op_finance
import operator_job_supervisor as op_jobs
import operator_policy as op_policy
import operator_session as op_session
import operator_session_tasks as op_session_tasks
import operator_status as op_status
import operator_swarm as op_swarm
import server_hermes_runtime as hermes_runtime
import server_http as http_server
import server_hermes_tools as hermes_tools
import server_skill_tools as skill_tools
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
from server_fleet_tools import FleetTools
from server_hermes_profile_tools import HermesProfileTools
from server_mission_tools import MissionTools
from server_operator_tools import OperatorTools
from server_session_browser_tools import register_session_browser_tools
from server_session_control_tools import SessionControlTools
from server_session_task_tools import ManagedSessionTaskTools
from server_session_tools import SessionHistoryTools, SessionToolContext
from server_work_tools import WorkTools
from server_workspace_tools import WorkspaceTools
from versioning import VERSION

LOCAL_DEV_PROFILE = "local-dev"
REMOTE_PROFILE = http_server.REMOTE_PROFILE
UNSAFE_REMOTE_ACK = "--i-understand-this-is-unsafe"
UNSAFE_REMOTE_ENV = "HERMES_GPT_UNSAFE_REMOTE_NOAUTH"
TRUSTED_PROXY_IPS_ENV = http_server.TRUSTED_PROXY_IPS_ENV
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
    return http_server.is_loopback_host(host)


is_hermes_root = hermes_runtime.is_hermes_root
candidate_roots = hermes_runtime.candidate_roots
find_hermes_root = hermes_runtime.find_hermes_root
add_path_once = hermes_runtime.add_path_once
add_hermes_to_syspath = hermes_runtime.add_hermes_to_syspath


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


def clean_error(tool_name: str, exc: Exception) -> RuntimeError:
    eprint(f"hermes-gpt: {tool_name} failed: {exc}")
    return RuntimeError(f"{tool_name} failed: {exc}")


from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from mcp_compat import HermesMCP as FastMCP

import_hermes()

_skill_tools = skill_tools.HermesSkillTools(
    require_imports=require_imports,
    get_hermes_home=lambda: get_hermes_home() if callable(get_hermes_home) else None,
    get_source_root=lambda: HERMES_ROOT,
    clean_error=clean_error,
    eprint=eprint,
)
parse_skill_doc = skill_tools.parse_skill_doc
skill_roots = _skill_tools.skill_roots
discover_skills = _skill_tools.discover_skills
hermes_skill_list = _skill_tools.hermes_skill_list
hermes_skill_view = _skill_tools.hermes_skill_view


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


_hermes_tool_context = hermes_tools.HermesToolContext(
    require_imports=lambda: require_imports(),
    env_enabled=lambda name: env_enabled(name),
    call_with_supported_kwargs=lambda func, **kwargs: call_with_supported_kwargs(
        func, **kwargs
    ),
    expand_path=lambda value: expand_path(value),
    clean_error=lambda name, exc: clean_error(name, exc),
    get_file_tools=lambda: file_tools,
    get_memory_tool=lambda: memory_tool,
    get_terminal_tool=lambda: terminal_tool,
    get_vision_tool=lambda: vision_tool,
    get_web_tool=lambda: web_tool,
    terminal_enabled_env=ENABLE_TERMINAL_ENV,
    memory_write_enabled_env=ENABLE_MEMORY_WRITE_ENV,
    vision_enabled_env=ENABLE_VISION_ENV,
    web_enabled_env=ENABLE_WEB_ENV,
)
_hermes_tools = hermes_tools.HermesTools(_hermes_tool_context)
hermes_read_file = _hermes_tools.hermes_read_file
hermes_write_file = _hermes_tools.hermes_write_file
hermes_patch = _hermes_tools.hermes_patch
hermes_search_files = _hermes_tools.hermes_search_files
hermes_run_command = _hermes_tools.hermes_run_command
hermes_memory = _hermes_tools.hermes_memory
hermes_vision_analyze = _hermes_tools.hermes_vision_analyze
hermes_web_search = _hermes_tools.hermes_web_search
hermes_web_extract = _hermes_tools.hermes_web_extract


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
_managed_session_task_tools = ManagedSessionTaskTools(_session_tool_context)

_session_error = _session_history_tools._session_error
_session_page_response = _session_history_tools._session_page_response
_session_markdown_response = _session_history_tools._session_markdown_response
hermes_bot_chat_get = _session_history_tools.hermes_bot_chat_get
hermes_bot_chat_send = _session_history_tools.hermes_bot_chat_send
hermes_session_list = _session_history_tools.hermes_session_list
hermes_session_read = _session_history_tools.hermes_session_read
hermes_session_export = _session_history_tools.hermes_session_export
hermes_session_search = _session_history_tools.hermes_session_search
hermes_session_start = _session_control_tools.hermes_session_start
hermes_session_continue = _session_control_tools.hermes_session_continue
hermes_session_send = _session_control_tools.hermes_session_send
hermes_session_rename = _session_control_tools.hermes_session_rename
hermes_session_pin = _session_control_tools.hermes_session_pin
hermes_session_job_status = _session_control_tools.hermes_session_job_status
hermes_session_job_cancel = _session_control_tools.hermes_session_job_cancel
hermes_session_job_result = _session_control_tools.hermes_session_job_result
hermes_task_list = _managed_session_task_tools.hermes_task_list
hermes_task_workspaces = _managed_session_task_tools.hermes_task_workspaces
hermes_task_start = _managed_session_task_tools.hermes_task_start
hermes_task_continue = _managed_session_task_tools.hermes_task_continue
hermes_task_status = _managed_session_task_tools.hermes_task_status
hermes_task_result = _managed_session_task_tools.hermes_task_result
hermes_task_cancel = _managed_session_task_tools.hermes_task_cancel


def hermes_finance_analyze(evidence_json: str, timeout: int = 120) -> str:
    """Analyze a bounded finance.evidence/v1 packet with the local Finance profile."""
    return op_finance.hermes_finance_analyze(
        evidence_json=evidence_json,
        timeout=timeout,
        hermes_root=_default_hermes_root(),
        agent_root=HERMES_ROOT,
    )


# Shared root resolution for operator and profile tool adapters.


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


_operator_tools = OperatorTools(
    get_hermes_root=lambda: _default_hermes_root(),
    get_agent_root=lambda: HERMES_ROOT,
    get_project_root=lambda: Path(__file__).resolve().parent,
    get_active_profile=lambda: _active_profile_name(),
)
hermes_operator_policy = _operator_tools.hermes_operator_policy
hermes_operator_status = _operator_tools.hermes_operator_status
hermes_operator_audit_tail = _operator_tools.hermes_operator_audit_tail
hermes_operator_doctor = _operator_tools.hermes_operator_doctor
hermes_operator_snapshot = _operator_tools.hermes_operator_snapshot
hermes_release_doctor = _operator_tools.hermes_release_doctor
hermes_operator_recover = _operator_tools.hermes_operator_recover
hermes_swarm_reconcile = _operator_tools.hermes_swarm_reconcile
hermes_events_query = _operator_tools.hermes_events_query
hermes_events_tail = _operator_tools.hermes_events_tail
hermes_capability_manifest = _operator_tools.hermes_capability_manifest
hermes_mission_ledger = _operator_tools.hermes_mission_ledger
hermes_mission_ledger_replay = _operator_tools.hermes_mission_ledger_replay
hermes_live_events_cursor = _operator_tools.hermes_live_events_cursor
hermes_live_events_since = _operator_tools.hermes_live_events_since
hermes_oauth_status = _operator_tools.hermes_oauth_status
hermes_oauth_revoke = _operator_tools.hermes_oauth_revoke


# Fleet MCP handlers keep their A2A registration list with their adapters.
_fleet_tools = FleetTools()

hermes_fleet_list = _fleet_tools.hermes_fleet_list
hermes_fleet_status = _fleet_tools.hermes_fleet_status
hermes_fleet_dispatch = _fleet_tools.hermes_fleet_dispatch
hermes_fleet_task = _fleet_tools.hermes_fleet_task
hermes_fleet_dispatch_work_order = _fleet_tools.hermes_fleet_dispatch_work_order
hermes_fleet_result = _fleet_tools.hermes_fleet_result
hermes_fleet_authority_drift = _fleet_tools.hermes_fleet_authority_drift


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


# Workspace, export, gateway, and Owner handlers keep their MCP registration
# beside the adapters while preserving the public tool names.
_workspace_tools = WorkspaceTools(_default_hermes_root)

hermes_gateway_status = _workspace_tools.hermes_gateway_status
hermes_gateway_restart = _workspace_tools.hermes_gateway_restart
hermes_workspace_read = _workspace_tools.hermes_workspace_read
hermes_export_file = _workspace_tools.hermes_export_file
hermes_workspace_patch = _workspace_tools.hermes_workspace_patch
hermes_workspace_write_file = _workspace_tools.hermes_workspace_write_file
hermes_workspace_run_test = _workspace_tools.hermes_workspace_run_test
hermes_git_status = _workspace_tools.hermes_git_status
hermes_git_diff = _workspace_tools.hermes_git_diff
hermes_owner_run_command = _workspace_tools.hermes_owner_run_command
hermes_owner_patch = _workspace_tools.hermes_owner_patch
hermes_owner_write_file = _workspace_tools.hermes_owner_write_file


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


_work_tools = WorkTools(_hermes_root_for_operator)

hermes_contract_define = _work_tools.hermes_contract_define
hermes_contract_dispatch = _work_tools.hermes_contract_dispatch
hermes_contract_validate = _work_tools.hermes_contract_validate
hermes_contract_status = _work_tools.hermes_contract_status
hermes_runner_list = _work_tools.hermes_runner_list
hermes_runner_status = _work_tools.hermes_runner_status
hermes_runner_cancel = _work_tools.hermes_runner_cancel
hermes_delegation_dispatch = _work_tools.hermes_delegation_dispatch
hermes_delegation_get = _work_tools.hermes_delegation_get
hermes_delegation_list = _work_tools.hermes_delegation_list
hermes_delegation_reconcile = _work_tools.hermes_delegation_reconcile
hermes_delegation_cancel = _work_tools.hermes_delegation_cancel
hermes_review_accept = _work_tools.hermes_review_accept
hermes_swarm_workflow_create = _work_tools.hermes_swarm_workflow_create
hermes_swarm_workflow_list = _work_tools.hermes_swarm_workflow_list
hermes_swarm_workflow_status = _work_tools.hermes_swarm_workflow_status
hermes_swarm_workflow_validate = _work_tools.hermes_swarm_workflow_validate
hermes_swarm_stage_dispatch = _work_tools.hermes_swarm_stage_dispatch
hermes_swarm_stage_advance = _work_tools.hermes_swarm_stage_advance
hermes_swarm_approve = _work_tools.hermes_swarm_approve


def oauth_state_from_env() -> oauth_auth.OAuthState | None:
    return http_server.oauth_state_from_env(_default_hermes_root)


auth_enabled = http_server.auth_enabled
trusted_proxy_ips_from_env = http_server.trusted_proxy_ips_from_env
authenticated_http_security_options = http_server.authenticated_http_security_options
health_root = http_server.health_root
_fleet_peer_name = http_server._fleet_peer_name
_fleet_peer_url = http_server._fleet_peer_url
_fleet_agent_card = http_server._fleet_agent_card


def _register_fleet_local_card() -> None:
    http_server.register_fleet_local_card()


def build_asgi_app(server: FastMCP, *, http: bool) -> Any:
    return http_server.build_asgi_app(
        server,
        http=http,
        get_hermes_root=_default_hermes_root,
        eprint=eprint,
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
    server._hermes_oauth_state = oauth_state
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
    if env_enabled(ENABLE_SESSION_CONTROL_ENV) and env_enabled(ENABLE_SESSION_SEARCH_ENV):
        server.add_tool(hermes_bot_chat_send, meta=tool_meta())
    _session_control_tools.register_mcp_tools(
        server,
        tool_meta=tool_meta,
        session_control_enabled=env_enabled(ENABLE_SESSION_CONTROL_ENV),
    )
    scoped_tasks_enabled = env_enabled(ENABLE_SCOPED_TASKS_ENV)
    _managed_session_task_tools.register_mcp_tools(
        server,
        tool_meta=tool_meta,
        enabled=scoped_tasks_enabled,
    )
    if scoped_tasks_enabled:
        register_session_browser_tools(server, tool_meta=tool_meta)
    if env_enabled(ENABLE_VISION_ENV):
        server.add_tool(hermes_vision_analyze, meta=tool_meta())
    if env_enabled(ENABLE_WEB_ENV):
        server.add_tool(hermes_web_search, meta=tool_meta())
        server.add_tool(hermes_web_extract, meta=tool_meta())
    if op_finance.finance_enabled(_default_hermes_root()):
        server.add_tool(hermes_finance_analyze, meta=tool_meta())

    _operator_tools.register_mcp_tools(server, tool_meta=tool_meta)

    _fleet_tools.register_mcp_tools(server, tool_meta=tool_meta)

    _profile_tools.register_cron_tools(server, tool_meta=tool_meta)

    # The mission adapter owns its registration list; each handler still applies
    # its own read-only, dry-run, workspace, or Owner policy gates.
    _mission_tools.register_mcp_tools(server, tool_meta=tool_meta)

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

    _profile_tools.register_admin_tools(server, tool_meta=tool_meta)

    _workspace_tools.register_mcp_tools(server, tool_meta=tool_meta)

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
    from codex_core import CodexToolCore, codex_toolset
    from codex_mcp import NOAUTH_META, build_codex_server

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
    codex_server = build_codex_server(
        core, host=host, port=port, http=http, operator_tools=operator_tools
    )
    if codex_toolset() == "sessions":
        from codex_session_tools import register_codex_session_tools

        register_codex_session_tools(
            codex_server,
            history_tools=_session_history_tools,
            session_control_tools=_session_control_tools,
            managed_task_tools=_managed_session_task_tools,
            tool_meta=lambda: dict(NOAUTH_META),
            session_history_enabled=env_enabled(ENABLE_SESSION_SEARCH_ENV),
            session_control_enabled=env_enabled(ENABLE_SESSION_CONTROL_ENV),
            scoped_tasks_enabled=env_enabled(ENABLE_SCOPED_TASKS_ENV),
        )
    return codex_server


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
