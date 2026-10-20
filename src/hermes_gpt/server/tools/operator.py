"""MCP adapters for operator status, event history, and token controls."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from hermes_gpt.missions import capability_manifest as op_capability_manifest
from hermes_gpt.workspace import diagnostics as op_diagnostics
from hermes_gpt.workspace import events as op_events
from hermes_gpt.workspace import live_events as op_live_events
from hermes_gpt.missions import ledger as op_mission_ledger
from hermes_gpt.auth import tools as op_oauth
from hermes_gpt.policy import authorization as op_policy
from hermes_gpt.workspace import recovery as op_recovery
from hermes_gpt.workspace import status as op_status


class OperatorTools:
    """Keep operator-facing MCP handlers out of the server entry point."""

    def __init__(
        self,
        *,
        get_hermes_root: Callable[[], Path | None],
        get_agent_root: Callable[[], Path | None],
        get_project_root: Callable[[], Path],
        get_active_profile: Callable[[], str],
    ) -> None:
        self.get_hermes_root = get_hermes_root
        self.get_agent_root = get_agent_root
        self.get_project_root = get_project_root
        self.get_active_profile = get_active_profile

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        """Register operator tools with their read-only or mutation hints."""
        for tool in (
            self.hermes_operator_policy,
            self.hermes_operator_status,
            self.hermes_operator_audit_tail,
            self.hermes_operator_doctor,
            self.hermes_operator_snapshot,
            self.hermes_release_doctor,
            self.hermes_operator_recover,
        ):
            server.add_tool(tool, meta=tool_meta())

        server.add_tool(
            self.hermes_swarm_reconcile,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="Reconcile state after a restart (dry-run by default; apply requires workspace + direct)"
            ),
        )

        for tool, title in (
            (
                self.hermes_events_query,
                "Query the normalized Hermes GPT event timeline",
            ),
            (
                self.hermes_events_tail,
                "Tail recent Hermes GPT events across allowed sources",
            ),
            (self.hermes_live_events_cursor, "Read the durable v0.9 live-event cursor"),
            (
                self.hermes_live_events_since,
                "Read or wait for durable v0.9 live events",
            ),
            (
                self.hermes_capability_manifest,
                "Query the derived capability manifest (read-only)",
            ),
            (
                self.hermes_mission_ledger,
                "Query the merged, replayable per-mission ledger (read-only)",
            ),
            (
                self.hermes_mission_ledger_replay,
                "Replay a mission's full ledger event history (read-only)",
            ),
            (self.hermes_oauth_status, "Durable OAuth token store status"),
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=title, readOnlyHint=True),
            )

        server.add_tool(
            self.hermes_oauth_revoke,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="Revoke durable OAuth tokens",
                destructiveHint=True,
            ),
        )

    def hermes_operator_policy(self) -> str:
        """Return the current operator policy summary without secrets."""
        try:
            summary = op_policy.OperatorPolicy().to_summary()
            summary["success"] = True
            return json.dumps(summary, indent=2)
        except Exception as exc:  # noqa: BLE001 - return a safe MCP error envelope.
            return json.dumps(
                op_policy.error_from_exception(
                    exc,
                    layer="operator",
                    code="POLICY_SUMMARY_ERROR",
                    suggested_action="Check operator environment variables.",
                ),
                indent=2,
            )

    def hermes_operator_status(self) -> str:
        """Return runtime status without secrets."""
        try:
            default_root = self.get_hermes_root()
            agent_root = self.get_agent_root()
            return op_status.build_operator_status(
                project_path=str(self.get_project_root()),
                agent_root=str(agent_root) if agent_root else None,
                default_root=str(default_root) if default_root else None,
                active_profile=self.get_active_profile(),
            )
        except Exception as exc:  # noqa: BLE001 - return a safe MCP error envelope.
            return json.dumps(
                op_policy.error_from_exception(
                    exc,
                    layer="operator",
                    code="OPERATOR_STATUS_ERROR",
                    suggested_action="Check HERMES_HOME and operator environment variables.",
                ),
                indent=2,
            )

    def hermes_operator_audit_tail(self, limit: int = 20) -> str:
        """Return bounded recent audit records."""
        try:
            records = op_policy.audit_tail(limit=limit)
            return json.dumps(
                {"success": True, "count": len(records), "records": records},
                indent=2,
            )
        except Exception as exc:  # noqa: BLE001 - return a safe MCP error envelope.
            return json.dumps(
                op_policy.error_from_exception(
                    exc,
                    layer="audit",
                    code="AUDIT_TAIL_ERROR",
                    suggested_action="Check audit log path and permissions.",
                ),
                indent=2,
            )

    def hermes_operator_doctor(self, profile: str = "default") -> str:
        """Run a read-only health check across operator surfaces."""
        return op_diagnostics.hermes_operator_doctor(
            profile=profile, hermes_root=self.get_hermes_root()
        )

    def hermes_operator_snapshot(self, profile: str = "default") -> str:
        """Return one current-state summary of the operator."""
        return op_diagnostics.hermes_operator_snapshot(
            profile=profile, hermes_root=self.get_hermes_root()
        )

    def hermes_release_doctor(
        self, workdir: str | None = None, full_tests: bool = False, timeout: int = 180
    ) -> str:
        """Check whether the repo and operator are safe to ship."""
        return op_diagnostics.hermes_release_doctor(
            workdir=workdir, full_tests=full_tests, timeout=timeout
        )

    def hermes_operator_recover(
        self, profile: str = "default", apply: bool = False
    ) -> str:
        """Run the conservative recovery sequence; dry-run by default."""
        return op_diagnostics.hermes_operator_recover(
            profile=profile, apply=apply, hermes_root=self.get_hermes_root()
        )

    def hermes_swarm_reconcile(self, apply: bool = False) -> str:
        """Reconcile interrupted swarm state without auto-advancing stages."""
        return op_recovery.hermes_operator_reconcile(
            apply=apply, hermes_root=self.get_hermes_root()
        )

    def hermes_events_query(
        self,
        source: str = "",
        subject_id: str = "",
        kind: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
    ) -> str:
        """Query the normalized, redacted, bounded event timeline."""
        return op_events.hermes_events_query(
            source=source,
            subject_id=subject_id,
            kind=kind,
            since=since,
            until=until,
            limit=limit,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_events_tail(self, limit: int = 20) -> str:
        """Read recent events across allowed sources."""
        return op_events.hermes_events_tail(
            limit=limit, hermes_root=self.get_hermes_root()
        )

    def hermes_capability_manifest(
        self, source: str = "", include_cache: bool = True, limit: int = 100
    ) -> str:
        """Query the derived capability manifest."""
        return op_capability_manifest.hermes_capability_manifest(
            source=source,
            include_cache=include_cache,
            limit=limit,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_mission_ledger(
        self,
        mission_id: str,
        source: str = "",
        cursor: int | str = 0,
        limit: int = 100,
        replay: bool = False,
    ) -> str:
        """Read a page from the merged, replayable mission ledger."""
        return op_mission_ledger.hermes_mission_ledger(
            mission_id=mission_id,
            source=source,
            cursor=cursor,
            limit=limit,
            replay=replay,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_mission_ledger_replay(self, mission_id: str, limit: int = 500) -> str:
        """Replay a mission's full event history."""
        return op_mission_ledger.hermes_mission_ledger_replay(
            mission_id=mission_id,
            limit=limit,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_live_events_cursor(self) -> str:
        """Return the durable live-event high-water cursor."""
        return op_live_events.hermes_live_events_cursor(
            hermes_root=self.get_hermes_root()
        )

    def hermes_live_events_since(
        self,
        cursor: int = 0,
        mission_id: str = "",
        topic: str = "",
        kind: str = "",
        limit: int = 100,
        wait_ms: int = 0,
    ) -> str:
        """Read or wait for durable events after a cursor."""
        return op_live_events.hermes_live_events_since(
            cursor=cursor,
            mission_id=mission_id,
            topic=topic,
            kind=kind,
            limit=limit,
            wait_ms=wait_ms,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_oauth_status(self) -> str:
        """Report durable token presence and expiry without values."""
        return op_oauth.hermes_oauth_status(hermes_root=self.get_hermes_root())

    def hermes_oauth_revoke(
        self, confirm: bool = False, dry_run: bool = True, rotate_key: bool = True
    ) -> str:
        """Revoke durable OAuth tokens through the operator policy gates."""
        return op_oauth.hermes_oauth_revoke(
            confirm=confirm,
            dry_run=dry_run,
            rotate_key=rotate_key,
            hermes_root=self.get_hermes_root(),
        )


__all__ = ["OperatorTools"]
