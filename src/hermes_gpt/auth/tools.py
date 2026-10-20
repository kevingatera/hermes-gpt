"""Trusted-client OAuth tool surfaces for hermes-gpt v0.7 (Flight Deck, S5).

- ``hermes_oauth_status``  — read_only: durable token store presence/expiry only;
  never exposes token material.
- ``hermes_oauth_revoke`` requires owner + direct + confirm to retire durable
  tokens and optionally rotate the key.
  Every call is audited.

The token store owns transactional writes. These tools expose status and
policy-gated revocation without returning credentials.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.auth import oauth as oauth_auth
from hermes_gpt.policy import authorization as op


def _default_hermes_root() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def hermes_oauth_status(hermes_root: Path | None = None) -> str:
    """Read-only durable token store status (presence/expiry only)."""
    from hermes_gpt.auth import token_store

    tool = "hermes_oauth_status"
    tid = op.new_trace_id()
    root = hermes_root or _default_hermes_root()
    try:
        status = token_store.status(root)
        success = True
        summary = f"oauth token store {status.get('presence')}"
    except Exception as exc:
        status = {"available": False, "presence": "error", "error": op.redact_output(str(exc))[:200]}
        success = False
        summary = "oauth token store status failed"
    op.audit_record(
        tool=tool,
        level=op.OperatorPolicy().level or "read_only",
        apply_mode=op.OperatorPolicy().apply_mode,
        dry_run=True,
        success=success,
        changed=False,
        summary=summary,
        extra={"presence": status.get("presence", ""), "client_count": status.get("client_count", 0)},
    )
    return json.dumps(
        {
            "success": success,
            "schema_version": "0.7-oauth.1",
            "tool": tool,
            "surface": "oauth_status",
            "trace_id": tid,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "store": status,
        },
        ensure_ascii=False,
        indent=2,
    )


def hermes_oauth_revoke(
    confirm: bool = False,
    dry_run: bool = True,
    rotate_key: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Revoke durable OAuth tokens with owner + direct + confirm.

    Retires durable token rows; optionally rotates the master key.
    Never exposes token material. Audit is written for every call.
    """
    from hermes_gpt.auth import token_store

    tool = "hermes_oauth_revoke"
    tid = op.new_trace_id()
    root = hermes_root or _default_hermes_root()
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="OAUTH_REVOKE_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable Owner Mode (owner level, owner active, owner ack) with direct apply before revoking tokens.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level="owner", apply_mode="dry_run", dry_run=True, success=False,
            changed=False, summary="oauth revoke denied", error=str(exc)[:300],
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if effective:
        # Keep the historical plan key for clients; SQLite revocation now
        # retires token rows rather than deleting the database file.
        payload = {
            "success": True,
            "schema_version": "0.7-oauth.1",
            "tool": tool,
            "surface": "oauth_revoke",
            "dry_run": True,
            "plan": {"would_delete_envelope": True, "would_rotate_key": rotate_key},
            "trace_id": tid,
        }
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=True,
            success=True, changed=False, summary="oauth revoke dry-run plan",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = op.make_error_envelope(
            layer="operator",
            code="CONFIRMATION_REQUIRED",
            safe_message="oauth revoke requires confirm=true for direct execution.",
            suggested_action="Review the plan and call again with confirm=true, dry_run=false.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
            success=False, changed=False, summary="oauth revoke confirmation required",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        result = token_store.revoke_tokens(root, rotate_key=rotate_key)
        # Notify the live OAuth state (if installed) that the durable envelope
        # was revoked, so in-memory caches are dropped and a later issuance
        # cannot re-persist pre-revocation tokens.
        oauth_auth.run_revocation_hook()
    except Exception as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="OAUTH_REVOKE_ERROR",
            safe_message=f"could not revoke tokens: {op.redact_output(str(exc))[:200]}",
            suggested_action="Check secrets directory permissions and retry.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
            success=False, changed=False, summary="oauth revoke failed", error=str(exc)[:300],
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    op.audit_record(
        tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
        success=True, changed=bool(result.get("revoked") or result.get("key_rotated")),
        summary="oauth tokens revoked",
        extra={"revoked": result.get("revoked", False), "key_rotated": result.get("key_rotated", False)},
    )
    return json.dumps(
        {
            "success": True,
            "changed": bool(result.get("revoked") or result.get("key_rotated")),
            "schema_version": "0.7-oauth.1",
            "tool": tool,
            "surface": "oauth_revoke",
            "result": result,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        },
        ensure_ascii=False,
        indent=2,
    )
