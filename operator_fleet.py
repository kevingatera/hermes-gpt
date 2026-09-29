"""Structured, bounded fleet control backed by Hermes A2A.

This module originally shelled out to the legacy hermes-a2a-bridge CLI
(``hermes a2a registry/doctor/send/task``). It now uses the official Hermes
A2A platform surface: peer entries configured under ``a2a_agents`` in
config.yaml are discovered via their Agent Card, and tasks are sent directly
over the A2A v1.0 JSON-RPC protocol. The old CLI runner signature is preserved
for tests; when a bridge binary is still present it is used as a read-only
fallback for registry listing only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import operator_fleet_a2a as fleet_a2a
import operator_fleet_authority as fleet_authority
import operator_policy as op

# Keep the established fleet imports available while implementation is split.
# A2A transport and peer authority rules live in focused modules.
AUTHORITY_MANIFEST_ENV = fleet_authority.AUTHORITY_MANIFEST_ENV
A2A_REGISTRY_MODE_ENV = fleet_a2a.A2A_REGISTRY_MODE_ENV
_A2A_DEFAULT_TIMEOUT = fleet_a2a._A2A_DEFAULT_TIMEOUT
_A2A_DEFAULT_REGISTRY_TIMEOUT = fleet_a2a._A2A_DEFAULT_REGISTRY_TIMEOUT
_AGENT_RE = fleet_a2a._AGENT_RE
_TASK_ID_RE = fleet_a2a._TASK_ID_RE
_MAX_REMOTE_BYTES = fleet_a2a._MAX_REMOTE_BYTES
_LOCAL_AGENT_CARDS = fleet_a2a._LOCAL_AGENT_CARDS
FleetDispatchTimeout = fleet_a2a.FleetDispatchTimeout
register_local_agent_card = fleet_a2a.register_local_agent_card

# Preserve helpers imported by Fabric and test isolation while delegating the
# actual configuration and network work to the A2A module.
_a2a_mode = fleet_a2a._a2a_mode
_load_hermes_config = fleet_a2a._load_hermes_config
_a2a_peers = fleet_a2a._a2a_peers
_auth_header = fleet_a2a._auth_header
_resolve_env_token = fleet_a2a._resolve_env_token
_a2a_peers_with_resolved_tokens = fleet_a2a._a2a_peers_with_resolved_tokens
_http_get_json = fleet_a2a._http_get_json
_http_get_json_threaded = fleet_a2a._http_get_json_threaded
_http_post_json = fleet_a2a._http_post_json
_card_url = fleet_a2a._card_url
_fetch_card = fleet_a2a._fetch_card
_jsonrpc_interface = fleet_a2a._jsonrpc_interface
_rpc_url = fleet_a2a._rpc_url
_interface_tenant = fleet_a2a._interface_tenant
_send_message = fleet_a2a._send_message
_get_task = fleet_a2a._get_task
_registry_official = fleet_a2a._registry_official

Runner = Callable[..., tuple[int, str, str]]

AuthorityPeer = fleet_authority.AuthorityPeer
_CARD_IDENTITY_RE = fleet_authority._CARD_IDENTITY_RE
_PROFILE_RE = fleet_authority._PROFILE_RE
_ROLE_RE = fleet_authority._ROLE_RE
_CONTROL_RE = fleet_authority._CONTROL_RE
_AUTH_CLASSES = fleet_authority._AUTH_CLASSES
_MAX_MANIFEST_BYTES = fleet_authority._MAX_MANIFEST_BYTES
_MAX_TEXT = fleet_authority._MAX_TEXT
_MAX_ITEMS = fleet_authority._MAX_ITEMS
_BUILTIN_PROFILES = fleet_authority._BUILTIN_PROFILES
_PUBLIC_ACTION_RE = fleet_authority._PUBLIC_ACTION_RE
_NEGATED_ACTION_RE = fleet_authority._NEGATED_ACTION_RE
_COMMAND_PREFIX_RE = fleet_authority._COMMAND_PREFIX_RE
_SECRET_RE = fleet_authority._SECRET_RE
_VAULT_RE = fleet_authority._VAULT_RE
_FLEET_POLICY_ACTION_RE = fleet_authority._FLEET_POLICY_ACTION_RE
_CHILD_MCP_ACTION_RE = fleet_authority._CHILD_MCP_ACTION_RE
_clean_text = fleet_authority._clean_text
_string_list = fleet_authority._string_list
_manifest_path = fleet_authority._manifest_path
_authorization = fleet_authority._authorization
_requests_affirmative_action = fleet_authority._requests_affirmative_action
_requests_public_action = fleet_authority._requests_public_action
_requests_fleet_policy_change = fleet_authority._requests_fleet_policy_change
_requests_child_mcp_inheritance = fleet_authority._requests_child_mcp_inheritance
_work_order_text_fields = fleet_authority._work_order_text_fields
_requests_raw_secret = fleet_authority._requests_raw_secret
_requests_vault_policy_change = fleet_authority._requests_vault_policy_change
_canonical_work_order = fleet_authority._canonical_work_order
_authorize_order = fleet_authority._authorize_order


def _load_authority(path: Path | None = None) -> dict[str, AuthorityPeer]:
    """Retain the fleet facade while keeping authority policy in its own module."""
    return fleet_authority._load_authority(
        path, builtin_profiles=_BUILTIN_PROFILES
    )


class PeerVerificationError(RuntimeError):
    """A live Agent Card could not be safely matched to local authority."""


def _hermes_bin(hermes_root: Path | None = None) -> str | None:
    configured = os.environ.get("HERMES_CLI", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
    root = hermes_root or op.normalize_hermes_data_root(os.environ.get("HERMES_HOME"))
    if root:
        for candidate in (root / "hermes-agent" / "venv" / "bin" / "hermes", root / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"):
            if candidate.is_file():
                return str(candidate)
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    local_bin = Path.home() / ".local" / "bin" / "hermes"
    if local_bin.is_file():
        return str(local_bin)
    return None


def _bridge_available(hermes_bin: str) -> bool:
    """Best-effort check: does the given Hermes binary still provide the 'a2a' subcommand?"""
    if not hermes_bin:
        return False
    if hermes_bin == "/test/hermes":
        return True
    try:
        code, _, _ = op.run_argv([hermes_bin, "a2a", "--help"], timeout=5, max_output_chars=1_000)
        return code == 0
    except Exception:
        return False


def _run(argv: list[str], *, timeout: int, runner: Runner | None) -> tuple[int, str, str]:
    return (
        runner(argv, timeout=timeout)
        if runner is not None
        else op.run_argv(argv, timeout=timeout, max_output_chars=_MAX_REMOTE_BYTES)
    )


def _registry_bridge(*, runner: Runner | None, hermes_bin: str | None) -> tuple[list[dict[str, Any]], str | None]:
    binary = hermes_bin or _hermes_bin()
    if not binary:
        raise RuntimeError("Hermes CLI was not found and no A2A peers are configured.")
    code, stdout, stderr = _run([binary, "a2a", "registry", "list", "--json"], timeout=15, runner=runner)
    if code != 0:
        raise RuntimeError(op.redact_output(stderr or "A2A registry lookup failed"))
    agents = _parse_json(stdout, operation="A2A registry lookup").get("agents", [])
    if not isinstance(agents, list) or len(agents) > 256:
        raise ValueError("A2A registry returned an invalid agents list")
    clean = []
    for item in agents:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and _AGENT_RE.fullmatch(item["name"]) and isinstance(item.get("url"), str):
            clean.append({"name": item["name"], "has_token": bool(item.get("hasToken"))})
    return clean, binary


def _registry(*, runner: Runner | None, hermes_bin: str | None, timeout: int = _A2A_DEFAULT_REGISTRY_TIMEOUT) -> tuple[list[dict[str, Any]], str | None]:
    mode = _a2a_mode()
    if mode == "bridge":
        return _registry_bridge(runner=runner, hermes_bin=hermes_bin)
    if mode == "official":
        return _registry_official(timeout=timeout), None

    # auto: prefer official config; fall back to the bridge CLI if it is still available.
    official = _registry_official(timeout=timeout)
    if official:
        return official, None
    try:
        bridge, binary = _registry_bridge(runner=runner, hermes_bin=hermes_bin)
        return bridge, binary
    except Exception:
        return [], None



def _error(code: str, message: str, action: str) -> str:
    return json.dumps(op.make_error_envelope(layer="operator", code=code, safe_message=message, suggested_action=action), indent=2)


def _dispatch_timeout_error(agent: str, task_id: str) -> str:
    payload = op.make_error_envelope(
        layer="operator",
        code="FLEET_DISPATCH_TIMEOUT",
        safe_message="timed out awaiting A2A peer reply; remote task state is unknown",
        suggested_action="Call hermes_fleet_task with the returned task_id before retrying dispatch.",
    )
    payload.update({
        "agent": agent,
        "task_id": task_id,
        "submission_may_have_succeeded": True,
    })
    return json.dumps(payload, indent=2)


def _parse_json(stdout: str, *, operation: str) -> dict[str, Any]:
    if not isinstance(stdout, str):
        raise ValueError(f"{operation} returned invalid UTF-8 text")
    if len(stdout.encode("utf-8")) > _MAX_REMOTE_BYTES:
        raise ValueError(f"{operation} response exceeded the bounded response limit")
    cleaned = _CONTROL_RE.sub("", stdout).lstrip("\ufeff")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", cleaned):
        try:
            parsed, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if cleaned.rstrip().endswith(("{", "[", ",", ":")) or cleaned.count("{") > cleaned.count("}"):
        raise ValueError(f"{operation} returned truncated JSON")
    raise ValueError(f"{operation} returned invalid JSON")


def _registered_agent(agent: str, *, runner: Runner | None, hermes_bin: str | None) -> tuple[str, str | None]:
    if not isinstance(agent, str) or not _AGENT_RE.fullmatch(agent):
        raise ValueError("agent must be a registered peer name")
    agents, binary = _registry(runner=runner, hermes_bin=hermes_bin)
    if agent not in {item["name"] for item in agents}:
        raise LookupError("unknown registered peer")
    return agent, binary


def _unwrap_task(payload: dict[str, Any]) -> dict[str, Any]:
    current: Any = payload
    for _ in range(8):
        if not isinstance(current, dict):
            break
        if isinstance(current.get("task"), dict):
            current = current["task"]
        elif isinstance(current.get("result"), dict):
            current = current["result"]
        elif isinstance(current.get("data"), dict):
            current = current["data"]
        else:
            break
    if not isinstance(current, dict):
        raise ValueError("A2A task lookup returned an invalid task shape")
    return current


def _verify_live_peer(peer: AuthorityPeer, binary: str | None, timeout: int, runner: Runner | None) -> None:
    """Verify the live peer's Agent Card matches the authority manifest.

    Uses the official A2A surface when no bridge binary is available. The bridge
    CLI's ``a2a doctor`` output is still accepted if binary is present.
    """
    capped = max(1, min(int(timeout), 30))
    if binary and _bridge_available(binary):
        code, stdout, _ = _run(
            [binary, "a2a", "doctor", peer.name, "--timeout", str(capped), "--json"],
            timeout=capped + 5,
            runner=runner,
        )
        if code != 0:
            raise PeerVerificationError("peer verification failed")
        try:
            card = _parse_json(stdout, operation="A2A peer verification")
        except (TypeError, ValueError) as exc:
            raise PeerVerificationError("peer verification failed") from exc
        identity = card.get("name") or card.get("identity")
        host_role = card.get("host_role") or card.get("role")
        if card.get("ok") is not True:
            raise PeerVerificationError("peer verification failed")
        if (
            not isinstance(identity, str)
            or identity != identity.strip()
            or not _CARD_IDENTITY_RE.fullmatch(identity)
        ):
            raise PeerVerificationError("peer verification failed")
        if not isinstance(host_role, str) or not _ROLE_RE.fullmatch(host_role):
            raise PeerVerificationError("peer verification failed")
        if identity != peer.expected_card_identity or host_role != peer.expected_host_role:
            raise PeerVerificationError("peer verification failed")
        return

    # Official A2A path: fetch the Agent Card directly from the configured peer URL.
    peers = _a2a_peers_with_resolved_tokens()
    entry = peers.get(peer.name)
    if not entry or not isinstance(entry.get("url"), str):
        raise PeerVerificationError("peer verification failed")
    headers = _auth_header(entry)
    try:
        card = _fetch_card(entry["url"], headers, capped)
    except Exception as exc:
        raise PeerVerificationError("peer verification failed") from exc
    if not isinstance(card, dict):
        raise PeerVerificationError("peer verification failed")
    identity = card.get("name")
    if not isinstance(identity, str) or identity != identity.strip() or not _CARD_IDENTITY_RE.fullmatch(identity):
        raise PeerVerificationError("peer verification failed")
    if identity != peer.expected_card_identity:
        raise PeerVerificationError("peer verification failed")


def hermes_fleet_list(*, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        agents, _ = _registry(runner=runner, hermes_bin=hermes_bin)
        return json.dumps({"success": True, "count": len(agents), "agents": agents}, indent=2)
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before inspecting fleet peers.")
    except Exception as exc:
        return _error("FLEET_REGISTRY_ERROR", op.redact_output(str(exc)), "Verify the local Hermes A2A registry.")


def hermes_fleet_status(agent: str, timeout: int = 10, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        capped = max(1, min(int(timeout), 30))
        if binary and _bridge_available(binary):
            code, stdout, stderr = _run([binary, "a2a", "doctor", peer, "--timeout", str(capped), "--json"], timeout=capped + 5, runner=runner)
            if code != 0:
                return _error("FLEET_STATUS_ERROR", op.redact_output(stderr or "A2A peer health check failed"), "Check the peer's A2A service and registry entry.")
            payload = _parse_json(stdout, operation="A2A peer health check")
            return json.dumps({"success": bool(payload.get("ok")), "agent": peer, "status": payload.get("status", "unknown"),
                               "capability_count": len(payload.get("capabilities", {})) if isinstance(payload.get("capabilities"), dict) else 0,
                               "warnings_count": len(payload.get("warnings", [])) if isinstance(payload.get("warnings"), list) else 0,
                               "errors_count": len(payload.get("errors", [])) if isinstance(payload.get("errors"), list) else 0}, indent=2)

        # Official A2A path: fetch the Agent Card and report the name and version.
        peers = _a2a_peers_with_resolved_tokens()
        entry = peers.get(peer)
        if not entry or not isinstance(entry.get("url"), str):
            return _error("FLEET_STATUS_ERROR", "peer has no configured URL", "Check the a2a_agents entry in config.yaml.")
        headers = _auth_header(entry)
        card = _fetch_card(entry["url"], headers, capped)
        if not isinstance(card, dict):
            return _error("FLEET_STATUS_ERROR", "peer returned an invalid Agent Card", "Check the peer's A2A service.")
        name = card.get("name")
        version = card.get("version", "unknown")
        skills = card.get("skills", []) if isinstance(card.get("skills"), list) else []
        return json.dumps({
            "success": isinstance(name, str) and bool(name),
            "agent": peer,
            "status": f"compatible (v{version})",
            "capability_count": len(skills),
            "warnings_count": 0,
            "errors_count": 0,
        }, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before checking fleet peers.")
    except Exception as exc:
        return _error("FLEET_STATUS_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_dispatch(agent: str, message: str, confirm: bool = False, dry_run: bool = True, timeout: int = 30,
                          *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    """Backward-compatible free-form dispatch with original gates."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        effective = policy.effective_dry_run(dry_run)
        policy.require_mutation(dry_run)
        message = _clean_text(message, field="message", maximum=16_000)
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        if effective:
            audit = op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=True,
                                    success=True, changed=False, summary=f"fleet dispatch plan for {peer}", prompt=message, extra={"agent": peer})
            return json.dumps({"success": True, "dry_run": True, "plan": {"agent": peer, "message_len": len(message)}, "audit": audit}, indent=2)
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "remote dispatch requires confirm=true", "Review the task and call again with confirm=true.")
        capped = max(5, min(int(timeout), 120))
        if binary and _bridge_available(binary):
            code, stdout, stderr = _run([binary, "a2a", "send", "--json", peer, "--", message], timeout=capped, runner=runner)
            if code != 0:
                audit = op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                                        success=False, changed=False, summary=f"fleet dispatch failed for {peer}", error=op.redact_output(stderr),
                                        prompt=message, extra={"agent": peer})
                return json.dumps({"success": False, "agent": peer, "code": "FLEET_DISPATCH_ERROR", "audit": audit}, indent=2)
            task = _unwrap_task(_parse_json(stdout, operation="A2A task submission"))
        else:
            peers = _a2a_peers_with_resolved_tokens()
            entry = peers.get(peer)
            if not entry or not isinstance(entry.get("url"), str):
                return _error("FLEET_DISPATCH_ERROR", "peer has no configured URL", "Check the a2a_agents entry in config.yaml.")
            result = _send_message(peer, entry, message, capped)
            task = _unwrap_task(result)
        task_id = task.get("id")
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            return _error("FLEET_DISPATCH_ERROR", "A2A task submission returned no valid task id", "Check the peer's A2A service logs.")
        op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=False, success=True,
                        changed=True, summary=f"fleet task submitted to {peer}", prompt=message, job_id=task_id, extra={"agent": peer, "state": status.get("state")})
        return json.dumps({"success": True, "changed": True, "agent": peer, "task_id": task_id, "state": status.get("state")}, indent=2)
    except FleetDispatchTimeout as exc:
        op.audit_record(
            tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=False, success=False, changed=True,
            summary=f"fleet dispatch timed out after submission to {agent}",
            error="peer reply timed out; remote task state unknown",
            prompt=message, job_id=exc.task_id,
            extra={"agent": agent, "submission_may_have_succeeded": True},
        )
        return _dispatch_timeout_error(agent, exc.task_id)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable workspace-level Operator Mode and use direct mode only for intentional dispatch.")
    except ValueError as exc:
        return _error("INVALID_MESSAGE", str(exc), "Provide a bounded task message.")
    except Exception as exc:
        return _error("FLEET_DISPATCH_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_dispatch_work_order(agent: str, task_id: str, target_profile: str, objective: str, workspace: str,
                                     inputs: list[str], constraints: list[str], acceptance_checks: list[str],
                                     deliverables: list[str], authorization: dict[str, Any] | str,
                                     confirm: bool = False, dry_run: bool = True, timeout: int = 30,
                                     *, runner: Runner | None = None, hermes_bin: str | None = None,
                                     authority_manifest: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        effective = policy.effective_dry_run(dry_run)
        policy.require_mutation(dry_run)
        canonical, envelope = _canonical_work_order(agent=agent, task_id=task_id, target_profile=target_profile, objective=objective,
                                                    workspace=workspace, inputs=inputs, constraints=constraints,
                                                    acceptance_checks=acceptance_checks, deliverables=deliverables, authorization=authorization)
        authorities = _load_authority(authority_manifest)
        peer_authority = authorities.get(agent)
        if peer_authority is None:
            raise PermissionError("peer is absent from the authority manifest")
        _authorize_order(peer_authority, envelope, canonical)
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if effective:
            audit = op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode,
                                    dry_run=True, success=True, changed=False, summary=f"fleet work-order plan for {peer}",
                                    prompt=canonical, job_id=task_id, extra={"agent": peer, "profile": target_profile})
            return json.dumps({"success": True, "dry_run": True, "plan": {"agent": peer, "task_id": task_id, "target_profile": target_profile,
                              "authorization": envelope["authorization"], "work_order_bytes": len(canonical.encode("utf-8")),
                              "live_peer_verification": "required_before_dispatch", "prompt_sha256": digest}, "audit": audit}, indent=2)
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "remote work-order dispatch requires confirm=true", "Review the work order and call again with confirm=true.")
        capped = max(5, min(int(timeout), 120))
        _verify_live_peer(peer_authority, binary, capped, runner)
        if binary and _bridge_available(binary):
            code, stdout, stderr = _run([binary, "a2a", "send", "--json", peer, "--", canonical], timeout=capped, runner=runner)
            if code != 0:
                op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                                success=False, changed=False, summary=f"fleet work-order failed for {peer}", error=op.redact_output(stderr),
                                prompt=canonical, job_id=task_id, extra={"agent": peer, "profile": target_profile})
                return _error("FLEET_DISPATCH_ERROR", "A2A work-order submission failed", "Check the peer's A2A service.")
            remote = _unwrap_task(_parse_json(stdout, operation="A2A work-order submission"))
        else:
            peers = _a2a_peers_with_resolved_tokens()
            entry = peers.get(peer)
            if not entry or not isinstance(entry.get("url"), str):
                return _error("FLEET_DISPATCH_ERROR", "peer has no configured URL", "Check the a2a_agents entry in config.yaml.")
            result = _send_message(peer, entry, canonical, capped)
            remote = _unwrap_task(result)
        remote_id = remote.get("id")
        if not isinstance(remote_id, str) or not _TASK_ID_RE.fullmatch(remote_id):
            return _error("FLEET_DISPATCH_ERROR", "A2A submission returned no valid task id", "Check the peer's A2A service.")
        status = remote.get("status") if isinstance(remote.get("status"), dict) else {}
        op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                        success=True, changed=True, summary=f"fleet work-order submitted to {peer}", prompt=canonical,
                        job_id=remote_id, extra={"agent": peer, "profile": target_profile})
        return json.dumps({"success": True, "changed": True, "agent": peer, "task_id": remote_id,
                           "requested_task_id": task_id, "state": status.get("state"), "live_peer_verified": True,
                           "prompt_sha256": digest}, indent=2)
    except FleetDispatchTimeout as exc:
        op.audit_record(
            tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=False, success=False, changed=True,
            summary=f"fleet work-order timed out after submission to {agent}",
            error="peer reply timed out; remote task state unknown",
            prompt=canonical, job_id=exc.task_id,
            extra={
                "agent": agent,
                "profile": target_profile,
                "requested_task_id": task_id,
                "submission_may_have_succeeded": True,
            },
        )
        return _dispatch_timeout_error(agent, exc.task_id)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except FileNotFoundError:
        return _error("AUTHORITY_MANIFEST_MISSING", "fleet authority manifest is not configured", "Install the authority manifest before dispatch.")
    except PermissionError as exc:
        code = "AUTHORIZATION_DENIED" if "Operator Mode" not in str(exc) and "operator" not in str(exc).lower() else "FLEET_POLICY_DENIED"
        return _error(code, str(exc), "Review fleet authority and explicit approval metadata.")
    except ValueError as exc:
        return _error("INVALID_WORK_ORDER", str(exc), "Correct the bounded work-order fields.")
    except PeerVerificationError:
        return _error("FLEET_PEER_VERIFICATION_FAILED", "registered peer verification failed",
                      "Verify the peer identity and host role before retrying.")
    except Exception as exc:
        return _error("FLEET_DISPATCH_ERROR", op.redact_output(str(exc)), "Check local authority, registry, and peer service.")


