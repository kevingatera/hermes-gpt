"""MCP adapters for Mission Control and mission lifecycle tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import operator_mission as op_mission
import operator_mission_runtime as op_mission_runtime


class MissionTools:
    """Expose read-only Mission Control views and durable mission lifecycle actions."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        tools = (
            self.hermes_mission_overview,
            self.hermes_mission_health,
            self.hermes_mission_profiles,
            self.hermes_mission_fleet,
            self.hermes_mission_codex,
            self.hermes_mission_cron,
            self.hermes_mission_delegations,
            self.hermes_mission_failures,
            self.hermes_mission_approvals,
            self.hermes_mission_vault,
            self.hermes_mission_usage,
            self.hermes_mission_audit,
            self.hermes_mission_create,
            self.hermes_mission_get,
            self.hermes_mission_list,
            self.hermes_mission_update,
            self.hermes_mission_attach,
            self.hermes_mission_reconcile,
            self.hermes_mission_transition,
            self.hermes_mission_approve,
        )
        for tool in tools:
            server.add_tool(tool, meta=tool_meta())

    # --- Mission Control (v0.6 M0, read-only) --------------------------------

    def hermes_mission_overview(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_overview_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_health(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_health_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_cron(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_cron_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_fleet(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_fleet_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_audit(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_audit_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_profiles(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_profiles_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_delegations(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_delegations_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_failures(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_failures_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_approvals(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_approvals_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_codex(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_codex_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_vault(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_vault_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    def hermes_mission_usage(self, force_refresh: bool = False) -> str:
        return op_mission.hermes_mission_usage_tool(
            hermes_root=self.get_hermes_root(), force_refresh=force_refresh
        )

    # --- First-class Mission runtime (v0.9) -----------------------------------

    def hermes_mission_create(
        self, mission_json: str, confirm: bool = False, dry_run: bool = True
    ) -> str:
        return op_mission_runtime.hermes_mission_create(
            mission_json, confirm, dry_run, self.get_hermes_root()
        )

    def hermes_mission_get(self, mission_id: str) -> str:
        return op_mission_runtime.hermes_mission_get(mission_id, self.get_hermes_root())

    def hermes_mission_list(self, status: str = "", limit: int = 50) -> str:
        return op_mission_runtime.hermes_mission_list(
            status, limit, self.get_hermes_root()
        )

    def hermes_mission_update(
        self,
        mission_id: str,
        patch_json: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_mission_runtime.hermes_mission_update(
            mission_id, patch_json, confirm, dry_run, self.get_hermes_root()
        )

    def hermes_mission_attach(
        self,
        mission_id: str,
        kind: str,
        ref: str,
        relationship: str = "contains",
        state: str = "unknown",
        evidence_ref: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_mission_runtime.hermes_mission_attach(
            mission_id,
            kind,
            ref,
            relationship,
            state,
            evidence_ref,
            confirm,
            dry_run,
            self.get_hermes_root(),
        )

    def hermes_mission_reconcile(
        self, mission_id: str, confirm: bool = False, dry_run: bool = True
    ) -> str:
        return op_mission_runtime.hermes_mission_reconcile(
            mission_id, confirm, dry_run, self.get_hermes_root()
        )

    def hermes_mission_transition(
        self,
        mission_id: str,
        status: str,
        reason: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_mission_runtime.hermes_mission_transition(
            mission_id, status, reason, confirm, dry_run, self.get_hermes_root()
        )

    def hermes_mission_approve(
        self,
        mission_id: str,
        approval_reference: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_mission_runtime.hermes_mission_approve(
            mission_id, approval_reference, confirm, dry_run, self.get_hermes_root()
        )


__all__ = ["MissionTools"]
