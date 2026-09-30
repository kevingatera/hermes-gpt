"""Record the final Owner-gated Swarm workflow approval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import swarm_common as common
from hermes_gpt.execution import swarm_model
from hermes_gpt.execution import swarm_store


def hermes_swarm_approve(
    workflow_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Record the final human approval (owner level, direct, audited).

    Only valid when the workflow is ``awaiting_approval`` (every non-approval
    stage done). The engine never auto-advances past this gate (D-SW8).
    """
    tool = "hermes_swarm_approve"
    tid = op.new_trace_id()
    root = swarm_store.resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = common.swarm_error(
            code="SWARM_POLICY_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable Owner Mode (direct) before approving a workflow.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="approve denied",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = swarm_store.load_workflow(root, workflow_id)
    if record is None:
        payload = common.swarm_error(
            code="WORKFLOW_NOT_FOUND",
            safe_message=f"workflow {workflow_id!r} not found.",
            suggested_action="Create the workflow first.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="approve not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if record.get("status") != swarm_model.WORKFLOW_STATUS_AWAITING:
        payload = common.swarm_error(
            code="NOT_AWAITING_APPROVAL",
            safe_message=f"workflow is {record.get('status')}; approval requires awaiting_approval.",
            suggested_action="Advance all stages to completion before approving.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=True,
            success=False,
            changed=False,
            summary="approve not awaiting",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if effective:
        payload = {
            "success": True,
            "schema_version": swarm_model.SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_approve",
            "workflow_id": workflow_id,
            "dry_run": True,
            "plan": {
                "workflow_id": workflow_id,
                "title": record["title"],
                "would_record": {
                    "approved": True,
                    "approved_by": "owner",
                    "approval_reference": f"{workflow_id}-approval",
                },
                "workflow_status_after": swarm_model.WORKFLOW_STATUS_DONE,
            },
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
            summary="approve plan",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = common.swarm_error(
            code="CONFIRMATION_REQUIRED",
            safe_message="approve requires confirm=true for direct execution.",
            suggested_action="Review the workflow and call again with confirm=true, dry_run=false.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id="",
            dry_run=False,
            success=False,
            changed=False,
            summary="approve confirmation required",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    now = datetime.now(timezone.utc).isoformat()
    record["approval"] = {
        "approved": True,
        "approved_by": "owner",
        "approval_reference": f"{workflow_id}-approval",
        "approved_at": now,
    }
    record["status"] = swarm_model.WORKFLOW_STATUS_DONE
    record["updated_at"] = now
    for st in record.get("stages", []):
        if st.get("kind") == "approval":
            st["status"] = swarm_model.STAGE_STATUS_DONE
            st["verdict"] = "SATISFIED"
            st["ended_at"] = now
    swarm_store.save_workflow(root, record)

    payload = {
        "success": True,
        "changed": True,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_approve",
        "workflow_id": workflow_id,
        "approval": record["approval"],
        "workflow_status": swarm_model.WORKFLOW_STATUS_DONE,
        "generated_at": now,
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id=workflow_id,
        stage_id="",
        dry_run=False,
        success=True,
        changed=True,
        summary="workflow approved",
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
