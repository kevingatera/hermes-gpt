"""Runner backend facade and detached worker entrypoint.

Backend behavior is split by ownership. This module keeps the existing
``operator_runners`` import surface and the single executable worker entrypoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import operator_policy as op
import operator_runner_common as runner_common
import operator_runner_local as runner_local
import operator_runner_opencode as runner_opencode
import operator_runner_registry as runner_registry
import operator_runner_workers as runner_workers
from operator_runner_local import (
    CodexBackend,
    OmxBackend,
    PiRpcBackend,
    _LocalProcessBackend,
)
from operator_runner_opencode import OpenCodeBackend
from operator_runner_registry import (
    FleetBackend,
    RunnerBackend,
    dispatch_contract,
    get_backend,
    list_backends,
    load_entrypoint_backends,
    observed_runs,
    register_backend,
    selected_backend,
)
from operator_runner_workers import _worker

# Keep the previous module attributes available to established callers.
SCHEMA_VERSION = runner_common.SCHEMA_VERSION
RUNNER_PLUGINS_ENV = runner_common.RUNNER_PLUGINS_ENV
RUNNER_PLUGIN_ALLOWLIST_ENV = runner_common.RUNNER_PLUGIN_ALLOWLIST_ENV
RUNNER_BACKEND_ALLOWLIST_ENV = runner_common.RUNNER_BACKEND_ALLOWLIST_ENV
RUNNER_PROVIDER_ALLOWLIST_ENV = runner_common.RUNNER_PROVIDER_ALLOWLIST_ENV
RUNNER_MODEL_ALLOWLIST_ENV = runner_common.RUNNER_MODEL_ALLOWLIST_ENV
_BACKENDS = runner_registry._BACKENDS
_REGISTRY_LOCK = runner_registry._REGISTRY_LOCK
_BACKEND_RE = runner_common._BACKEND_RE
_TASK_ID_RE = runner_common._TASK_ID_RE
_MAX_OPTIONS_BYTES = runner_common._MAX_OPTIONS_BYTES
_MAX_RESULT_CHARS = runner_common._MAX_RESULT_CHARS
_TERMINAL_STATES = runner_common._TERMINAL_STATES
_PI_WRITE_AUTH_CLASSES = runner_common._PI_WRITE_AUTH_CLASSES
logger = runner_common.logger
_now = runner_common._now
_root = runner_common._root
_job_paths = runner_common._job_paths
_cancel_path = runner_common._cancel_path
_atomic_json = runner_common._atomic_json
_load_json = runner_common._load_json
_append_event = runner_common._append_event
_bounded_text = runner_common._bounded_text
_popen_process_group = runner_common._popen_process_group
_windows_taskkill = runner_common._windows_taskkill
_terminate_process_tree = runner_common._terminate_process_tree
_terminate_process_group = runner_common._terminate_process_group
_audit_runner = runner_common._audit_runner
_split_allowlist = runner_common._split_allowlist
_allowed_by_env = runner_common._allowed_by_env
_runner_allowed = runner_common._runner_allowed
_minimal_child_env = runner_common._minimal_child_env
_cleanup_stale_request_envelopes = runner_common._cleanup_stale_request_envelopes
_authorization_class = runner_common._authorization_class
_pi_tools = runner_common._pi_tools
_pi_agent_dir = runner_common._pi_agent_dir
_pi_selection = runner_common._pi_selection
_unquote_env_value = runner_common._unquote_env_value
_pi_child_env = runner_common._pi_child_env
_sandbox_for = runner_common._sandbox_for
normalize_execution = runner_common.normalize_execution
_OPENCODE_ENV_REF_RE = runner_opencode._OPENCODE_ENV_REF_RE
_OPENCODE_PROXY_MAX_BODY = runner_opencode._OPENCODE_PROXY_MAX_BODY
_OPENCODE_HOP_HEADERS = runner_opencode._OPENCODE_HOP_HEADERS
_opencode_profile_env_value = runner_opencode._opencode_profile_env_value
_opencode_runtime_material = runner_opencode._opencode_runtime_material
_OpenCodeCredentialProxy = runner_opencode._OpenCodeCredentialProxy
_OpenCodeCredentialProxyHandler = runner_opencode._OpenCodeCredentialProxyHandler
_opencode_child_config = runner_opencode._opencode_child_config
_extract_pi_text = runner_workers._extract_pi_text
_worker_pi = runner_workers._worker_pi
_worker_opencode = runner_workers._worker_opencode
_worker_omx = runner_workers._worker_omx

# Preserve useful module-level patch points for integrations and older tests.
confinement = runner_common.confinement
job_supervisor = runner_local.job_supervisor
op_fleet = runner_registry.op_fleet
os = runner_common.os
signal = runner_common.signal
subprocess = runner_common.subprocess
threading = runner_workers.threading
selectors = runner_workers.selectors
http = runner_opencode.http
urllib = runner_opencode.urllib
hmac = runner_opencode.hmac
secrets = runner_opencode.secrets
importlib = runner_registry.importlib
BaseHTTPRequestHandler = runner_opencode.BaseHTTPRequestHandler
ThreadingHTTPServer = runner_opencode.ThreadingHTTPServer


def hermes_runner_list(hermes_root: Path | None = None) -> str:
    """List registered runner backends and bounded availability metadata."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "backends": list_backends(hermes_root=hermes_root),
        }
        _audit_runner(
            tool="hermes_runner_list",
            policy=policy,
            dry_run=True,
            success=True,
            summary="listed runner backends",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except PermissionError as exc:
        return json.dumps(
            {
                "success": False,
                "code": "RUNNER_POLICY_DENIED",
                "safe_message": _bounded_text(exc, 300),
            },
            indent=2,
        )


def hermes_runner_status(task_id: str, hermes_root: Path | None = None) -> str:
    """Return bounded observed state for a contract task across runner backends."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        _cleanup_stale_request_envelopes(hermes_root=hermes_root)
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            raise ValueError("task_id has an invalid format")
        runs = observed_runs(task_id, hermes_root=hermes_root)
        _audit_runner(
            tool="hermes_runner_status",
            policy=policy,
            dry_run=True,
            success=True,
            summary=f"observed {len(runs)} runner record(s)",
            task_id=task_id,
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "runs": runs,
                "count": len(runs),
            },
            ensure_ascii=False,
            indent=2,
        )
    except (PermissionError, ValueError) as exc:
        return json.dumps(
            {
                "success": False,
                "code": "RUNNER_STATUS_ERROR",
                "safe_message": _bounded_text(exc, 300),
            },
            indent=2,
        )


def hermes_runner_cancel(
    task_id: str,
    backend: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Cancel a runner job when the selected backend supports cancellation."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            raise ValueError("task_id has an invalid format")
        selected = str(backend or "").strip().lower()
        if not selected:
            meta_path, _, _ = _job_paths(task_id, hermes_root)
            meta = _load_json(meta_path) or {}
            selected = str(meta.get("backend") or "")
        if not selected:
            return json.dumps(
                {
                    "success": False,
                    "code": "RUNNER_BACKEND_REQUIRED",
                    "safe_message": "backend could not be inferred for task",
                },
                indent=2,
            )
        target = get_backend(selected)
        if isinstance(target, _LocalProcessBackend):
            meta_path, _, _ = _job_paths(task_id, hermes_root)
            meta = _load_json(meta_path)
            if not meta or meta.get("backend") != selected:
                return json.dumps(
                    {
                        "success": False,
                        "code": "RUNNER_JOB_NOT_FOUND",
                        "backend": selected,
                        "task_id": task_id,
                    },
                    indent=2,
                )
            workspace = meta.get("workspace")
            if not isinstance(workspace, str) or not workspace:
                raise PermissionError("runner job has no valid workspace scope")
            policy.require_workspace_path(workspace)
        if effective:
            _audit_runner(
                tool="hermes_runner_cancel",
                policy=policy,
                dry_run=True,
                success=True,
                changed=False,
                summary="cancel plan",
                task_id=task_id,
                backend=selected,
            )
            return json.dumps(
                {
                    "success": True,
                    "dry_run": True,
                    "changed": False,
                    "backend": selected,
                    "task_id": task_id,
                    "plan": "cancel",
                },
                indent=2,
            )
        if not confirm:
            _audit_runner(
                tool="hermes_runner_cancel",
                policy=policy,
                dry_run=True,
                success=False,
                changed=False,
                summary="confirmation required",
                task_id=task_id,
                backend=selected,
            )
            return json.dumps(
                {
                    "success": False,
                    "code": "CONFIRMATION_REQUIRED",
                    "backend": selected,
                    "safe_message": "runner cancellation requires confirm=true",
                },
                indent=2,
            )
        result = target.cancel(task_id, hermes_root=hermes_root)
        result.setdefault("backend", selected)
        result.setdefault("task_id", task_id)
        _audit_runner(
            tool="hermes_runner_cancel",
            policy=policy,
            dry_run=False,
            success=bool(result.get("success")),
            changed=bool(result.get("changed")),
            summary="cancelled runner job"
            if result.get("success")
            else "runner cancel failed",
            task_id=task_id,
            backend=selected,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, LookupError) as exc:
        return json.dumps(
            {
                "success": False,
                "code": "RUNNER_CANCEL_ERROR",
                "safe_message": _bounded_text(exc, 300),
            },
            indent=2,
        )


def _register_builtins() -> None:
    for backend in (
        FleetBackend(),
        PiRpcBackend(),
        OpenCodeBackend(),
        OmxBackend(),
        CodexBackend(),
    ):
        register_backend(backend, replace=True)


def _main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--worker":
        task_id = argv[2]
        if not _TASK_ID_RE.fullmatch(task_id):
            return 2
        jobs_root = None
        if len(argv) >= 5 and argv[3] == "--root":
            jobs_root = Path(argv[4]).expanduser().resolve()
        if jobs_root is None:
            jobs_root = _root()
        return _worker(task_id, jobs_root)
    return 2


__all__ = [
    "CodexBackend",
    "FleetBackend",
    "OmxBackend",
    "OpenCodeBackend",
    "PiRpcBackend",
    "RunnerBackend",
    "dispatch_contract",
    "get_backend",
    "hermes_runner_cancel",
    "hermes_runner_list",
    "hermes_runner_status",
    "list_backends",
    "load_entrypoint_backends",
    "normalize_execution",
    "observed_runs",
    "register_backend",
    "selected_backend",
]

_register_builtins()
if op.env_truthy(RUNNER_PLUGINS_ENV):
    load_entrypoint_backends()

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
