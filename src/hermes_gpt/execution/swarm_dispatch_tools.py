"""Dispatch ready Swarm stages as Work Contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.execution import contract as contract_mod
from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import swarm_common as common
from hermes_gpt.execution import swarm_model
from hermes_gpt.execution import swarm_scheduler
from hermes_gpt.execution import swarm_store


def hermes_swarm_stage_dispatch(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_bin: str | None = None,
    authority_manifest: Path | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Dispatch one ready stage as an M1 contract (workspace, dry-run-first).

    Reuses ``hermes_contract_dispatch`` (fleet authority, live peer
    verification, dry-run/confirm gates, audit). Respects the per-workflow
    and per-board concurrency caps (D-SW3) and only dispatches stages whose
    parents are done (fan-in).
    """
    tool = "hermes_swarm_stage_dispatch"
    tid = op.new_trace_id()
    root = swarm_store.resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = common.swarm_error(
            code="SWARM_POLICY_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable workspace-level Operator Mode (direct) before dispatching stages.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="dispatch denied",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = swarm_store.load_workflow(root, workflow_id)
    if record is None:
        payload = common.swarm_error(
            code="WORKFLOW_NOT_FOUND",
            safe_message=f"workflow {workflow_id!r} not found.",
            suggested_action="Create the workflow first with hermes_swarm_workflow_create.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="dispatch not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if record.get("status") in (
        swarm_model.WORKFLOW_STATUS_DONE,
        swarm_model.WORKFLOW_STATUS_BLOCKED,
    ):
        payload = common.swarm_error(
            code="WORKFLOW_FINISHED",
            safe_message=f"workflow is {record.get('status')}; no stages can dispatch.",
            suggested_action="Inspect the workflow status before dispatching.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="dispatch finished workflow",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    workflow = {
        "schema": swarm_model.WORKFLOW_SCHEMA,
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "workspace": record["workspace"],
        "project": record.get("project", {}),
        "max_parallel": record["max_parallel"],
        "board_cap": record["board_cap"],
        "max_stages": record["max_stages"],
        "stages": swarm_store.workflow_stages_for_dispatch(record),
    }
    stage = next((s for s in workflow["stages"] if s["id"] == stage_id), None)
    if stage is None:
        payload = common.swarm_error(
            code="STAGE_NOT_FOUND",
            safe_message=f"stage {stage_id!r} not found in workflow {workflow_id!r}.",
            suggested_action="Check the stage id with hermes_swarm_workflow_status.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="dispatch stage not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if stage.get("kind") == "approval":
        payload = common.swarm_error(
            code="APPROVAL_GATE",
            safe_message="the approval stage is never dispatched; it waits on hermes_swarm_approve.",
            suggested_action="Advance prior stages; the workflow will surface awaiting_approval.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="approval gate not dispatched",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    ready, reason = swarm_scheduler.stage_ready(record, stage)
    if not ready:
        payload = common.swarm_error(
            code="STAGE_NOT_READY",
            safe_message=f"stage {stage_id!r} is not ready: {reason}",
            suggested_action="Complete parent stages before dispatching this one.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="stage not ready",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Bounds (D-SW3): workflow cap + global board cap.
    max_parallel, board_cap, _ = swarm_model.workflow_caps(workflow)
    workflow_running = swarm_scheduler.running_count(record)
    board_running = swarm_scheduler.board_running_count(root)
    if workflow_running >= max_parallel:
        payload = common.swarm_error(
            code="WORKFLOW_CAP_REACHED",
            safe_message=f"workflow parallelism cap reached ({workflow_running}/{max_parallel}).",
            suggested_action="Advance a running stage before dispatching more.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="workflow cap reached",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if board_running >= board_cap:
        payload = common.swarm_error(
            code="BOARD_CAP_REACHED",
            safe_message=f"global board concurrency cap reached ({board_running}/{board_cap}).",
            suggested_action="Wait for running stages on the board to complete.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="board cap reached",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Build the stage contract (fresh task_id per attempt for rework).
    stage_state = swarm_store.stage_state(record, stage_id) or {}
    attempt = int(stage_state.get("rework_count", 0))
    task_id = (
        f"{workflow_id}-{stage_id}"
        if attempt == 0
        else f"{workflow_id}-{stage_id}-r{attempt}"
    )
    try:
        contract = swarm_model.stage_contract(workflow, stage, task_id=task_id)
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
    except (ValueError, TypeError, PermissionError) as exc:
        payload = common.swarm_error(
            code="INVALID_STAGE_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the stage contract fields and re-dispatch.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid stage contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    plan = swarm_model.worktree_plan(workflow, stage, task_id)
    dispatch_result = contract_mod.hermes_contract_dispatch(
        json.dumps(contract),
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        runner=runner,
        hermes_bin=hermes_bin,
        authority_manifest=authority_manifest,
        hermes_root=root,
    )
    result_payload: dict[str, Any]
    try:
        parsed = json.loads(dispatch_result)
        result_payload = parsed if isinstance(parsed, dict) else {"success": False}
    except json.JSONDecodeError:
        result_payload = {"success": False}

    success = bool(result_payload.get("success", False))
    changed = bool(result_payload.get("changed", False))

    # Record the stage state transition on a real dispatch.
    st = swarm_store.stage_state(record, stage_id)
    if st is not None and (not effective or changed):
        st["task_id"] = task_id
        st["contract_sha256"] = sha
        st["worktree_plan"] = plan
        if changed and not effective:
            st["status"] = swarm_model.STAGE_STATUS_RUNNING
            st["started_at"] = datetime.now(timezone.utc).isoformat()
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        swarm_store.save_workflow(root, record)

    result_payload["tool"] = tool
    result_payload["workflow_id"] = workflow_id
    result_payload["stage_id"] = stage_id
    result_payload["task_id"] = task_id
    result_payload["contract_sha256"] = sha
    if plan:
        result_payload["worktree_plan"] = plan

    common.audit_call(
        tool=tool,
        workflow_id=workflow_id,
        stage_id=stage_id,
        dry_run=effective or bool(result_payload.get("dry_run", True)),
        success=success,
        changed=changed,
        owner=stage["owner"],
        summary=f"stage dispatch plan task={task_id}"
        if not changed
        else f"stage dispatched task={task_id}",
    )
    return json.dumps(result_payload, ensure_ascii=False, indent=2)
