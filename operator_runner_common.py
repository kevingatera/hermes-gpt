"""Shared validation, process, environment, and job-store helpers for runners."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_fleet as op_fleet
import operator_policy as op
import runner_confinement as confinement

SCHEMA_VERSION = "0.6-runner.1"


RUNNER_PLUGINS_ENV = "HERMES_GPT_ENABLE_RUNNER_PLUGINS"


RUNNER_PLUGIN_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_PLUGIN_ALLOWLIST"


RUNNER_BACKEND_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_BACKEND_ALLOWLIST"


RUNNER_PROVIDER_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_PROVIDER_ALLOWLIST"


RUNNER_MODEL_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_MODEL_ALLOWLIST"


_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


_TASK_ID_RE = op_fleet._TASK_ID_RE


_MAX_OPTIONS_BYTES = 8_000


_MAX_RESULT_CHARS = 8_000


_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    base = op.normalize_hermes_data_root(
        hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    return Path(base or Path.home() / ".hermes") / "runner-jobs"


def _job_paths(
    task_id: str, hermes_root: Path | None = None
) -> tuple[Path, Path, Path]:
    root = _root(hermes_root)
    return (
        root / f"{task_id}.json",
        root / f"{task_id}.request.json",
        root / f"{task_id}.jsonl",
    )


def _cancel_path(task_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{task_id}.cancel.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _bounded_text(value: Any, maximum: int = _MAX_RESULT_CHARS) -> str:
    text = op.redact_output(str(value or ""))
    return text if len(text) <= maximum else text[: maximum - 3] + "..."


def _popen_process_group(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a child with a platform-specific process-tree boundary."""
    kwargs.setdefault("close_fds", True)
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(argv, **kwargs)


