"""Validate, create, list, and inspect Swarm workflows."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.execution import contract as contract_mod
from hermes_gpt.missions import common as mission_common
from hermes_gpt.missions import work as mission_work
from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import swarm_common as common
from hermes_gpt.execution import swarm_model
from hermes_gpt.execution import swarm_store


def hermes_swarm_workflow_validate(
    workflow_json: str, hermes_root: Path | None = None
) -> str:
    """Validate a proposed workflow DAG (read-only, pure).

    Checks schema, DAG shape/cycles, owners, caps (parallel group, stage
    count), and that every stage builds a valid M1 contract.
    """
    tool = "hermes_swarm_workflow_validate"
    tid = op.new_trace_id()
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(
            common.swarm_error(
                code="SWARM_POLICY_DENIED",
                safe_message=op.redact_output(str(exc))[:300],
                suggested_action="Enable read-only Operator Mode before validating workflows.",
                trace_id=tid,
            ),
            ensure_ascii=False,
            indent=2,
        )

    try:
        _, workflow, sha = swarm_model.parse_workflow(workflow_json)
    except (ValueError, TypeError, PermissionError) as exc:
        return json.dumps(
            common.swarm_error(
                code="INVALID_WORKFLOW",
                safe_message=op.redact_output(str(exc))[:300],
                suggested_action="Correct the workflow schema and re-validate.",
                trace_id=tid,
            ),
            ensure_ascii=False,
            indent=2,
        )

    # Per-stage M1 contract validation (D-SW1): each stage must canonicalize
    # as a valid contract.
    contract_issues: list[str] = []
    for stage in workflow["stages"]:
        if stage.get("kind") == "approval":
            continue  # the approval gate never runs as a dispatched contract
        try:
            contract = swarm_model.stage_contract(workflow, stage)
            contract_mod._parse_contract(json.dumps(contract))
        except (ValueError, TypeError, PermissionError) as exc:
            contract_issues.append(f"{stage['id']}: {op.redact_output(str(exc))[:200]}")

    payload = {
        "success": not contract_issues,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_validate",
        "workflow_id": workflow["workflow_id"] or "<generated>",
        "workflow_sha256": sha,
        "valid": not contract_issues,
        "title": workflow["title"],
        "stage_count": len(workflow["stages"]),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "stages": [
            {
                "id": s["id"],
                "kind": s.get("kind", "single"),
                "owner": s["owner"],
                "parents": s.get("parents") or [],
                "objective": common.prompt_meta(s.get("objective")),
            }
            for s in workflow["stages"]
        ],
        "contract_issues": contract_issues[:10],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id=workflow["workflow_id"] or "<generated>",
        stage_id="",
        dry_run=True,
        success=payload["valid"],
        changed=False,
        summary=f"validate workflow stages={payload['stage_count']} valid={payload['valid']}",
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def hermes_swarm_workflow_create(
    workflow_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Register a workflow instance (workspace level, dry-run-first).

    Returns ``workflow_id`` + the stage plan. Direct execution requires
    workspace + direct apply mode + ``confirm=true``.
    """
    tool = "hermes_swarm_workflow_create"
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
            suggested_action="Enable workspace-level Operator Mode (direct) before creating workflows.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id="",
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="create denied",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        _, workflow, sha = swarm_model.parse_workflow(workflow_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = common.swarm_error(
            code="INVALID_WORKFLOW",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the workflow schema and re-create.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id="",
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid workflow",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not workflow["workflow_id"]:
        workflow["workflow_id"] = f"sw-{uuid.uuid4().hex[:12]}"
    workflow_id = workflow["workflow_id"]

    if swarm_store.load_workflow(root, workflow_id) is not None:
        payload = common.swarm_error(
            code="WORKFLOW_ALREADY_EXISTS",
            safe_message=f"workflow {workflow_id!r} already exists.",
            suggested_action="Pick a new workflow_id or inspect the existing workflow.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="duplicate workflow_id",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    plan = {
        "workflow_id": workflow_id,
        "title": workflow["title"],
        "stage_count": len(workflow["stages"]),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "stages": [
            {
                "id": s["id"],
                "kind": s.get("kind", "single"),
                "owner": s["owner"],
                "parents": s.get("parents") or [],
                "worktree": bool(
                    isinstance(s.get("worktree"), dict) and s["worktree"].get("enabled")
                ),
            }
            for s in workflow["stages"]
        ],
    }

    if effective:
        payload = {
            "success": True,
            "schema_version": swarm_model.SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_workflow_create",
            "workflow_id": workflow_id,
            "workflow_sha256": sha,
            "dry_run": True,
            "plan": plan,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=True,
            changed=False,
            summary=f"workflow create plan stages={len(workflow['stages'])}",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = common.swarm_error(
            code="CONFIRMATION_REQUIRED",
            safe_message="workflow create requires confirm=true for direct execution.",
            suggested_action="Review the plan and call again with confirm=true, dry_run=false.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=False,
            success=False,
            changed=False,
            summary="create confirmation required",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = swarm_store.new_record(workflow, root)
    swarm_store.save_workflow(root, record)
    payload = {
        "success": True,
        "changed": True,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_create",
        "workflow_id": workflow_id,
        "workflow_sha256": sha,
        "dry_run": False,
        "plan": plan,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id=workflow_id,
        stage_id="",
        dry_run=False,
        success=True,
        changed=True,
        summary=f"workflow created stages={len(workflow['stages'])}",
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _surface_workflow_summary(
    record: dict[str, Any], hermes_root: Path
) -> dict[str, Any]:
    stages = record.get("stages", [])
    stages_done = sum(
        1 for s in stages if s.get("status") == swarm_model.STAGE_STATUS_DONE
    )
    parallel_active = sum(
        1 for s in stages if s.get("status") == swarm_model.STAGE_STATUS_RUNNING
    )
    current_owners = sorted(
        {
            s.get("owner", "")
            for s in stages
            if s.get("status")
            in (
                swarm_model.STAGE_STATUS_TODO,
                swarm_model.STAGE_STATUS_RUNNING,
                swarm_model.STAGE_STATUS_REWORK,
            )
        }
    )
    awaiting: list[str] = []
    if record.get("status") == swarm_model.WORKFLOW_STATUS_AWAITING:
        awaiting.append("human approval")
    elif record.get("status") == swarm_model.WORKFLOW_STATUS_BLOCKED:
        awaiting.append("human (blocked)")
    return {
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "status": record.get("status", swarm_model.WORKFLOW_STATUS_RUNNING),
        "stage_count": len(stages),
        "stages_done": stages_done,
        "parallel_active": parallel_active,
        "current_owners": current_owners[:8],
        "awaiting": awaiting,
        "approval": record.get("approval", {}).get("approved", False),
    }


def hermes_swarm_workflow_list(hermes_root: Path | None = None) -> str:
    """List workflow instances + status (read-only)."""
    tool = "hermes_swarm_workflow_list"
    tid = op.new_trace_id()
    root = swarm_store.resolve_root(hermes_root)
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(
            common.swarm_error(
                code="SWARM_POLICY_DENIED",
                safe_message=op.redact_output(str(exc))[:300],
                suggested_action="Enable read-only Operator Mode before listing workflows.",
                trace_id=tid,
            ),
            ensure_ascii=False,
            indent=2,
        )

    records = swarm_store.list_records(root)
    workflows = [_surface_workflow_summary(rec, root) for rec in records]
    payload = {
        "success": True,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_list",
        "count": len(workflows),
        "workflows": workflows[:50],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id="",
        stage_id="",
        dry_run=True,
        success=True,
        changed=False,
        summary=f"list workflows count={len(workflows)}",
    )
    return mission_common._bounded_json(payload)


def hermes_swarm_workflow_status(
    workflow_id: str, hermes_root: Path | None = None
) -> str:
    """One workflow's stage map, owners, handoffs, verdicts (read-only)."""
    tool = "hermes_swarm_workflow_status"
    tid = op.new_trace_id()
    root = swarm_store.resolve_root(hermes_root)
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(
            common.swarm_error(
                code="SWARM_POLICY_DENIED",
                safe_message=op.redact_output(str(exc))[:300],
                suggested_action="Enable read-only Operator Mode before reading workflow status.",
                trace_id=tid,
            ),
            ensure_ascii=False,
            indent=2,
        )

    record = swarm_store.load_workflow(root, workflow_id)
    if record is None:
        payload = common.swarm_error(
            code="WORKFLOW_NOT_FOUND",
            safe_message=f"workflow {workflow_id!r} not found.",
            suggested_action="Call hermes_swarm_workflow_list to see registered workflows.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="status not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Observed kanban runs per stage task_id (bounded, redacted).
    observed_by_task: dict[str, list[dict[str, Any]]] = {}
    try:
        warnings: list[str] = []
        runs = mission_work._kanban_runs_for(root, warnings)
        for r in runs:
            observed_by_task.setdefault(str(r.get("task_id") or ""), []).append(
                {
                    "status": r.get("status"),
                    "outcome": r.get("outcome"),
                    "board": r.get("board"),
                }
            )
    except Exception:
        pass

    stages: list[dict[str, Any]] = []
    for st in record.get("stages", []):
        # Expected contract task_id resolves even before dispatch so status
        # can link a stage to its observed kanban run (M0 read surface).
        expected_task = st.get("task_id") or f"{record['workflow_id']}-{st['id']}"
        stages.append(
            {
                "id": st["id"],
                "kind": st.get("kind", "single"),
                "owner": st["owner"],
                "parents": st.get("parents") or [],
                "status": st.get("status", swarm_model.STAGE_STATUS_TODO),
                "task_id": st.get("task_id", ""),
                "contract_sha256": st.get("contract_sha256", ""),
                "verdict": st.get("verdict", ""),
                "rework_count": st.get("rework_count", 0),
                "worktree": st.get("worktree_plan"),
                "observed": observed_by_task.get(expected_task, [])[:10],
                "handoffs": (st.get("handoffs") or [])[: swarm_model._MAX_HANDOFFS],
            }
        )

    payload = {
        "success": True,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_status",
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "status": record.get("status", swarm_model.WORKFLOW_STATUS_RUNNING),
        "approval": record.get("approval", {}),
        "retention_note": record.get("retention_note", swarm_model.RETENTION_NOTE),
        "stages": stages,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id=workflow_id,
        stage_id="",
        dry_run=True,
        success=True,
        changed=False,
        summary=f"status stages={len(stages)} status={record.get('status')}",
    )
    return mission_common._bounded_json(payload)
