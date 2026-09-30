from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from hermes_gpt import paths
from hermes_gpt.auth import oauth as oauth_auth
from hermes_gpt.workspace import finance as op_finance
from hermes_gpt.policy import authorization as op_policy
from hermes_gpt.sessions import session as op_session
from hermes_gpt.sessions import tasks as op_session_tasks
from hermes_gpt.server import cli as server_cli
from hermes_gpt.server.tools import codex as codex_tools
from hermes_gpt.server import hermes_runtime
from hermes_gpt.server.tools import hermes as hermes_tools
from hermes_gpt.server import http as http_server
from hermes_gpt.server.tools import skills as skill_tools
from hermes_gpt.sessions.history import ReadOnlySessionStore
from hermes_gpt.sessions.history import validate_profile as _validate_profile
from hermes_gpt.server.tools.fleet import FleetTools
from hermes_gpt.server.tools.hermes_profile import HermesProfileTools
from hermes_gpt.server.tools.jobs import DurableJobTools
from hermes_gpt.server.tools.mission_decisions import MissionDecisionTools
from hermes_gpt.server.tools.mission_plans import MissionPlanTools
from hermes_gpt.server.tools.missions import MissionTools
from hermes_gpt.server.tools.operator import OperatorTools
from hermes_gpt.server.tools.session_browser import register_session_browser_tools
from hermes_gpt.server.tools.session_control import SessionControlTools
from hermes_gpt.server.tools.session_tasks import ManagedSessionTaskTools
from hermes_gpt.server.tools.sessions import SessionHistoryTools, SessionToolContext
from hermes_gpt.server.tools.work import WorkTools
from hermes_gpt.server.tools.workspace import WorkspaceTools
from hermes_gpt.versioning import VERSION

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
        except Exception as exc:  # noqa: BLE001 - Optional integration failures must not disable core tools.
            eprint(f"hermes-gpt: vision tool unavailable: {exc}")

        try:
            from tools import web_tools as wt

            web_tool = wt
        except Exception as exc:  # noqa: BLE001 - Optional integration failures must not disable core tools.
            eprint(f"hermes-gpt: web tool unavailable: {exc}")

        try:
            from tools import skill_manager_tool as smt

            skill_manager_tool = smt
        except Exception as exc:  # noqa: BLE001 - Optional integration failures must not disable core tools.
            eprint(f"hermes-gpt: skill manager unavailable: {exc}")

        try:
            from hermes_gpt.ui.state import SessionDB as SDB
            from hermes_gpt.ui.state import get_hermes_home as ghh

            SessionDB = SDB
            get_hermes_home = ghh
        except Exception as exc:  # noqa: BLE001 - Session history is optional at startup.
            eprint(f"hermes-gpt: session search unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 - Keep the server importable when Hermes is unavailable.
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

from hermes_gpt.mcp_compat import HermesMCP as FastMCP

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
    except (OSError, RuntimeError, ValueError):
        return "default"


_operator_tools = OperatorTools(
    get_hermes_root=lambda: _default_hermes_root(),
    get_agent_root=lambda: HERMES_ROOT,
    get_project_root=paths.project_root,
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


_codex_tools = codex_tools.CodexTools(
    get_hermes_root=lambda: _default_hermes_root()
)
hermes_codex_status = _codex_tools.hermes_codex_status
hermes_codex_plan = _codex_tools.hermes_codex_plan
hermes_codex_start = _codex_tools.hermes_codex_start
hermes_codex_review_start = _codex_tools.hermes_codex_review_start
hermes_codex_jobs = _codex_tools.hermes_codex_jobs
hermes_codex_job_status = _codex_tools.hermes_codex_job_status
hermes_codex_job_result = _codex_tools.hermes_codex_job_result
hermes_codex_cancel = _codex_tools.hermes_codex_cancel


# --- Durable background-job lifecycle ------------------------------------


_job_tools = DurableJobTools(_default_hermes_root)
hermes_job_status = _job_tools.hermes_job_status
hermes_job_wait = _job_tools.hermes_job_wait


_mission_tools = MissionTools(_hermes_root_for_operator)
_mission_plan_tools = MissionPlanTools(_hermes_root_for_operator)
_mission_decision_tools = MissionDecisionTools(_hermes_root_for_operator)

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
hermes_plan_create = _mission_plan_tools.hermes_plan_create
hermes_plan_get = _mission_plan_tools.hermes_plan_get
hermes_plan_list = _mission_plan_tools.hermes_plan_list
hermes_plan_validate = _mission_plan_tools.hermes_plan_validate
hermes_plan_decompose = _mission_plan_tools.hermes_plan_decompose
hermes_plan_review = _mission_plan_tools.hermes_plan_review
hermes_plan_node_transition = _mission_plan_tools.hermes_plan_node_transition
hermes_plan_set_status = _mission_plan_tools.hermes_plan_set_status
hermes_budget_set = _mission_plan_tools.hermes_budget_set
hermes_budget_get = _mission_plan_tools.hermes_budget_get
hermes_budget_check = _mission_plan_tools.hermes_budget_check
hermes_budget_record = _mission_plan_tools.hermes_budget_record
hermes_placement_score = _mission_decision_tools.hermes_placement_score
hermes_placement_candidates = _mission_decision_tools.hermes_placement_candidates
hermes_placement_get = _mission_decision_tools.hermes_placement_get
hermes_placement_list = _mission_decision_tools.hermes_placement_list
hermes_failure_classify = _mission_decision_tools.hermes_failure_classify
hermes_failure_taxonomy = _mission_decision_tools.hermes_failure_taxonomy
hermes_recovery_matrix = _mission_decision_tools.hermes_recovery_matrix
hermes_controller_plan_list = _mission_decision_tools.hermes_controller_plan_list
hermes_controller_reconcile = _mission_decision_tools.hermes_controller_reconcile
hermes_controller_status = _mission_decision_tools.hermes_controller_status
hermes_controller_lease_list = _mission_decision_tools.hermes_controller_lease_list
hermes_controller_trigger = _mission_decision_tools.hermes_controller_trigger


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
    _hermes_tools.register_read_tools(server, tool_meta=tool_meta)
    _skill_tools.register_mcp_tools(server, tool_meta=tool_meta)
    _hermes_tools.register_local_tools(
        server,
        tool_meta=tool_meta,
        write_enabled=env_enabled(ENABLE_WRITE_ENV),
        terminal_enabled=env_enabled(ENABLE_TERMINAL_ENV),
    )
    _session_history_tools.register_mcp_tools(
        server,
        tool_meta=tool_meta,
        history_enabled=env_enabled(ENABLE_SESSION_SEARCH_ENV),
        send_enabled=env_enabled(ENABLE_SESSION_CONTROL_ENV)
        and env_enabled(ENABLE_SESSION_SEARCH_ENV),
    )
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
    _hermes_tools.register_online_tools(
        server,
        tool_meta=tool_meta,
        vision_enabled=env_enabled(ENABLE_VISION_ENV),
        web_enabled=env_enabled(ENABLE_WEB_ENV),
    )
    if op_finance.finance_enabled(_default_hermes_root()):
        server.add_tool(hermes_finance_analyze, meta=tool_meta())

    _operator_tools.register_mcp_tools(server, tool_meta=tool_meta)

    _fleet_tools.register_mcp_tools(server, tool_meta=tool_meta)

    _profile_tools.register_cron_tools(server, tool_meta=tool_meta)

    # Each mission adapter registers one tool group; handlers keep their own
    # read-only, dry-run, workspace, and Owner policy gates.
    _mission_tools.register_mcp_tools(server, tool_meta=tool_meta)
    _mission_plan_tools.register_mcp_tools(server, tool_meta=tool_meta)
    _mission_decision_tools.register_mcp_tools(server, tool_meta=tool_meta)

    _work_tools.register_contract_and_runner_tools(server, tool_meta=tool_meta)
    _job_tools.register_mcp_tools(server, tool_meta=tool_meta)
    _work_tools.register_delegation_review_and_swarm_tools(
        server, tool_meta=tool_meta
    )

    _profile_tools.register_admin_tools(server, tool_meta=tool_meta)

    _workspace_tools.register_mcp_tools(server, tool_meta=tool_meta)
    _codex_tools.register_mcp_tools(server, tool_meta=tool_meta)


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
    from hermes_gpt.clients.codex.core import CodexToolCore, codex_toolset
    from hermes_gpt.clients.codex.mcp import NOAUTH_META, build_codex_server

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
        from hermes_gpt.clients.codex.session_tools import register_codex_session_tools

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


def _cli_context() -> server_cli.ServerCliContext:
    """Snapshot the CLI collaborators from this module's current state.

    Read per call rather than captured at import time: monkeypatching
    ``server.build_server`` (or any other passed callable) still affects the CLI
    path, exactly as it did when these functions lived here.
    """
    return server_cli.ServerCliContext(
        build_server=build_server,
        build_codex_mcp_server=build_codex_mcp_server,
        build_asgi_app=build_asgi_app,
        run_codex_mcp=_run_codex_mcp,
        run_legacy_server=_run_legacy_server,
        register_fleet_local_card=_register_fleet_local_card,
        gateway_status=hermes_gateway_status,
        auth_enabled=auth_enabled,
        authenticated_http_security_options=authenticated_http_security_options,
        is_loopback_host=is_loopback_host,
        eprint=eprint,
        env_enabled=env_enabled,
        local_dev_profile=LOCAL_DEV_PROFILE,
        remote_profile=REMOTE_PROFILE,
        unsafe_remote_ack=UNSAFE_REMOTE_ACK,
        unsafe_remote_env=UNSAFE_REMOTE_ENV,
    )


def _run_codex_mcp(argv: list[str]) -> None:
    """Run the Codex MCP surface; see server_cli.run_codex_mcp."""
    server_cli.run_codex_mcp(argv, _cli_context())


def _run_legacy_server(argv: list[str]) -> None:
    """Run the legacy MCP surface; see server_cli.run_legacy_server."""
    server_cli.run_legacy_server(argv, _cli_context())


def main(argv: list[str] | None = None) -> None:
    """Run legacy MCP, the Codex MCP alias, or the Codex installer helpers."""
    server_cli.main(argv, _cli_context())


if __name__ == "__main__":
    main()
