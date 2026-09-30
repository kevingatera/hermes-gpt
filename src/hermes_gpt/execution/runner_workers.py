"""Detached local runner workers and their bounded result handling."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.execution import job_supervisor
from hermes_gpt.policy import authorization as op
from hermes_gpt.policy import confinement
from hermes_gpt.execution.runner_common import RUNNER_MODEL_ALLOWLIST_ENV, RUNNER_PROVIDER_ALLOWLIST_ENV, _allowed_by_env, _append_event, _atomic_json, _bounded_text, _load_json, _minimal_child_env, _now, _pi_child_env, _pi_selection, _pi_tools, _popen_process_group, _sandbox_for, _terminate_process_group
from hermes_gpt.execution.runner_local import _LocalProcessBackend
from hermes_gpt.execution.runner_opencode import _opencode_child_config, _opencode_runtime_material, _OpenCodeCredentialProxy
from hermes_gpt.execution.runner_registry import get_backend


def _extract_pi_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _worker_pi(
    exe: str,
    contract: dict[str, Any],
    timeout: int,
    log_path: Path,
    hermes_root: Path | None = None,
) -> tuple[int, str]:
    options = (contract.get("execution") or {}).get("options") or {}
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
    argv = [exe, "--mode", "rpc", "--no-session", "--tools", tools]
    writable = bool(set(tools.split(",")) - {"read"})
    if not writable:
        # Pi extensions execute arbitrary startup code outside the built-in tool
        # allowlist. A read-only tool posture must disable extension discovery
        # even when the contract itself carries a write-authorized class.
        argv.append("--no-extensions")
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    if options.get("thinking"):
        argv += ["--thinking", str(options["thinking"])]
    child_env = _pi_child_env(contract, hermes_root, provider)
    workspaces = contract.get("allowed_scope", {}).get("workspaces") or []
    if not workspaces:
        raise PermissionError("pi_rpc sessions require an allowed workspace")
    workspace = Path(str(workspaces[0])).expanduser().resolve()
    # Every Pi child is physically scoped to the authorized workspace. Read-only
    # toolsets receive a read-only workspace mount/profile; write-capable
    # toolsets receive a writable workspace only after _pi_tools() has enforced
    # the independent authorization + sandbox gates.
    argv = confinement.wrap_argv(argv, workspace, writable=writable)
    proc = _popen_process_group(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # Route stderr away from PIPE so noisy Pi startup/logging cannot fill an
        # unconsumed pipe and stall RPC progress before agent_settled.
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=child_env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(
        json.dumps(
            {"id": "dispatch", "type": "prompt", "message": contract["objective"]},
            ensure_ascii=False,
        )
        + "\n"
    )
    proc.stdin.flush()
    final_text = ""
    settled = False
    rpc_error = ""
    deadline = datetime.now(timezone.utc).timestamp() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    while datetime.now(timezone.utc).timestamp() < deadline:
        remaining = max(0.0, deadline - datetime.now(timezone.utc).timestamp())
        ready = selector.select(timeout=min(0.5, remaining))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype in {
            "response",
            "agent_start",
            "agent_end",
            "agent_settled",
            "turn_end",
            "message_end",
            "extension_error",
            "auto_retry_start",
            "auto_retry_end",
        }:
            _append_event(
                log_path,
                {
                    "type": etype,
                    "at": _now(),
                    "success": event.get("success"),
                    "command": event.get("command"),
                },
            )
        if (
            etype == "response"
            and event.get("command") == "prompt"
            and event.get("success") is False
        ):
            rpc_error = _bounded_text(
                event.get("error") or "Pi RPC prompt rejected", 500
            )
            break
        if etype == "message_end":
            text = _extract_pi_text(event.get("message"))
            if text:
                final_text = text
        if etype == "agent_settled":
            settled = True
            break
    selector.close()
    if rpc_error:
        _terminate_process_group(proc)
        raise RuntimeError(f"Pi RPC prompt failed: {rpc_error}")
    if not settled:
        if proc.poll() is None:
            _terminate_process_group(proc)
            return 124, final_text
        rc = int(proc.returncode or 0)
        return (rc if rc else 1), final_text
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        rc = int(proc.returncode or 124)
    return rc, final_text


def _worker_opencode(
    exe: str,
    contract: dict[str, Any],
    timeout: int,
    log_path: Path,
    hermes_root: Path | None = None,
) -> tuple[int, str]:
    options = (contract.get("execution") or {}).get("options") or {}
    sandbox = _sandbox_for(contract, backend="opencode")
    writable = sandbox == "workspace-write"
    if not confinement.confinement_available(writable=writable, expose_proc=True):
        posture = "write-capable" if writable else "read-only"
        raise PermissionError(
            f"opencode {posture} sessions require usable filesystem confinement; set "
            f"{confinement.CONFINEMENT_ENABLE_ENV}=1 and install a working bubblewrap "
            "(or sandbox-exec on macOS)"
        )
    workspace = Path(contract["allowed_scope"]["workspaces"][0]).expanduser().resolve()
    material = _opencode_runtime_material(exe, contract, hermes_root)
    proxy = _OpenCodeCredentialProxy(material)
    proxy_thread = threading.Thread(
        target=proxy.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    proxy_thread.start()
    argv = [
        exe,
        "run",
        "--format",
        "json",
        "--pure",
        "--dir",
        str(workspace),
        "--model",
        material["model"],
    ]
    agent = str(options.get("agent") or "").strip()
    variant = str(options.get("variant") or "").strip()
    if agent:
        argv += ["--agent", agent]
    if variant:
        argv += ["--variant", variant]
    # Keep the objective out of argv/process listings. The confined child sees
    # only a dummy relay credential and an ephemeral /tmp XDG home. The real
    # provider credential remains in this trusted worker and is injected only
    # by the loopback relay.
    child_env = _minimal_child_env()
    child_env.update(
        {
            "XDG_CONFIG_HOME": "/tmp/hermes-opencode/config",
            "XDG_DATA_HOME": "/tmp/hermes-opencode/data",
            "XDG_CACHE_HOME": "/tmp/hermes-opencode/cache",
            "XDG_STATE_HOME": "/tmp/hermes-opencode/state",
            "OPENCODE_CONFIG_CONTENT": _opencode_child_config(
                proxy.material, proxy.server_port
            ),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
    )
    argv = confinement.wrap_argv(argv, workspace, writable=writable, expose_proc=True)
    try:
        proc = _popen_process_group(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
        try:
            stdout, stderr = proc.communicate(
                input=contract["objective"], timeout=timeout
            )
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            _append_event(log_path, {"type": "timeout", "at": _now()})
            return 124, ""
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2)
    final_text = ""
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = str(event.get("type") or "event")[:96]
        _append_event(log_path, {"type": etype, "at": _now()})
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        text = part.get("text") or event.get("text")
        if isinstance(text, str) and text:
            final_text = text
    if proc.returncode and stderr:
        _append_event(
            log_path,
            {"type": "stderr", "at": _now(), "summary": _bounded_text(stderr, 500)},
        )
    return int(proc.returncode or 0), final_text


def _worker_omx(
    exe: str, contract: dict[str, Any], timeout: int, log_path: Path
) -> tuple[int, str]:
    options = (contract.get("execution") or {}).get("options") or {}
    sandbox = _sandbox_for(contract, backend="omx")
    workspace = contract["allowed_scope"]["workspaces"][0]
    argv = [exe, "exec", "--json", "-C", workspace, "--sandbox", sandbox]
    if options.get("model"):
        model = str(options["model"])
        if not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(
                f"OMX model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
            )
        argv += ["--model", model]
    if options.get("profile"):
        argv += ["--profile", str(options["profile"])]
    argv.append("-")
    proc = _popen_process_group(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(input=contract["objective"], timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        _append_event(log_path, {"type": "timeout", "at": _now()})
        return 124, ""
    final_text = ""
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        _append_event(log_path, {"type": etype or "event", "at": _now()})
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        text = item.get("text") or event.get("text")
        if isinstance(text, str) and text:
            final_text = text
    if proc.returncode and stderr:
        _append_event(
            log_path,
            {"type": "stderr", "at": _now(), "summary": _bounded_text(stderr, 500)},
        )
    return int(proc.returncode or 0), final_text


def _worker(task_id: str, jobs_root: Path) -> int:
    meta_path = jobs_root / f"{task_id}.json"
    request_path = jobs_root / f"{task_id}.request.json"
    log_path = jobs_root / f"{task_id}.jsonl"
    meta = _load_json(meta_path) or {}
    request = _load_json(request_path)
    if not request or not isinstance(request.get("contract"), dict):
        meta.update(
            {
                "state": "failed",
                "outcome": "failed",
                "ended_at": _now(),
                "error": "runner request missing",
            }
        )
        _atomic_json(meta_path, meta)
        return 2
    contract = request["contract"]
    backend_name = str(request.get("backend") or "")
    timeout = max(10, min(int(request.get("timeout") or 900), 3600))
    request_root_raw = request.get("hermes_root")
    request_root = (
        Path(str(request_root_raw)).expanduser() if request_root_raw else None
    )
    normalized_root = (
        op.normalize_hermes_data_root(request_root)
        if request_root is not None
        else None
    )
    worker_hermes_root = (
        Path(normalized_root) if normalized_root is not None else request_root
    )
    # The objective is needed only to start the worker. Remove the durable
    # request envelope as soon as it has been loaded so prompt text is not
    # retained after dispatch.
    try:
        request_path.unlink()
    except OSError:
        pass

    def _terminalize(
        meta: dict[str, Any], *, state: str, rc: int | None = None, error: str = ""
    ) -> None:
        """Persist a terminal state, resolving to ``cancelled`` if a cancel
        marker exists (cancellation wins over later completed/failed). Clean
        the marker once the job is safely terminal."""
        cancelled = (jobs_root / f"{task_id}.cancel.json").exists()
        if cancelled:
            meta.update({"state": "cancelled", "outcome": "cancelled", "error": ""})
        else:
            meta.update({"state": state, "outcome": state})
            if error:
                meta["error"] = error
        if rc is not None:
            meta["returncode"] = rc
        meta["ended_at"] = _now()
        _atomic_json(meta_path, meta)
        # Close the check/write race with hermes_runner_cancel: cancellation
        # writes the marker before publishing cancelled metadata. If that marker
        # appeared after our first check but before/just after the terminal
        # write, cancellation still wins and no later worker write follows.
        cancel_path = jobs_root / f"{task_id}.cancel.json"
        if not cancelled and cancel_path.exists():
            cancelled = True
            meta.update(
                {
                    "state": "cancelled",
                    "outcome": "cancelled",
                    "error": "",
                    "ended_at": _now(),
                }
            )
            _atomic_json(meta_path, meta)
        if cancelled:
            try:
                cancel_path.unlink()
            except OSError:
                pass
        try:
            job_supervisor.terminalize(
                task_id,
                "cancelled" if cancelled else state,
                returncode=rc,
                summary=error,
                hermes_root=jobs_root.parent,
            )
        except FileNotFoundError:
            pass

    try:
        backend = get_backend(backend_name)
        exe = (
            backend.executable() if isinstance(backend, _LocalProcessBackend) else None
        )
        if not exe:
            raise RuntimeError(f"{backend_name} executable not found")
        meta.update(
            {
                "state": "running",
                "started_at": meta.get("started_at") or _now(),
                "pid": os.getpid(),
            }
        )
        _atomic_json(meta_path, meta)
        try:
            job_supervisor.mark_running(
                task_id, os.getpid(), hermes_root=jobs_root.parent
            )
        except FileNotFoundError:
            pass
        if (jobs_root / f"{task_id}.cancel.json").exists():
            _terminalize(meta, state="cancelled")
            return 0
        if backend_name == "pi_rpc":
            rc, _ = _worker_pi(exe, contract, timeout, log_path, worker_hermes_root)
        elif backend_name == "opencode":
            rc, _ = _worker_opencode(
                exe, contract, timeout, log_path, worker_hermes_root
            )
        elif backend_name == "omx":
            rc, _ = _worker_omx(exe, contract, timeout, log_path)
        else:
            raise RuntimeError(f"local worker does not support backend {backend_name}")
        meta["returncode"] = rc
        if rc == 0:
            _terminalize(meta, state="completed", rc=rc)
        else:
            _terminalize(
                meta,
                state="failed",
                rc=rc,
                error="runner timed out"
                if rc == 124
                else f"runner exited with code {rc}",
            )
        # Completion evidence is state/exit metadata only. Do not persist the
        # model's final text in the runner store; contract validation must not
        # depend on worker self-report or retain prompt-derived output.
        return rc
    except Exception as exc:  # noqa: BLE001
        _terminalize(meta, state="failed", error=_bounded_text(exc, 500))
        return 1
