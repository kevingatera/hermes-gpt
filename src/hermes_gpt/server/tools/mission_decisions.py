"""MCP adapters for placement decisions, failure analysis, and mission control."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.missions import controller as op_controller
from hermes_gpt.missions import failure_semantics as op_failure_semantics
from hermes_gpt.missions import placement as op_placement


class MissionDecisionTools:
    """Expose placement and recovery decisions plus the supervised controller."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        tools = (
            self.hermes_placement_score,
            self.hermes_placement_candidates,
            self.hermes_placement_get,
            self.hermes_placement_list,
            self.hermes_failure_classify,
            self.hermes_failure_taxonomy,
            self.hermes_recovery_matrix,
            self.hermes_controller_plan_list,
            self.hermes_controller_reconcile,
            self.hermes_controller_status,
            self.hermes_controller_lease_list,
            self.hermes_controller_trigger,
        )
        for tool in tools:
            server.add_tool(tool, meta=tool_meta())

    # --- Deterministic placement scoring (vNext slice-1, phase 2) -----------------

    def hermes_placement_score(
        self,
        mission_id: str,
        node_id: str,
        source: str = "",
        features: str = "",
        workspace: str = "",
        backends: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Score + record a plan node's placement (deterministic, dry-run-first).

        No assignment executes (``would_assign=False``); the controller proposes,
        actual dispatch goes through the existing contract/fleet/delegation surfaces.
        """
        return op_placement.hermes_placement_score(
            mission_id,
            node_id,
            source=source,
            features=features,
            workspace=workspace,
            backends=backends,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_placement_candidates(
        self,
        profile: str,
        skills: str = "",
        authorization_class: str = "reversible_write",
        features: str = "",
        workspace: str = "",
        backends: str = "",
        source: str = "",
    ) -> str:
        """Read-only probe: candidate targets + per-candidate filter/score."""
        return op_placement.hermes_placement_candidates(
            profile,
            skills=skills,
            authorization_class=authorization_class,
            features=features,
            workspace=workspace,
            backends=backends,
            source=source,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_placement_get(self, mission_id: str, node_id: str) -> str:
        """Read a recorded placement decision (read-only)."""
        return op_placement.hermes_placement_get(
            mission_id, node_id, hermes_root=self.get_hermes_root()
        )

    def hermes_placement_list(self, mission_id: str, limit: int = 50) -> str:
        """List recorded placement decisions for a mission (read-only)."""
        return op_placement.hermes_placement_list(
            mission_id, limit, hermes_root=self.get_hermes_root()
        )

    # --- Semantic failure classification + recovery matrix (vNext slice-1, phase 3)

    def hermes_failure_classify(
        self,
        mission_id: str,
        node_id: str,
        observation_json: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Classify an observation envelope; propose the smallest recovery action.

        Decision output only (§17 item 7): ``would_execute`` is always False; no
        dispatch/reclaim/redispatch/approval path exists. Recording the decision to
        ``controller_plan`` requires workspace + direct + confirm.
        """
        return op_failure_semantics.hermes_failure_classify(
            mission_id,
            node_id,
            observation_json,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_failure_taxonomy(self) -> str:
        """Read-only: the authoritative 8-class failure taxonomy (§11.1)."""
        return op_failure_semantics.hermes_failure_taxonomy()

    def hermes_recovery_matrix(self, row_key: str = "") -> str:
        """Read-only: the deterministic smallest-first recovery matrix (§11.2)."""
        return op_failure_semantics.hermes_recovery_matrix(row_key)

    def hermes_controller_plan_list(self, mission_id: str, limit: int = 50) -> str:
        """Read-only: recorded failure decisions (controller_plan rows)."""
        return op_failure_semantics.hermes_controller_plan_list(
            mission_id, limit, hermes_root=self.get_hermes_root()
        )

    # --- Supervised mission controller — shadow/observe reconciler loop (§17 item 6);

    def hermes_controller_reconcile(
        self,
        mission_id: str,
        trigger_kind: str = "T5_manual",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Run one controller reconciler pass (observe → classify → smallest action).

        L0/L1 (default): the controller observes authoritative
        mission/plan/delegation/runner state, classifies it via the Ops 8-class
        taxonomy, and emits the smallest recovery action — nothing is dispatched.

        ``dry_run=True`` (default) is a truthful preview: no durable writes to any
        mission/plan/delegation/controller state (only the repo-wide Operator
        audit trail every tool call produces).
        ``dry_run=False`` records the pass (controller_plan + controller_telemetry
        + pass lease + heartbeat) and requires workspace level with direct apply
        mode.

        L2 rung (opt-in, v0.12 slice-2): when the machine gate
        ``HERMES_GPT_CONTROLLER_EXECUTE=1`` is set AND ``confirm=True`` here AND the
        live policy is enabled with direct apply mode at workspace level, the pass
        EXECUTES its single smallest action (a dispatch) through the existing
        work-contract/delegation authority surface, keyed idempotently. Every other
        combination stays decision-only with ``would_execute`` False and an additive
        ``execution`` block naming the stable refusal reason. The rung never
        completes, approves, weakens evidence, replans, or retries unboundedly.
        """
        return op_controller.hermes_controller_reconcile(
            mission_id,
            trigger_kind,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_controller_status(self) -> str:
        """Read-only controller health surface (§12.2): liveness, passes, counts."""
        return op_controller.hermes_controller_status(self.get_hermes_root())

    def hermes_controller_lease_list(self, mission_id: str = "") -> str:
        """Read-only: current per-mission pass leases + trigger-queue conflation state."""
        return op_controller.hermes_controller_lease_list(
            mission_id, hermes_root=self.get_hermes_root()
        )

    def hermes_controller_trigger(
        self, mission_id: str, trigger_kind: str, ref: str = ""
    ) -> str:
        """Enqueue a T1–T5 work request for the reconciler (advisory; shadow)."""
        return op_controller.hermes_controller_trigger(
            mission_id, trigger_kind, ref, hermes_root=self.get_hermes_root()
        )

__all__ = ["MissionDecisionTools"]
