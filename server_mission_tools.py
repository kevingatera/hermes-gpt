"""MCP adapters for mission, plan, and controller operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import operator_controller as op_controller
import operator_failure_semantics as op_failure_semantics
import operator_mission as op_mission
import operator_mission_budget as op_mission_budget
import operator_mission_plan as op_mission_plan
import operator_mission_runtime as op_mission_runtime
import operator_placement as op_placement


class MissionTools:
    """Keep mission-facing MCP adapters out of the main server module."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

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
        return op_mission_plan.hermes_plan_validate(plan_json)

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
