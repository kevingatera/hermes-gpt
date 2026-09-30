"""Gateway health and restart operations.

Status reads sanitized state. Restart uses a fixed argv and the workspace
policy gates before it invokes Hermes.
"""

from __future__ import annotations

# Tool handlers return structured errors instead of leaking failures over MCP.
# ruff: noqa: BLE001
import json
import os
import shutil
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op

# Gateway
# ---------------------------------------------------------------------------


def _gateway_pid_path(profile_home: Path) -> Path:
    return profile_home / "gateway.pid"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"


def _read_gateway_state(state_path: Path) -> dict[str, Any]:
    """Read gateway_state.json safely.

    Never raises. Returns an empty dict if the file is missing, invalid,
    unreadable, or not a JSON object.
    """
    if not state_path.exists():
        return {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except (OSError, ValueError, UnicodeDecodeError):
        # Undecodable bytes (invalid UTF-8) or malformed JSON are a state,
        # not an error: the caller sees an empty state and reports the
        # gateway as not running rather than crashing the doctor pass.
        pass
    return {}


def _read_gateway_pid_from_pid_file(pid_path: Path) -> int | None:
    """Read gateway.pid as a plain integer.

    Older Hermes Gateway versions store only a raw PID here.
    If the file is absent, empty, or not parseable, return None.
    """
    if not pid_path.exists():
        return None
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except (OSError, ValueError):
        return None


def _read_gateway_pid_from_state(state: dict[str, Any]) -> int | None:
    """Read PID from gateway_state.json.

    Newer Hermes Gateway versions may store the actual PID in:
    {
      "pid": 9818,
      "kind": "hermes-gateway",
      "gateway_state": "running"
    }

    Accept int or numeric string. Otherwise return None.
    """
    value = state.get("pid")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_pid_alive(pid: int | None) -> bool:
    """Best-effort process liveness probe.

    Prefer psutil when available. Fall back to os.kill(pid, 0).
    Never raises.
    """
    if pid is None:
        return False

    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:  # noqa: S110
        # psutil is optional; fall back to the standard-library probe below.
        pass  # Fall through to os.kill fallback

    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _read_ticker_heartbeat(profile_home: Path) -> float | None:
    hb_path = profile_home / "cron" / "ticker_heartbeat"
    if not hb_path.exists():
        return None
    try:
        return hb_path.stat().st_mtime
    except OSError:
        return None


def _gateway_adapters_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sanitized adapter/platform summary across gateway-state schemas.

    Hermes has emitted two platform-state shapes over time:

    Legacy::

        {"telegram": {"connected": true}}

    Current::

        {"platforms": {"telegram": {"state": "connected", ...}}}

    Prefer an explicit legacy ``connected`` boolean when present; otherwise
    derive connectivity from the current ``state`` field.  Only safe status
    metadata is surfaced.  Tokens, URLs, and secret-like values are never
    copied into the result.
    """

    def _sanitize(name: str, entry: dict[str, Any]) -> dict[str, Any]:
        if "connected" in entry:
            connected = bool(entry.get("connected"))
        else:
            connected = str(entry.get("state") or "").strip().lower() == "connected"

        result: dict[str, Any] = {
            "name": name,
            "connected": connected,
        }
        platform_state = entry.get("state")
        if isinstance(platform_state, str) and platform_state.strip():
            result["state"] = platform_state.strip()
        if isinstance(entry.get("needs_attention"), bool):
            result["needs_attention"] = entry["needs_attention"]
        if isinstance(entry.get("updated_at"), str) and entry["updated_at"].strip():
            result["updated_at"] = entry["updated_at"].strip()
        return result

    adapters: list[dict[str, Any]] = []
    seen: set[str] = set()

    legacy_keys = ("telegram", "discord", "slack", "signal", "whatsapp", "api_server")
    for key in legacy_keys:
        entry = state.get(key)
        if isinstance(entry, dict):
            adapters.append(_sanitize(key, entry))
            seen.add(key)

    platforms = state.get("platforms")
    if isinstance(platforms, dict):
        for key, entry in platforms.items():
            name = str(key)
            if name in seen:
                continue
            if isinstance(entry, dict):
                adapters.append(_sanitize(name, entry))
                seen.add(name)

    return adapters


def hermes_gateway_status(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Return Hermes Gateway status.

    Important behavior:
    - First try ~/.hermes/gateway.pid.
    - If gateway.pid is missing or unparsable, fall back to gateway_state.json["pid"].
    - This fixes the case where Hermes Gateway is actually running but the PID
      file is absent/stale while gateway_state.json contains the correct PID.
    """
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)

        pid_path = _gateway_pid_path(profile_home)
        state_path = _gateway_state_path(profile_home)

        state = _read_gateway_state(state_path)

        pid = _read_gateway_pid_from_pid_file(pid_path)
        pid_source = "gateway.pid" if pid is not None else None

        if pid is None:
            pid = _read_gateway_pid_from_state(state)
            pid_source = "gateway_state.json" if pid is not None else None

        running = _is_pid_alive(pid)
        ticker_heartbeat = _read_ticker_heartbeat(profile_home)
        adapters_summary = _gateway_adapters_summary(state)

        result = {
            "success": True,
            "profile": profile,
            "gateway_pid": pid,
            "gateway_pid_source": pid_source,
            "gateway_running": running,
            "gateway_state": state.get("gateway_state"),
            "gateway_kind": state.get("kind"),
            "gateway_updated_at": state.get("updated_at"),
            "gateway_restart_requested": state.get("restart_requested"),
            "gateway_exit_reason": state.get("exit_reason"),
            "gateway_active_agents": state.get("active_agents"),
            "ticker_heartbeat_mtime": ticker_heartbeat,
            "adapters": adapters_summary,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="gateway",
                code="GATEWAY_STATUS_ERROR",
                suggested_action=(
                    "Check gateway.pid, gateway_state.json, and profile name."
                ),
            ),
            indent=2,
        )


