"""Validate stage completion and prepare bounded handoffs."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_codex as op_codex
import operator_contract as contract_mod
import operator_policy as op
import operator_swarm_common as common
import operator_swarm_model as swarm_model
import operator_swarm_scheduler as swarm_scheduler
import operator_swarm_store as swarm_store


def default_codex_reviewer(
    *,
    workdir: str,
    target: str,
    instructions: str = "",
    timeout: int = 900,
) -> dict[str, Any]:
    """Drive the existing operator_codex runner unchanged (D-SW7).

    Returns the bounded verdict envelope read from the job's observed state.
    The verdict JSON carries only pass/fail verdict + structured fields —
    never raw transcript or prompt text (risk-review P2-1).
    """
    result = op_codex.hermes_codex_review_start(
        workdir=workdir,
        target=target,
        instructions=instructions,
        timeout=timeout,
        confirm=False,
        dry_run=True,
    )
    if not isinstance(result, dict):
        return {
            "status": "error",
            "verdict": "UNKNOWN",
            "detail": "codex reviewer returned no envelope",
        }
    if not result.get("success", False):
        return {
            "status": "refused",
            "verdict": "UNKNOWN",
            "detail": common.truncate(
                op.redact_output(
                    str(result.get("safe_message") or result.get("error") or "refused")
                ),
                300,
            ),
        }
    # Dry-run plan: expose the fixed argv (prompt redacted) + posture so a
    # caller can confirm before a real run. The engine's advance path uses
    # this as the review evidence; no transcript is ever read.
    argv = result.get("argv") or []
    return {
        "status": "planned",
        "verdict": "PENDING",
        "mode": result.get("mode", "review"),
        "sandbox": result.get("sandbox", "read-only"),
        "timeout": result.get("timeout"),
        "workdir": result.get("workdir"),
        "argv_redacted": [common.truncate(str(a), 200) for a in argv[:12]],
    }


def hermes_swarm_stage_advance(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_root: Path | None = None,
    codex_reviewer: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Validate a stage from observed state; record handoff; promote.

    Runs the M1 validator against **observed** Mission Control state
    (D-SW6). A false "done" (NOT_SATISFIED / INCONCLUSIVE) returns the
    stage for rework — once; a second failure blocks the workflow for a
    human (bounded rework, §5.3). The approval stage is never auto-advanced
    past (D-SW8).
    """
    tool = "hermes_swarm_stage_advance"
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
            suggested_action="Enable workspace-level Operator Mode (direct) before advancing stages.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="advance denied",
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
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="advance not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    st = swarm_store.stage_state(record, stage_id)
    if st is None:
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
            summary="advance stage not found",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if st.get("status") in (
        swarm_model.STAGE_STATUS_DONE,
        swarm_model.STAGE_STATUS_VALIDATED,
    ):
        # Idempotent re-advance (ADR-007): a stage already validated or done
        # returns its current state as a no-op instead of erroring. Restart
        # recovery and retries can therefore safely re-issue an advance.
        payload = {
            "success": True,
            "changed": False,
            "idempotent": True,
            "schema_version": swarm_model.SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_stage_advance",
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage_status": st.get("status"),
            "verdict": st.get("verdict")
            or (
                "SATISFIED" if st.get("status") == swarm_model.STAGE_STATUS_DONE else ""
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=False,
            success=True,
            changed=False,
            owner=st.get("owner", ""),
            verdict=payload["verdict"],
            summary=f"stage {stage_id} already {st.get('status')}; no-op",
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
        stage = {
            "id": stage_id,
            "kind": st.get("kind", "single"),
            "owner": st.get("owner", ""),
            "parents": list(st.get("parents") or []),
            "objective": st.get("objective", ""),
            "expected_artifacts": st.get("expected_artifacts", []),
            "tests": st.get("tests", []),
            "review_requirements": st.get("review_requirements"),
            "completion_criteria": st.get("completion_criteria"),
            "authorization": st.get("authorization"),
            "worktree": st.get("worktree"),
            "forbidden_actions": st.get("forbidden_actions", []),
            "inputs": st.get("inputs", []),
            "constraints": st.get("constraints", []),
        }

    # The approval gate is advanced only by hermes_swarm_approve (D-SW8).
    if stage.get("kind") == "approval":
        payload = common.swarm_error(
            code="APPROVAL_GATE",
            safe_message="the approval stage is advanced only by hermes_swarm_approve.",
            suggested_action="Call hermes_swarm_approve at owner level when ready.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            summary="approval gate requires approve tool",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Codex review stage: drive the existing runner, read the bounded verdict.
    review_evidence: dict[str, Any] | None = None
    if (
        (stage.get("kind") == "codex_review" or stage.get("id") == "codex_review")
        and stage.get("execution") is None
        and stage.get("owner") == "codex"
    ):
        reviewer = codex_reviewer or default_codex_reviewer
        plan = swarm_model.worktree_plan(
            workflow, stage, st.get("task_id") or f"{workflow_id}-{stage_id}"
        )
        # Design §8: target = the merged branch (base:<integration-branch>).
        # The integration branch is the workflow's project branch under NG5
        # (the integration-review stage merges parallel branches there).
        target = "uncommitted"
        project = workflow.get("project") or {}
        if project.get("slug"):
            target = f"base:{project['slug']}/{workflow_id}"
        elif plan and plan.get("branch"):
            target = f"base:{plan['branch']}"
        try:
            review_evidence = reviewer(
                workdir=plan["path"] if plan else workflow["workspace"],
                target=target,
                instructions="",
                timeout=900,
            )
        except Exception as exc:
            review_evidence = {
                "status": "error",
                "verdict": "UNKNOWN",
                "detail": common.truncate(op.redact_output(str(exc)), 300),
            }
        if review_evidence and review_evidence.get("status") == "refused":
            payload = common.swarm_error(
                code="CODEX_REVIEW_REFUSED",
                safe_message=op.redact_output(
                    str(review_evidence.get("detail") or "refused")
                )[:300],
                suggested_action="Check the Codex runner posture (approved workdir, runner enabled).",
                trace_id=tid,
            )
            common.audit_call(
                tool=tool,
                workflow_id=workflow_id,
                stage_id=stage_id,
                dry_run=True,
                success=False,
                changed=False,
                owner=stage["owner"],
                summary="codex review refused",
            )
            return json.dumps(payload, ensure_ascii=False, indent=2)

    # Build the stage contract with the recorded task_id and validate.
    task_id = st.get("task_id") or f"{workflow_id}-{stage_id}"
    try:
        contract = swarm_model.stage_contract(workflow, stage, task_id=task_id)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = common.swarm_error(
            code="INVALID_STAGE_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the stage contract fields and retry.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=False,
            changed=False,
            owner=stage["owner"],
            summary="invalid stage contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Codex review: write the bounded verdict as review evidence so the M1
    # review check sees a distinct reviewer (P2-1: no transcript/prompt).
    if review_evidence is not None:
        verdict = review_evidence.get("verdict") or "UNKNOWN"
        if verdict in ("PASS", "APPROVED", "SATISFIED"):
            _add_review_evidence(
                contract,
                reviewer=stage["owner"],
                verdict="SATISFIED",
                workflow_id=workflow_id,
                stage_id=stage_id,
            )
        elif verdict in ("FAIL", "CHANGES_REQUESTED", "NOT_SATISFIED"):
            _add_review_evidence(
                contract,
                reviewer=stage["owner"],
                verdict="NOT_SATISFIED",
                workflow_id=workflow_id,
                stage_id=stage_id,
            )

    validate_out = contract_mod.hermes_contract_validate(
        json.dumps(contract), runner=runner, hermes_root=root
    )
    verdict_payload: dict[str, Any]
    try:
        parsed = json.loads(validate_out)
        verdict_payload = (
            parsed if isinstance(parsed, dict) else {"verdict": "INVALID_CONTRACT"}
        )
    except json.JSONDecodeError:
        verdict_payload = {"verdict": "INVALID_CONTRACT"}

    verdict = verdict_payload.get("verdict", "INVALID_CONTRACT")
    satisfied = verdict == "SATISFIED"
    false_done = bool(verdict_payload.get("false_done_detected", False))
    rejected = verdict_payload.get("rejected_reasons", [])

    # Worktree freeze: record artifact refs from the validation evidence.
    artifact_refs: list[str] = []
    for check in verdict_payload.get("checks", []):
        if check.get("kind") == "artifacts" and check.get("status") == "PASS":
            evidence = check.get("evidence") or []
            artifact_refs = [
                str(e.get("basename") or e.get("path") or "")
                for e in evidence
                if isinstance(e, dict)
            ][:32]

    # Dry-run: return the plan + validation snapshot, no state change.
    if effective:
        payload = {
            "success": True,
            "schema_version": swarm_model.SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_stage_advance",
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "dry_run": True,
            "plan": {
                "stage": stage_id,
                "owner": stage["owner"],
                "contract_sha256": st.get("contract_sha256")
                or swarm_model.stage_contract_sha(contract),
                "task_id": task_id,
                "verdict_if_advanced": verdict,
                "would_freeze_worktree": bool(st.get("worktree_plan")),
                "handoff_artifact_refs": artifact_refs,
            },
            "validation": {
                "verdict": verdict,
                "false_done_detected": false_done,
                "rejected_reasons": rejected[:5],
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=True,
            success=True,
            changed=False,
            owner=stage["owner"],
            verdict=verdict,
            summary=f"advance plan stage={stage_id} verdict={verdict}",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = common.swarm_error(
            code="CONFIRMATION_REQUIRED",
            safe_message="stage advance requires confirm=true for direct execution.",
            suggested_action="Review the validation verdict and call again with confirm=true, dry_run=false.",
            trace_id=tid,
        )
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=False,
            success=False,
            changed=False,
            owner=stage["owner"],
            verdict=verdict,
            summary="advance confirmation required",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # D-SW6: fail -> bounded rework (once), then blocked for a human.
    if not satisfied:
        st["verdict"] = verdict
        st["rework_count"] = int(st.get("rework_count", 0)) + 1
        if st["rework_count"] >= 2:
            st["status"] = swarm_model.STAGE_STATUS_BLOCKED
            st["blocked_reason"] = (
                f"validation failed twice: {rejected[0] if rejected else verdict}"
            )
            record["status"] = swarm_model.WORKFLOW_STATUS_BLOCKED
            summary = f"stage {stage_id} blocked after second failed validation"
        else:
            st["status"] = swarm_model.STAGE_STATUS_REWORK
            st["blocked_reason"] = ""
            summary = f"stage {stage_id} returned for rework ({st['rework_count']}/1)"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        swarm_store.save_workflow(root, record)
        payload = {
            "success": False,
            "schema_version": swarm_model.SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_stage_advance",
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "changed": True,
            "verdict": verdict,
            "false_done_detected": false_done,
            "rejected_reasons": rejected[:5],
            "stage_status": st["status"],
            "rework_count": st["rework_count"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        common.audit_call(
            tool=tool,
            workflow_id=workflow_id,
            stage_id=stage_id,
            dry_run=False,
            success=False,
            changed=True,
            owner=stage["owner"],
            verdict=verdict,
            summary=summary,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Pass: record verdict, freeze worktree, write handoff, promote.
    parents_done = [p for p in (stage.get("parents") or [])]
    handoff = {
        "from": parents_done,
        "to": stage_id,
        "artifact_refs": artifact_refs,
        "contract_verdict": "SATISFIED",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    st["verdict"] = "SATISFIED"
    st["status"] = swarm_model.STAGE_STATUS_DONE
    st["ended_at"] = datetime.now(timezone.utc).isoformat()
    st["handoffs"] = list(st.get("handoffs") or []) + [handoff]
    record["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Promotion: with an approval stage, the workflow reaches
    # awaiting_approval once every other stage is done; without one, all
    # stages done means the workflow is done. Never auto-advance the
    # approval gate (D-SW8).
    next_ready = swarm_scheduler.next_ready_stages(record, workflow)
    has_approval = any(s.get("kind") == "approval" for s in record.get("stages", []))
    all_non_approval_done = all(
        s.get("status") == swarm_model.STAGE_STATUS_DONE or s.get("kind") == "approval"
        for s in record.get("stages", [])
    )
    if all_non_approval_done and has_approval:
        record["status"] = swarm_model.WORKFLOW_STATUS_AWAITING
    elif all_non_approval_done:
        record["status"] = swarm_model.WORKFLOW_STATUS_DONE
    elif any(
        s.get("status") == swarm_model.STAGE_STATUS_BLOCKED
        for s in record.get("stages", [])
    ):
        record["status"] = swarm_model.WORKFLOW_STATUS_BLOCKED
    else:
        record["status"] = swarm_model.WORKFLOW_STATUS_RUNNING
    swarm_store.save_workflow(root, record)

    payload = {
        "success": True,
        "changed": True,
        "schema_version": swarm_model.SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_stage_advance",
        "workflow_id": workflow_id,
        "stage_id": stage_id,
        "verdict": "SATISFIED",
        "handoff": handoff,
        "workflow_status": record["status"],
        "next_ready_stages": [s["id"] for s in next_ready],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    common.audit_call(
        tool=tool,
        workflow_id=workflow_id,
        stage_id=stage_id,
        dry_run=False,
        success=True,
        changed=True,
        owner=stage["owner"],
        verdict="SATISFIED",
        summary=f"stage {stage_id} satisfied; workflow={record['status']}",
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _add_review_evidence(
    contract: dict[str, Any],
    *,
    reviewer: str,
    verdict: str,
    workflow_id: str,
    stage_id: str,
) -> None:
    """Write an audit acceptance record for a stage contract by a reviewer.

    Used by the Codex review stage so the M1 review check sees evidence from
    a distinct reviewer (the Codex role). Never includes prompt/transcript
    text (P2-1).
    """
    try:
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
    except (ValueError, PermissionError):
        return
    op.audit_record(
        tool="hermes_swarm_stage_advance",
        level="workspace",
        apply_mode="direct",
        dry_run=False,
        success=verdict == "SATISFIED",
        changed=True,
        summary=f"codex review verdict {verdict}",
        profile=reviewer,
        extra={
            "contract_sha256": sha,
            "task_id": contract["task_id"],
            "verdict": verdict,
            "reviewer": reviewer,
            "workflow_id": workflow_id,
            "stage_id": stage_id,
        },
    )
