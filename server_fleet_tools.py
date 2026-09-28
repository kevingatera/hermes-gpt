"""Register MCP adapters for authenticated A2A fleet peers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import operator_fleet


class FleetTools:
    """Keep fleet-facing MCP handlers and registration beside each other."""

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        for tool in (
            self.hermes_fleet_list,
            self.hermes_fleet_status,
            self.hermes_fleet_dispatch,
            self.hermes_fleet_dispatch_work_order,
            self.hermes_fleet_task,
            self.hermes_fleet_result,
            self.hermes_fleet_authority_drift,
        ):
            server.add_tool(tool, meta=tool_meta())

    def hermes_fleet_list(self) -> str:
        """List registered A2A peers without exposing credentials."""
        return operator_fleet.hermes_fleet_list()

    def hermes_fleet_status(self, agent: str, timeout: int = 10) -> str:
        """Read metadata-only compatibility status for one peer."""
        return operator_fleet.hermes_fleet_status(agent=agent, timeout=timeout)

    def hermes_fleet_dispatch(
        self,
        agent: str,
        message: str,
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = 30,
    ) -> str:
        """Dispatch a confirmed bounded task to one registered A2A peer."""
        return operator_fleet.hermes_fleet_dispatch(
            agent=agent,
            message=message,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
        )

    def hermes_fleet_task(self, agent: str, task_id: str, timeout: int = 15) -> str:
        """Read a safe status summary for a peer task."""
        return operator_fleet.hermes_fleet_task(
            agent=agent,
            task_id=task_id,
            timeout=timeout,
        )

    def hermes_fleet_dispatch_work_order(
        self,
        agent: str,
        task_id: str,
        target_profile: str,
        objective: str,
        workspace: str,
        inputs: list[str],
        constraints: list[str],
        acceptance_checks: list[str],
        deliverables: list[str],
        authorization: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = 30,
    ) -> str:
        """Dispatch a canonical work order to an authorized peer profile."""
        return operator_fleet.hermes_fleet_dispatch_work_order(
            agent=agent,
            task_id=task_id,
            target_profile=target_profile,
            objective=objective,
            workspace=workspace,
            inputs=inputs,
            constraints=constraints,
            acceptance_checks=acceptance_checks,
            deliverables=deliverables,
            authorization=authorization,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
        )

    def hermes_fleet_result(self, agent: str, task_id: str, timeout: int = 15) -> str:
        """Read a schema-filtered completion bundle from a peer task."""
        return operator_fleet.hermes_fleet_result(
            agent=agent,
            task_id=task_id,
            timeout=timeout,
        )

    def hermes_fleet_authority_drift(self) -> str:
        """Compare registered peers with configured authority and agent cards."""
        return operator_fleet.hermes_fleet_authority_drift()