def _windows_taskkill(pid: int, *, timeout: float) -> bool:
    """Terminate a Windows process tree, returning whether taskkill succeeded."""
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _terminate_process_tree(
    proc: subprocess.Popen[Any] | int, *, timeout: float = 5.0
) -> None:
    """Terminate a process tree created by _popen_process_group.

    Live Popen handles support bounded waits and escalation. Explicit runner
    cancellation only has the durable worker PID, so an integer PID follows the
    same platform dispatch without assuming this process owns a wait handle.

    POSIX uses the child session/process group. Windows uses taskkill /T /F for
    descendants and falls back to direct-process termination when taskkill is
    unavailable, times out, or reports failure.
    """
    detached = isinstance(proc, int)
    pid = proc if detached else proc.pid
    if pid <= 1 or (not detached and proc.poll() is not None):
        return
    if os.name == "nt":
        tree_terminated = _windows_taskkill(pid, timeout=timeout)
        if not tree_terminated:
            try:
                if detached:
                    # On Windows, os.kill(..., SIGTERM) uses TerminateProcess.
                    os.kill(pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if detached:
            return
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if detached:
        return
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _terminate_process_group(
    proc: subprocess.Popen[Any] | int, *, timeout: float = 5.0
) -> None:
    """Backward-compatible alias for the process-tree terminator."""
    _terminate_process_tree(proc, timeout=timeout)


def _audit_runner(
    *,
    tool: str,
    policy: op.OperatorPolicy,
    dry_run: bool,
    success: bool,
    changed: bool = False,
    summary: str = "",
    task_id: str = "",
    backend: str = "",
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=summary,
            extra={"task_id": task_id, "backend": backend},
        )
    except Exception as exc:
        logger.debug("runner audit failed", exc_info=exc)


def _split_allowlist(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _allowed_by_env(value: str, env_name: str) -> bool:
    allowed = _split_allowlist(os.environ.get(env_name))
    return not allowed or value in allowed


def _runner_allowed(name: str) -> bool:
    return _allowed_by_env(name, RUNNER_BACKEND_ALLOWLIST_ENV)


def _minimal_child_env() -> dict[str, str]:
    """Return a non-secret, explicit child env baseline for local runners."""
    allowed_keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "PYTHONIOENCODING",
        "PI_CODING_AGENT_DIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
    return {
        key: value for key, value in os.environ.items() if key in allowed_keys and value
    }


def _cleanup_stale_request_envelopes(
    *, hermes_root: Path | None = None, ttl_seconds: int = 3600
) -> int:
    """Delete stale transient request envelopes after their TTL expires."""
    root = _root(hermes_root)
    if not root.is_dir():
        return 0
    now = datetime.now(timezone.utc).timestamp()
    removed = 0
    for request_path in root.glob("*.request.json"):
        try:
            age = now - request_path.stat().st_mtime
        except OSError:
            continue
        if age < ttl_seconds:
            continue
        try:
            request_path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def normalize_execution(value: Any) -> dict[str, Any] | None:
    """Validate an optional contract ``execution`` selector.

    The block is deliberately backend-agnostic. ``options`` must be a bounded
    JSON object; individual backends validate the options they understand.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("execution must be an object")
    backend = str(value.get("backend") or "").strip().lower()
    if not _BACKEND_RE.fullmatch(backend):
        raise ValueError("execution.backend is invalid")
    options = value.get("options") or {}
    if not isinstance(options, dict):
        raise TypeError("execution.options must be an object")
    secretish = re.compile(
        r"(?:secret|token|password|api[_-]?key|credential|private[_-]?key)",
        re.IGNORECASE,
    )
    bad_keys = [str(key) for key in options if secretish.search(str(key))]
    if bad_keys:
        raise ValueError(
            "execution.options must not carry secrets; use runner environment/config instead"
        )
    try:
        encoded = json.dumps(
            options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("execution.options must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > _MAX_OPTIONS_BYTES:
        raise ValueError(f"execution.options exceeds {_MAX_OPTIONS_BYTES} bytes")
    return {"backend": backend, "options": options}


def _authorization_class(contract: dict[str, Any]) -> str:
    return str(contract.get("authorization", {}).get("class") or "none")


_PI_WRITE_AUTH_CLASSES = frozenset({"reversible_write", "high_impact"})


def _pi_tools(contract: dict[str, Any]) -> str:
    """Return Pi tools for the execution posture.

    Every Pi session requires a demonstrably usable OS confinement posture.
    Read-only sessions are physically scoped to the authorized workspace;
    write-capable tools additionally require a write-authorized contract and
    the ``workspace-write`` sandbox. CWD alone is never treated as a sandbox.
    """
    options = (contract.get("execution") or {}).get("options") or {}
    auth_class = _authorization_class(contract)
    writable_auth = auth_class in _PI_WRITE_AUTH_CLASSES

    requested = options.get("tools")
    if requested is None or requested == "":
        tools = ["read"]
    else:
        if not isinstance(requested, str):
            raise TypeError(
                "pi_rpc execution.options.tools must be a comma-delimited string"
            )
        tools = [item.strip() for item in requested.split(",") if item.strip()]
        if not tools:
            raise ValueError("pi_rpc execution.options.tools must not be empty")

    write_tools = set(tools) - {"read"}
    writable = bool(write_tools)
    if writable:
        # Authorization is a separate trust boundary from OS confinement.
        # A usable sandbox must never upgrade ``none``/``read_only`` into a
        # write-capable contract.
        if not writable_auth:
            raise PermissionError(
                "pi_rpc read-only authorization may only enable Pi's read tool; write tools require "
                "authorization.class=reversible_write or high_impact"
            )
        if options.get("sandbox") != "workspace-write":
            raise PermissionError(
                "pi_rpc write tools require execution.options.sandbox=workspace-write"
            )

    if not confinement.confinement_available(writable=writable):
        posture = "write-capable" if writable else "read-only"
        raise PermissionError(
            f"pi_rpc {posture} sessions require usable filesystem confinement; set "
            f"{confinement.CONFINEMENT_ENABLE_ENV}=1 and install a working bubblewrap "
            "(or sandbox-exec on macOS)"
        )

    return ",".join(dict.fromkeys(tools))


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return (
        Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
    )


def _pi_selection(contract: dict[str, Any]) -> tuple[str, str]:
    """Return the effective Pi provider/model without exposing credentials."""
    options = (contract.get("execution") or {}).get("options") or {}
    provider = str(options.get("provider") or "").strip()
    model = str(options.get("model") or "").strip()
    settings = _load_json(_pi_agent_dir() / "settings.json") or {}
    if not provider:
        provider = str(settings.get("defaultProvider") or "").strip()
    if not model:
        model = str(settings.get("defaultModel") or "").strip()
    return provider, model


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _pi_child_env(
    contract: dict[str, Any], hermes_root: Path | None, provider: str
) -> dict[str, str]:
    """Build a minimal child environment with only the selected provider credential.

    This intentionally does not inherit ``os.environ`` wholesale. The child gets
    a small non-secret baseline plus, when configured, exactly the single env-ref
    credential referenced by the selected Pi provider.
    """
    child_env = _minimal_child_env()
    if not provider:
        return child_env

    models = _load_json(_pi_agent_dir() / "models.json") or {}
    providers = (
        models.get("providers") if isinstance(models.get("providers"), dict) else {}
    )
    config = providers.get(provider) if isinstance(providers, dict) else None
    raw_key = config.get("apiKey") if isinstance(config, dict) else None
    if not isinstance(raw_key, str):
        return child_env
    match = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", raw_key.strip())
    if not match:
        return child_env
    key_name = match.group(1)

    profile = str(contract.get("assigned_profile") or "default")
    allowed_profiles = contract.get("allowed_scope", {}).get("profiles") or []
    if allowed_profiles and profile not in allowed_profiles:
        raise PermissionError("Pi runner profile is outside the contract allowed_scope")

    value = os.environ.get(key_name)
    if not value:
        profile_home = op.resolve_profile_home(profile, hermes_root)
        import operator_config as op_config

        value = op_config._read_env_value(profile_home / ".env", key_name)
    if value:
        child_env[key_name] = _unquote_env_value(value)
    return child_env


def _sandbox_for(contract: dict[str, Any], *, backend: str) -> str:
    options = (contract.get("execution") or {}).get("options") or {}
    auth_class = _authorization_class(contract)
    requested = options.get("sandbox")
    sandbox = str(
        requested
        or ("read-only" if auth_class in {"none", "read_only"} else "workspace-write")
    )
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError(
            f"{backend} execution.options.sandbox must be read-only or workspace-write"
        )
    if auth_class in {"none", "read_only"} and sandbox != "read-only":
        raise PermissionError(
            f"read-only authorization may not use {backend} workspace-write sandbox"
        )
    return sandbox


# Child processes re-enter the compatibility facade so its worker mode loads
# the same built-in and explicitly allowlisted plugin registry.
_RUNNER_ENTRYPOINT = Path(__file__).resolve().with_name("operator_runners.py")
