"""Local Pi, OMX, and Codex runner backends."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_job_supervisor as job_supervisor
import operator_policy as op
from operator_runner_common import (
    _RUNNER_ENTRYPOINT,
    _TASK_ID_RE,
    _TERMINAL_STATES,
    RUNNER_MODEL_ALLOWLIST_ENV,
    RUNNER_PROVIDER_ALLOWLIST_ENV,
    SCHEMA_VERSION,
    _allowed_by_env,
    _atomic_json,
    _bounded_text,
    _cancel_path,
    _job_paths,
    _load_json,
    _now,
    _pi_selection,
    _pi_tools,
    _popen_process_group,
    _root,
    _sandbox_for,
)

logger = logging.getLogger(__name__)


class _LocalProcessBackend:
    name = "local"

    def executable(self) -> str | None:
        raise NotImplementedError

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        exe = self.executable()
        return {"available": bool(exe), "executable": exe}

    def _policy_workspace(self, contract: dict[str, Any]) -> Path:
        workspaces = contract.get("allowed_scope", {}).get("workspaces") or []
        if not workspaces:
            raise ValueError("local runner requires at least one allowed workspace")
        workspace = Path(workspaces[0]).expanduser().resolve()
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(str(workspace))
        return workspace

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
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
        workspace = self._policy_workspace(contract)
        exe = self.executable()
        if not exe:
            return {
                "success": False,
                "code": "RUNNER_UNAVAILABLE",
                "backend": self.name,
                "safe_message": f"{self.name} executable not found",
            }
        plan = self.build_plan(contract)
        plan.update(
            {
                "backend": self.name,
                "workspace": str(workspace),
                "task_id": contract["task_id"],
            }
        )
        if effective:
            return {
                "success": True,
                "dry_run": True,
                "changed": False,
                "backend": self.name,
                "plan": plan,
            }
        if not confirm:
            return {
                "success": False,
                "code": "CONFIRMATION_REQUIRED",
                "backend": self.name,
                "safe_message": "local runner dispatch requires confirm=true",
            }

        task_id = contract["task_id"]
        meta_path, request_path, log_path = _job_paths(task_id, hermes_root)
        if meta_path.exists():
            return {
                "success": False,
                "code": "RUNNER_JOB_EXISTS",
                "backend": self.name,
                "safe_message": f"runner job {task_id!r} already exists",
            }
        request = {
            "backend": self.name,
            "contract": contract,
            "timeout": max(10, min(int(timeout), 3600)),
            "hermes_root": str((hermes_root or Path.home() / ".hermes").expanduser()),
        }
        _atomic_json(request_path, request)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "backend": self.name,
            "state": "queued",
            "outcome": "",
            "workspace": str(workspace),
            "created_at": _now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "returncode": None,
            "error": "",
        }
        _atomic_json(meta_path, meta)
        job_supervisor.register_job(
            task_id,
            backend=self.name,
            workspace=workspace,
            log_path=log_path,
            source_record=meta_path,
            cancel_path=_cancel_path(task_id, hermes_root),
            hermes_root=hermes_root,
        )
        try:
            proc = _popen_process_group(
                [
                    sys.executable,
                    str(_RUNNER_ENTRYPOINT),
                    "--worker",
                    task_id,
                    "--root",
                    str(_root(hermes_root)),
                ],
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            # Spawn failed after the request/meta envelopes were written.
            # Delete the request envelope so the raw objective/prompt cannot
            # remain on disk, and leave only bounded failed metadata. Catch
            # broadly because wrappers/test doubles can fail before a child
            # process exists with exceptions other than OSError.
            for path in (
                request_path,
                request_path.with_suffix(request_path.suffix + ".tmp"),
            ):
                try:
                    path.unlink()
                except OSError:
                    pass
            meta.update(
                {
                    "state": "failed",
                    "outcome": "failed",
                    "ended_at": _now(),
                    "error": _bounded_text(f"runner spawn failed: {exc}", 300),
                }
            )
            _atomic_json(meta_path, meta)
            job_supervisor.terminalize(
                task_id,
                "failed",
                summary=meta["error"],
                hermes_root=hermes_root,
            )
            return {
                "success": False,
                "code": "RUNNER_SPAWN_FAILED",
                "backend": self.name,
                "task_id": task_id,
                "safe_message": _bounded_text(exc, 300),
            }
        # The worker owns durable state transitions. Avoid a parent-side write
        # after spawn: a fast worker could otherwise complete and then be
        # overwritten back to "running" by the parent. The shared supervisor
        # records the detached worker identity so a later server can verify it.
        job_supervisor.mark_running(task_id, proc.pid, hermes_root=hermes_root)
        return {
            "success": True,
            "changed": True,
            "dry_run": False,
            "backend": self.name,
            "task_id": task_id,
            "state": "queued",
            "pid": proc.pid,
        }

    def observed_runs(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> list[dict[str, Any]]:
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            return []
        meta_path, _, _ = _job_paths(task_id, hermes_root)
        meta = _load_json(meta_path)
        if not meta or meta.get("backend") != self.name:
            return []
        return [
            {
                "task_id": task_id,
                "status": meta.get("state"),
                "outcome": meta.get("outcome") or meta.get("state"),
                "error": meta.get("error") or None,
                "started_at": meta.get("started_at") or meta.get("created_at"),
                "ended_at": meta.get("ended_at"),
                "scope": f"runner:{self.name}",
            }
        ]

    def cancel(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> dict[str, Any]:
        meta_path, _, _ = _job_paths(task_id, hermes_root)
        meta = _load_json(meta_path)
        if not meta or meta.get("backend") != self.name:
            return {
                "success": False,
                "code": "RUNNER_JOB_NOT_FOUND",
                "backend": self.name,
            }
        if meta.get("state") in _TERMINAL_STATES:
            return {
                "success": True,
                "changed": False,
                "backend": self.name,
                "state": meta.get("state"),
            }
        cancelled = job_supervisor.request_cancel(task_id, hermes_root=hermes_root)
        if not cancelled.get("success"):
            return {
                "success": False,
                "changed": bool(cancelled.get("changed")),
                "code": cancelled.get("code") or "RUNNER_CANCEL_FAILED",
                "backend": self.name,
                "safe_message": cancelled.get("safe_message")
                or "runner process could not be safely cancelled",
            }
        state = str(cancelled.get("status") or meta.get("state") or "unknown")
        changed = bool(cancelled.get("changed"))
        if state in _TERMINAL_STATES:
            # The shared supervisor is the cancellation authority. Mirror its
            # terminal truth into the legacy runner record without inventing a
            # cancellation when a concurrent worker already completed/failed.
            refreshed = _load_json(meta_path) or meta
            refreshed["state"] = state
            refreshed["outcome"] = state
            if state == "cancelled":
                refreshed["error"] = ""
            refreshed["ended_at"] = refreshed.get("ended_at") or _now()
            _atomic_json(meta_path, refreshed)
        return {
            "success": True,
            "changed": changed,
            "backend": self.name,
            "state": state,
        }


@dataclass
class PiRpcBackend(_LocalProcessBackend):
    name: str = "pi_rpc"

    def executable(self) -> str | None:
        configured = os.environ.get("HERMES_GPT_PI_EXE")
        candidates = [
            configured,
            shutil.which("pi"),
            str(Path.home() / ".local" / "bin" / "pi"),
        ]
        package_cli = (
            Path.home()
            / ".local"
            / "lib"
            / "node_modules"
            / "@earendil-works"
            / "pi-coding-agent"
            / "dist"
            / "cli.js"
        )
        candidates.append(str(package_cli))
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        tools = _pi_tools(contract)
        provider, model = _pi_selection(contract)
        if provider and not _allowed_by_env(provider, RUNNER_PROVIDER_ALLOWLIST_ENV):
            raise PermissionError(
                f"Pi provider {provider!r} is not allowed by {RUNNER_PROVIDER_ALLOWLIST_ENV}"
            )
        if model and not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(
                f"Pi model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
            )
        return {
            "protocol": "jsonl-rpc",
            "mode": "rpc",
            "tools": tools,
            "model": model or None,
            "provider": provider or None,
        }


@dataclass
class OmxBackend(_LocalProcessBackend):
    name: str = "omx"

    def executable(self) -> str | None:
        configured = os.environ.get("HERMES_GPT_OMX_EXE")
        candidates = [
            configured,
            shutil.which("omx"),
            "/usr/bin/omx",
            str(Path.home() / ".local" / "bin" / "omx"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        options = (contract.get("execution") or {}).get("options") or {}
        sandbox = _sandbox_for(contract, backend="omx")
        model = options.get("model")
        if model and not _allowed_by_env(str(model), RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(
                f"OMX model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
            )
        return {
            "mode": "exec",
            "json": True,
            "sandbox": sandbox,
            "model": model,
            "profile": options.get("profile"),
        }


@dataclass
class CodexBackend:
    name: str = "codex"

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            import operator_codex as op_codex

            status = op_codex.hermes_codex_status()
            if isinstance(status, str):
                status = json.loads(status)
            return {
                "available": bool(status.get("codex_available")),
                "enabled": bool(status.get("enabled")),
                "write_enabled": bool(status.get("write_enabled")),
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
        import operator_codex as op_codex

        options = (contract.get("execution") or {}).get("options") or {}
        workspace = contract["allowed_scope"]["workspaces"][0]
        sandbox = _sandbox_for(contract, backend="codex")
        model = options.get("model")
        if model and not _allowed_by_env(str(model), RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(
                f"Codex model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
            )
        result = op_codex.hermes_codex_start(
            prompt=contract["objective"],
            workdir=workspace,
            sandbox=sandbox,
            model=model,
            ignore_user_config=bool(options.get("ignore_user_config", False)),
            timeout=max(10, min(int(timeout), 3600)),
            confirm=confirm,
            dry_run=dry_run,
        )
        if isinstance(result, str):
            result = json.loads(result)
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if isinstance(job_id, str) and result.get("success"):
            try:
                meta = op_codex._load(job_id, hermes_root)
                if isinstance(meta, dict):
                    meta["task_id"] = contract["task_id"]
                    op_codex._save(meta, hermes_root)
            except Exception as exc:
                logger.debug(
                    "failed to persist Codex task linkage for %s", job_id, exc_info=exc
                )
        result["backend"] = self.name
        result.setdefault("task_id", contract["task_id"])
        return result

    def observed_runs(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> list[dict[str, Any]]:
        # Codex jobs use their own opaque job ids, so contract linkage is only
        # available when the operator metadata recorded task_id (newer stores).
        import operator_codex as op_codex

        root = op_codex._root(hermes_root)
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in list(root.glob("*.json"))[:500]:
            meta = _load_json(path)
            if not meta or meta.get("task_id") != task_id:
                continue
            state = str(meta.get("state") or meta.get("status") or "unknown")
            out.append(
                {
                    "task_id": task_id,
                    "status": state,
                    "outcome": meta.get("outcome") or state,
                    "error": meta.get("error") or None,
                    "started_at": meta.get("started_at") or meta.get("created_at"),
                    "ended_at": meta.get("ended_at") or meta.get("completed_at"),
                    "scope": "runner:codex",
                }
            )
        return out

    def cancel(
        self, task_id: str, *, hermes_root: Path | None = None
    ) -> dict[str, Any]:
        return {
            "success": False,
            "code": "RUNNER_CANCEL_REQUIRES_JOB_ID",
            "backend": self.name,
        }
