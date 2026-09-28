"""MCP adapters for work contracts, runners, delegations, reviews, and swarms."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import operator_contract as op_contract
import operator_delegations as op_delegations
import operator_review as op_review
import operator_runners as op_runners
import operator_swarm as op_swarm


class WorkTools:
    """Keep work lifecycle adapters out of the main server module."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def hermes_contract_define(self, contract_json: str) -> str:
        return op_contract.hermes_contract_define(
            contract_json=contract_json,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_contract_dispatch(
        self,
        contract_json: str,
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = 30,
    ) -> str:
        return op_contract.hermes_contract_dispatch(
            contract_json=contract_json,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_contract_validate(self, contract_json: str) -> str:
        return op_contract.hermes_contract_validate(
            contract_json=contract_json,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_contract_status(self, contract_json: str) -> str:
        return op_contract.hermes_contract_status(
            contract_json=contract_json,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_runner_list(self) -> str:
        return op_runners.hermes_runner_list(hermes_root=self.get_hermes_root())

    def hermes_runner_status(self, task_id: str) -> str:
        return op_runners.hermes_runner_status(
            task_id=task_id,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_runner_cancel(
        self,
        task_id: str,
        backend: str = "",
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_runners.hermes_runner_cancel(
            task_id=task_id,
            backend=backend,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_delegation_dispatch(
        self,
        contract_json: str,
        mission_id: str = "",
        delegation_id: str = "",
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = 30,
    ) -> str:
        return op_delegations.hermes_delegation_dispatch(
            contract_json=contract_json,
            mission_id=mission_id,
            delegation_id=delegation_id,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_delegation_get(self, delegation_id: str) -> str:
        return op_delegations.hermes_delegation_get(
            delegation_id=delegation_id,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_delegation_list(
        self,
        mission_id: str = "",
        state: str = "",
        limit: int = 50,
    ) -> str:
        return op_delegations.hermes_delegation_list(
            mission_id=mission_id,
            state=state,
            limit=limit,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_delegation_reconcile(
        self,
        delegation_id: str,
        contract_json: str = "",
        apply: bool = False,
    ) -> str:
        return op_delegations.hermes_delegation_reconcile(
            delegation_id=delegation_id,
            contract_json=contract_json,
            apply=apply,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_delegation_cancel(
        self,
        delegation_id: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_delegations.hermes_delegation_cancel(
            delegation_id=delegation_id,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_review_accept(
        self,
        contract_sha256: str,
        task_id: str,
        assignee: str,
        reviewer: str,
        verdict: str,
        evidence_refs: list[str] | None = None,
        approval_reference: str = "",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> str:
        """Write bounded review evidence; evidence itself is referenced, not copied."""
        return op_review.hermes_review_accept(
            contract_sha256=contract_sha256,
            task_id=task_id,
            assignee=assignee,
            reviewer=reviewer,
            verdict=verdict,
            evidence_refs=evidence_refs,
            approval_reference=approval_reference,
            dry_run=dry_run,
            confirm=confirm,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_workflow_create(
        self,
        workflow_json: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_swarm.hermes_swarm_workflow_create(
            workflow_json=workflow_json,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_workflow_list(self) -> str:
        return op_swarm.hermes_swarm_workflow_list(hermes_root=self.get_hermes_root())

    def hermes_swarm_workflow_status(self, workflow_id: str) -> str:
        return op_swarm.hermes_swarm_workflow_status(
            workflow_id=workflow_id,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_workflow_validate(self, workflow_json: str) -> str:
        return op_swarm.hermes_swarm_workflow_validate(
            workflow_json=workflow_json,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_stage_dispatch(
        self,
        workflow_id: str,
        stage_id: str,
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = 30,
    ) -> str:
        return op_swarm.hermes_swarm_stage_dispatch(
            workflow_id=workflow_id,
            stage_id=stage_id,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_stage_advance(
        self,
        workflow_id: str,
        stage_id: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_swarm.hermes_swarm_stage_advance(
            workflow_id=workflow_id,
            stage_id=stage_id,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_swarm_approve(
        self,
        workflow_id: str,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_swarm.hermes_swarm_approve(
            workflow_id=workflow_id,
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )
