"""Backend registry, plugin loading, routing, and fleet transport."""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.execution.runner_common import _BACKEND_RE, RUNNER_BACKEND_ALLOWLIST_ENV, RUNNER_PLUGIN_ALLOWLIST_ENV, _bounded_text, _cleanup_stale_request_envelopes, _runner_allowed, _split_allowlist

logger = logging.getLogger(__name__)


class RunnerBackend(Protocol):
    name: str

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]: ...

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def observed_runs(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> list[dict[str, Any]]: ...

    def cancel(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> dict[str, Any]: ...


_BACKENDS: dict[str, RunnerBackend] = {}


_REGISTRY_LOCK = threading.RLock()


def register_backend(backend: RunnerBackend, *, replace: bool = False) -> None:
    name = str(getattr(backend, "name", "") or "").strip().lower()
    if not _BACKEND_RE.fullmatch(name):
        raise ValueError("runner backend name is invalid")
    with _REGISTRY_LOCK:
        if name in _BACKENDS and not replace:
            raise ValueError(f"runner backend {name!r} is already registered")
        _BACKENDS[name] = backend


def get_backend(name: str) -> RunnerBackend:
    key = str(name or "").strip().lower()
    with _REGISTRY_LOCK:
        backend = _BACKENDS.get(key)
    if backend is None:
        raise LookupError(f"runner backend {key!r} is not registered")
    return backend


def load_entrypoint_backends() -> list[str]:
    """Load external runner plugins from the ``hermes_gpt.runners`` group.

    Each entry point may expose either a backend instance or a zero-argument
    factory/class returning one. Broken plugins are isolated and skipped.
    """
    loaded: list[str] = []
    try:
        eps = importlib.metadata.entry_points()
        selected = (
            eps.select(group="hermes_gpt.runners")
            if hasattr(eps, "select")
            else eps.get("hermes_gpt.runners", [])
        )
    except Exception as exc:
        logger.debug("runner entry-point discovery failed", exc_info=exc)
        return loaded
    builtin_names = {"fleet", "pi_rpc", "omx", "opencode", "codex"}
    for ep in selected:
        try:
            candidate = ep.load()
            if isinstance(candidate, type) or (
                callable(candidate) and not hasattr(candidate, "dispatch")
            ):
                backend = candidate()
            else:
                backend = candidate
            name = str(getattr(backend, "name", "") or "").strip().lower()
            if name in builtin_names:
                raise ValueError(
                    f"external runner may not shadow built-in backend {name!r}"
                )
            allowed_plugins = _split_allowlist(
                os.environ.get(RUNNER_PLUGIN_ALLOWLIST_ENV)
            )
            if not allowed_plugins or (
                name not in allowed_plugins
                and getattr(ep, "name", "") not in allowed_plugins
            ):
                raise PermissionError(
                    f"external runner {name!r} is not allowlisted by {RUNNER_PLUGIN_ALLOWLIST_ENV}"
                )
            register_backend(backend, replace=False)
            loaded.append(str(getattr(backend, "name", ep.name)))
        except Exception as exc:
            logger.debug(
                "runner entry point %s failed to load",
                getattr(ep, "name", "unknown"),
                exc_info=exc,
            )
            continue
    return loaded


def list_backends(*, hermes_root: Path | None = None) -> list[dict[str, Any]]:
    _cleanup_stale_request_envelopes(hermes_root=hermes_root)
    with _REGISTRY_LOCK:
        items = list(_BACKENDS.values())
    out: list[dict[str, Any]] = []
    for backend in items:
        try:
            info = backend.availability(hermes_root=hermes_root)
        except Exception as exc:  # noqa: BLE001
            info = {"available": False, "reason": exc.__class__.__name__}
        out.append({"name": backend.name, **info})
    return sorted(out, key=lambda item: item["name"])


def selected_backend(contract: dict[str, Any]) -> str:
    execution = contract.get("execution")
    if isinstance(execution, dict) and execution.get("backend"):
        backend = str(execution["backend"])
    else:
        backend = "fleet"
    if not _runner_allowed(backend):
        raise PermissionError(
            f"runner backend {backend!r} is not allowed by {RUNNER_BACKEND_ALLOWLIST_ENV}"
        )
    return backend


def dispatch_contract(
    contract: dict[str, Any],
    *,
    confirm: bool,
    dry_run: bool,
    timeout: int,
    hermes_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        backend_name = selected_backend(contract)
        backend = get_backend(backend_name)
    except LookupError as exc:
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_BACKEND_UNKNOWN",
            "safe_message": str(exc),
            "suggested_action": "Select a registered execution.backend.",
            "backend": str((contract.get("execution") or {}).get("backend") or "fleet"),
        }
    except PermissionError as exc:
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_BACKEND_NOT_ALLOWED",
            "safe_message": _bounded_text(exc, 300),
            "suggested_action": "Update runner backend allowlist or select an allowed backend.",
            "backend": str((contract.get("execution") or {}).get("backend") or "fleet"),
        }
    try:
        result = backend.dispatch(
            contract,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=hermes_root,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        if backend_name == "fleet" and not isinstance(contract.get("execution"), dict):
            # Legacy contract-dispatch compatibility applies only to contracts
            # that omit the execution selector and therefore use the historical
            # implicit fleet path. An explicit execution.backend="fleet" is a
            # runner selection and uses the runner error contract below.
            return {
                "success": False,
                "ok": False,
                "code": "CONTRACT_DISPATCH_ERROR",
                "safe_message": _bounded_text(exc, 300),
                "suggested_action": "Check fleet authority manifest, registry, and peer service.",
                "backend": backend_name,
            }
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_DISPATCH_ERROR",
            "safe_message": _bounded_text(exc, 300),
            "suggested_action": f"Check the {backend_name} runner backend and retry.",
            "backend": backend_name,
        }
    result.setdefault("backend", backend_name)
    return result


def observed_runs(
    task_id: str, *, hermes_root: Path | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _REGISTRY_LOCK:
        items = list(_BACKENDS.values())
    for backend in items:
        try:
            out.extend(backend.observed_runs(task_id, hermes_root=hermes_root))
        except Exception as exc:
            logger.debug(
                "runner backend %s observation failed",
                getattr(backend, "name", "unknown"),
                exc_info=exc,
            )
            continue
    return out


@dataclass
class FleetBackend:
    name: str = "fleet"

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            payload = json.loads(op_fleet.hermes_fleet_list())
            return {
                "available": bool(payload.get("success")),
                "reason": payload.get("safe_message")
                if not payload.get("success")
                else None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": _bounded_text(exc, 200)}

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspaces = contract["allowed_scope"]["workspaces"]
        acceptance_checks = [
            f"run_state outcome_ok={contract['completion_criteria']['run_state']['outcome_ok']}",
            f"artifacts_present={contract['completion_criteria']['artifacts_present']}",
            f"tests_pass={contract['completion_criteria']['tests_pass']}",
            f"review_satisfied={contract['completion_criteria']['review_satisfied']}",
            f"no_forbidden_actions={contract['completion_criteria']['no_forbidden_actions']}",
        ]
        deliverables = [a["path"] for a in contract["expected_artifacts"]]
        text = op_fleet.hermes_fleet_dispatch_work_order(
            agent=contract["assigned_agent"],
            task_id=contract["task_id"],
            target_profile=contract["assigned_profile"],
            objective=contract["objective"],
            workspace=workspaces[0] if workspaces else "",
            inputs=contract["inputs"],
            constraints=contract["constraints"],
            acceptance_checks=acceptance_checks,
            deliverables=deliverables,
            authorization=contract["authorization"],
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            runner=kwargs.get("runner"),
            hermes_bin=kwargs.get("hermes_bin"),
            authority_manifest=kwargs.get("authority_manifest"),
        )
        payload = json.loads(text)
        payload["backend"] = self.name
        return payload

    def observed_runs(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> list[dict[str, Any]]:
        return []  # Fleet observations remain sourced from Mission Control.

    def cancel(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> dict[str, Any]:
        return {
            "success": False,
            "code": "RUNNER_CANCEL_UNSUPPORTED",
            "backend": self.name,
        }