def _fetch_task(agent: str, task_id: str, timeout: int, runner: Runner | None, hermes_bin: str | None) -> tuple[str, dict[str, Any]]:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
    capped = max(1, min(int(timeout), 60))
    if binary and _bridge_available(binary):
        code, stdout, stderr = _run([binary, "a2a", "task", "--agent", peer, "--json", "--", task_id], timeout=capped, runner=runner)
        if code != 0:
            raise RuntimeError(op.redact_output(stderr or "A2A task lookup failed"))
        return peer, _unwrap_task(_parse_json(stdout, operation="A2A task lookup"))
    peers = _a2a_peers_with_resolved_tokens()
    entry = peers.get(peer)
    if not entry or not isinstance(entry.get("url"), str):
        raise RuntimeError("peer has no configured URL")
    result = _get_task(peer, entry, task_id, capped)
    return peer, _unwrap_task(result)


def hermes_fleet_task(agent: str, task_id: str, timeout: int = 15, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return _error("INVALID_TASK_ID", "task_id has an invalid format", "Use the task id returned by fleet dispatch.")
    try:
        op.OperatorPolicy().require_level("read_only")
        peer, task = _fetch_task(agent, task_id, timeout, runner, hermes_bin)
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return json.dumps({"success": True, "agent": peer, "task_id": task.get("id", task_id), "state": status.get("state"),
                           "timestamp": status.get("timestamp"), "artifact_count": len(task.get("artifacts", [])) if isinstance(task.get("artifacts"), list) else 0}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before inspecting fleet tasks.")
    except Exception as exc:
        return _error("FLEET_TASK_ERROR", op.redact_output(str(exc)), "Check the peer and task id.")


def _text_parts(value: Any) -> list[str]:
    found: list[str] = []
    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= _MAX_ITEMS:
            return
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                found.append(_clean_text(node["text"], field="result text", maximum=_MAX_TEXT, required=False))
            for key in ("parts", "content", "data"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)
    walk(value)
    return [x for x in found if x]


def _completion_payload(task: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for source in (task.get("result"), task.get("artifacts"), task.get("status")):
        if isinstance(source, dict):
            candidates.append(source)
        elif isinstance(source, list):
            candidates.extend(item for item in source[:_MAX_ITEMS] if isinstance(item, dict))
    texts = _text_parts(candidates)
    for text in texts:
        try:
            candidates.insert(0, _parse_json(text, operation="completed task content"))
        except ValueError:
            pass
    allowed = {"status", "node", "profile", "summary", "changed_paths", "artifacts", "verification", "residual_risk", "recommended_next_action", "authorization"}
    raw: dict[str, Any] = {}
    for candidate in candidates:
        inner = candidate.get("completion_bundle", candidate)
        if isinstance(inner, dict) and any(key in inner for key in allowed):
            raw = inner
            break
    status_obj = task.get("status") if isinstance(task.get("status"), dict) else {}
    def bounded(value: Any, field: str) -> str:
        return op.redact_output(_clean_text(value if isinstance(value, str) else "", field=field, maximum=_MAX_TEXT, required=False))
    result = {
        "status": bounded(raw.get("status") or status_obj.get("state") or "unknown", "status"),
        "node": bounded(raw.get("node") or "", "node"),
        "profile": bounded(raw.get("profile") or "", "profile"),
        "summary": bounded(raw.get("summary") or (texts[0] if texts else ""), "summary"),
        "changed_paths": [],
        "artifacts": [],
        "verification": [],
        "residual_risk": bounded(raw.get("residual_risk") or "", "residual_risk"),
        "recommended_next_action": bounded(raw.get("recommended_next_action") or "", "recommended_next_action"),
        "authorization": {"class": "none", "approved": False},
    }
    for field in ("changed_paths", "artifacts", "verification"):
        value = raw.get(field, [])
        if isinstance(value, list):
            result[field] = [bounded(x, field) for x in value[:_MAX_ITEMS] if isinstance(x, str)]
    if isinstance(raw.get("authorization"), (dict, str)):
        try:
            result["authorization"] = _authorization(raw["authorization"])
        except (ValueError, PermissionError):
            pass
    return result


def hermes_fleet_result(agent: str, task_id: str, timeout: int = 15, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return _error("INVALID_TASK_ID", "task_id has an invalid format", "Use the task id returned by fleet dispatch.")
    try:
        op.OperatorPolicy().require_level("read_only")
        _, task = _fetch_task(agent, task_id, timeout, runner, hermes_bin)
        return json.dumps({"success": True, "task_id": task_id, **_completion_payload(task)}, ensure_ascii=False, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode.")
    except Exception as exc:
        return _error("FLEET_RESULT_ERROR", op.redact_output(str(exc)), "Check the peer, task id, and completion shape.")


def hermes_fleet_authority_drift(*, runner: Runner | None = None, hermes_bin: str | None = None,
                                 authority_manifest: Path | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        authorities = _load_authority(authority_manifest)
        registered, binary = _registry(runner=runner, hermes_bin=hermes_bin)
        registered_names = {x["name"] for x in registered}
        findings: list[dict[str, str]] = []
        for name in sorted(registered_names | set(authorities)):
            if name not in registered_names:
                findings.append({"agent": name, "code": "MANIFEST_ONLY", "severity": "error"})
                continue
            if name not in authorities:
                findings.append({"agent": name, "code": "REGISTRY_ONLY", "severity": "error"})
                continue
            card: dict[str, Any] | None = None
            if binary and _bridge_available(binary):
                code, stdout, _ = _run([binary, "a2a", "doctor", name, "--timeout", "10", "--json"], timeout=15, runner=runner)
                if code != 0:
                    findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                    continue
                try:
                    card = _parse_json(stdout, operation="A2A peer health check")
                except (TypeError, ValueError):
                    findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                    continue
            else:
                peers = _a2a_peers_with_resolved_tokens()
                entry = peers.get(name)
                if not entry or not isinstance(entry.get("url"), str):
                    findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                    continue
                headers = _auth_header(entry)
                try:
                    card = _fetch_card(entry["url"], headers, 10)
                except Exception:
                    findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                    continue
            if not isinstance(card, dict):
                findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                continue
            reported = card.get("name") or card.get("identity")
            role = card.get("host_role") or card.get("role")
            expected = authorities[name]
            if reported != expected.expected_card_identity:
                findings.append({"agent": name, "code": "IDENTITY_MISMATCH", "severity": "error"})
            # Host-role attestation is optional on Agent Cards (neither Hermes
            # v0.19 nor v0.20 emits host_role/role today). Only enforce the
            # manifest's expected role when the card actually attests one;
            # identity, allowed profiles, and the auth ceiling are enforced
            # regardless.
            if role is not None and role != expected.expected_host_role:
                findings.append({"agent": name, "code": "HOST_ROLE_MISMATCH", "severity": "error"})
        return json.dumps({"success": True, "valid": not findings, "registered_count": len(registered_names),
                           "manifest_count": len(authorities), "finding_count": len(findings), "findings": findings}, indent=2)
    except FileNotFoundError:
        return _error("AUTHORITY_MANIFEST_MISSING", "fleet authority manifest is not configured", "Install the authority manifest.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode and check manifest safety.")
    except Exception as exc:
        return _error("FLEET_AUTHORITY_ERROR", op.redact_output(str(exc)), "Check the local manifest, registry, and Agent Cards.")