def _hermes_cli() -> str:
    """Return the Hermes CLI executable used by fixed-argv gateway commands."""
    configured = os.environ.get("HERMES_CLI", "").strip()
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("hermes")
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / "hermes"
    if local_bin.exists():
        return str(local_bin)
    return "hermes"


def _hermes_argv(profile: str, sub: list[str]) -> list[str]:
    """Return the stable logical Hermes argv used by plans and injected runners."""
    if profile == "default":
        return ["hermes", *sub]
    return ["hermes", "-p", profile, *sub]


def _gateway_restart_argv(profile: str) -> list[str]:
    return _hermes_argv(profile, ["gateway", "restart"])


def _hermes_gateway_restart_raw(
    profile: str = "default",
    runner=None,
) -> tuple[int, str, str]:
    """Execute the raw gateway restart argv. Returns (rc, stdout, stderr).

    No policy checks; callers must gate mutation themselves.
    """
    argv = _gateway_restart_argv(profile)
    if runner is None:
        argv = [_hermes_cli(), *argv[1:]]
        run_fn = op.run_argv
    else:
        run_fn = runner
    return run_fn(argv, timeout=120, workdir=None)


def hermes_gateway_restart(
    profile: str = "default",
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_profile(profile, hermes_root)
        argv = _gateway_restart_argv(profile)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_restart": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
            }
            op.audit_record(
                tool="hermes_gateway_restart",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        rc, out, err = _hermes_gateway_restart_raw(profile, runner=runner)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
            "note": (
                "If hermes does not support 'gateway restart', this command "
                "may have failed. Try 'hermes gateway stop' + 'hermes gateway start' "
                "manually."
            ),
        }
        op.audit_record(
            tool="hermes_gateway_restart",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc}",
            profile=profile,
            error=op.redact_output(err) if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_gateway_restart",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="gateway",
                code="GATEWAY_RESTART_ERROR",
                suggested_action=(
                    "Check Hermes CLI availability, profile, and operator "
                    "level/apply mode."
                ),
            ),
            indent=2,
        )
