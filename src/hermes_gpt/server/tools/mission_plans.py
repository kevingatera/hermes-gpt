"""MCP adapters for MissionPlan and mission budget tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.missions import budget as op_mission_budget
from hermes_gpt.missions import plan as op_mission_plan


class MissionPlanTools:
    """Handle MissionPlan and budget review operations."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        tools = (
            self.hermes_plan_create,
            self.hermes_plan_get,
            self.hermes_plan_list,
            self.hermes_plan_validate,
            self.hermes_plan_decompose,
            self.hermes_plan_review,
            self.hermes_plan_node_transition,
            self.hermes_plan_set_status,
            self.hermes_budget_set,
            self.hermes_budget_get,
            self.hermes_budget_check,
            self.hermes_budget_record,
        )
        for tool in tools:
            server.add_tool(tool, meta=tool_meta())

    # --- MissionPlan (decomposition DAG, additive; read-only re missions) ------

    def hermes_plan_create(
        self,
        mission_id: str,
        plan_json: str = "",
        confirm: bool = False,
        dry_run: bool = True,
        status: str = "draft",
    ) -> str:
        """Create (or replace-version) a MissionPlan for a mission (additive, read-only re mission)."""
        return op_mission_plan.hermes_plan_create(
            mission_id,
            plan_json,
            confirm=confirm,
            dry_run=dry_run,
            status=status,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_plan_get(self, mission_id: str) -> str:
        """Read the MissionPlan + plan_nodes for a mission (read-only)."""
        return op_mission_plan.hermes_plan_get(mission_id, self.get_hermes_root())

    def hermes_plan_list(self, status: str = "", limit: int = 50) -> str:
        """List MissionPlans (read-only)."""
        return op_mission_plan.hermes_plan_list(status, limit, self.get_hermes_root())

    def hermes_plan_validate(self, plan_json: str) -> str:
        """Pure read-only validation of a MissionPlan document."""
        return op_mission_plan.hermes_plan_validate(plan_json, self.get_hermes_root())

    def hermes_plan_decompose(self, mission_id: str) -> str:
        """Deterministically decompose a MissionSpec into a bounded plan DAG (read-only)."""
        return op_mission_plan.hermes_plan_decompose(mission_id, self.get_hermes_root())

    def hermes_plan_review(self, mission_id: str) -> str:
        """Operator review surface: the bounded DAG + node state (read-only)."""
        return op_mission_plan.hermes_plan_review(mission_id, self.get_hermes_root())

    def hermes_plan_node_transition(
        self,
        mission_id: str,
        node_id: str,
        target_state: str,
        reason: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Advance a plan node via the validated state machine (read-only re mission)."""
        return op_mission_plan.hermes_plan_node_transition(
            mission_id,
            node_id,
            target_state,
            reason,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_plan_set_status(
        self,
        mission_id: str,
        status: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Set the plan-level review status (operator-reviewable, read-only re mission)."""
        return op_mission_plan.hermes_plan_set_status(
            mission_id,
            status,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    # --- Mission budget envelope (spend envelope + budget_check; dry-run) -------

    def hermes_budget_set(
        self,
        mission_id: str,
        quota: float,
        policy_json: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Create (or update) the mission-scoped spend envelope (budget_accounts)."""
        return op_mission_budget.hermes_budget_set(
            mission_id,
            quota,
            policy_json,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_budget_get(self, mission_id: str) -> str:
        """Read the mission spend envelope (read-only)."""
        return op_mission_budget.hermes_budget_get(mission_id, self.get_hermes_root())

    def hermes_budget_check(self, mission_id: str) -> str:
        """Evaluate the mission spend envelope (read-only `budget_check` surface)."""
        return op_mission_budget.hermes_budget_check(mission_id, self.get_hermes_root())

    def hermes_budget_record(
        self,
        mission_id: str,
        amount: float,
        ref: str = "",
        reason: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Record a spend increment against a mission envelope (dry-run enforcement)."""
        return op_mission_budget.hermes_budget_record(
            mission_id,
            amount,
            ref,
            reason,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )


__all__ = ["MissionPlanTools"]
