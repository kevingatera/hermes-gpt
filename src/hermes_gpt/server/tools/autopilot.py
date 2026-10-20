"""MCP tools for gated, durable Mission Autopilot runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.missions import autopilot as op_autopilot


class AutopilotTools:
    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(self, server: Any, *, tool_meta: Callable[[], dict[str, Any]]) -> None:
        for tool in (self.hermes_autopilot_start, self.hermes_autopilot_status, self.hermes_autopilot_stop):
            server.add_tool(tool, meta=tool_meta())

    def hermes_autopilot_start(
        self,
        mission_id: str,
        max_concurrency: int = 3,
        max_replans: int = 2,
        confirm: bool = False,
        dry_run: bool = True,
        max_attempts_per_node: int = 3,
        max_runtime_seconds: int = 86400,
    ) -> str:
        """Place a Mission under durable Autopilot control.

        Dry-run-first; a direct call additionally requires confirm=true and the
        HERMES_GPT_AUTOPILOT=1 machine gate. Idempotent while a non-terminal run
        already exists for the Mission. Limits: max_concurrency (1-16), max_replans
        (0-10), max_attempts_per_node (1-10, first attempt included) and
        max_runtime_seconds (60-604800; new work stops at the limit and the run ends
        once in-flight work has drained). New dispatch also stops whenever the
        Mission's budget envelope is not verifiably within.
        """
        return op_autopilot.hermes_autopilot_start(
            mission_id, max_concurrency=max_concurrency, max_replans=max_replans,
            confirm=confirm, dry_run=dry_run, hermes_root=self.get_hermes_root(),
            max_attempts_per_node=max_attempts_per_node, max_runtime_seconds=max_runtime_seconds,
        )

    def hermes_autopilot_status(self, mission_id: str) -> str:
        """Read-only Autopilot status for a Mission; reconciles worker liveness first."""
        return op_autopilot.hermes_autopilot_status(mission_id, hermes_root=self.get_hermes_root())

    def hermes_autopilot_stop(
        self,
        mission_id: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Request that a Mission's Autopilot worker stop. Always allowed (the safe direction)."""
        return op_autopilot.hermes_autopilot_stop(
            mission_id, confirm=confirm, dry_run=dry_run, hermes_root=self.get_hermes_root(),
        )
