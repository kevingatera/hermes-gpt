"""Outcome / Work Contracts for hermes-gpt v0.6 (M1).

Implements the approved Work Contracts design (v0.6 M1,
feat/m1-work-contracts): structured work orders whose objective, assigned
agent/profile, allowed scope, forbidden actions, expected artifacts, tests, review
requirements, and completion criteria are declared up front, and whose completion is
verified against **observed** Mission Control state (run/outcome/artifacts) rather
than a worker's claim (success criterion S2, risk R4).

The surface is the ``hermes_contract_*`` tool group:

- ``hermes_contract_define``   — read-only, pure: validate + canonicalize a contract
  document (``hermes.work-contract/v1``) and return its canonical form + sha256.
- ``hermes_contract_dispatch`` — workspace level, dry-run-first: submit a contract as
  a fleet work order (reuses ``operator_fleet`` authority, live peer verification,
  dry-run/confirm gates).
- ``hermes_contract_validate`` — read-only by default; may run allowlisted tests when
  operator policy grants workspace + direct. Returns a deterministic verdict
  (``SATISFIED`` / ``NOT_SATISFIED`` / ``INCONCLUSIVE`` / ``INVALID_CONTRACT``) against
  observed Mission Control state, never the worker's self-report.
- ``hermes_contract_status``   — read-only: link a contract to its observed run state.

Design invariants enforced here (D1-D11, §9):

- Observed-only evidence (D4): the validator reads kanban ``task_runs``, async
  delegations, on-disk artifacts, and the audit trail. A worker-supplied
  ``result``/``completion_bundle`` is never accepted as proof.
- Fail-closed verdicts (D7): any required check that cannot be positively verified
  yields ``NOT_SATISFIED`` or ``INCONCLUSIVE``; a missing observed run is
  ``INCONCLUSIVE``, never ``SATISFIED``.
- Tests only through the workspace allowlist (D6): ``hermes_workspace_run_test``,
  ``shell=False``, bounded timeout, workdir under allowed paths; test execution
  individually gated at workspace + direct.
- Forbidden actions are detected, not prevented (D5/NG-WC4): the validator scans the
  audit trail (and artifact set) for forbidden-action signals.
- Redaction by class (D8): objective appears only as ``{prompt_len, prompt_sha256}``;
  forbidden-action labels are class-level; artifact paths are basenames on surfaces;
  validation evidence is summary-based.
- Every contract call is audited (D9) with ``contract_sha256`` + ``task_id``.
- No new persistent store (D10): contracts are declarative JSON documents; the
  validator reads existing Mission Control sources.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_gpt.execution import contract_checks as _contract_checks
from hermes_gpt.execution import contract_observations as _contract_observations
from hermes_gpt.execution import contract_schema as _contract_schema
from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import runners as op_runners
from hermes_gpt.skills import resolution as skill_resolution

# Preserve the established operator_contract import surface while the
# implementation stays grouped by schema, evidence, and completion checks.
SCHEMA_VERSION = _contract_schema.SCHEMA_VERSION
CONTRACT_SCHEMA = _contract_schema.CONTRACT_SCHEMA
_AGENT_RE = _contract_schema._AGENT_RE
_PROFILE_RE = _contract_schema._PROFILE_RE
_TASK_ID_RE = _contract_schema._TASK_ID_RE
_FORBIDDEN_CLASSES = _contract_schema._FORBIDDEN_CLASSES
_AUTH_CLASSES = _contract_schema._AUTH_CLASSES
_MAX_OBJECTIVE_BYTES = _contract_schema._MAX_OBJECTIVE_BYTES
_MAX_ARTIFACTS = _contract_schema._MAX_ARTIFACTS
_MAX_TESTS = _contract_schema._MAX_TESTS
_MAX_FORBIDDEN_ACTIONS = _contract_schema._MAX_FORBIDDEN_ACTIONS
_MAX_SCOPE_WORKSPACES = _contract_schema._MAX_SCOPE_WORKSPACES
_MAX_SCOPE_PROFILES = _contract_schema._MAX_SCOPE_PROFILES
_MAX_CAPABILITY_SKILLS = _contract_schema._MAX_CAPABILITY_SKILLS
_MAX_SKILL_NAME = _contract_schema._MAX_SKILL_NAME
_MAX_REVIEW_EVIDENCE_SCAN = _contract_schema._MAX_REVIEW_EVIDENCE_SCAN
_VERDICT_SATISFIED = _contract_schema._VERDICT_SATISFIED
_VERDICT_NOT_SATISFIED = _contract_schema._VERDICT_NOT_SATISFIED
_VERDICT_INCONCLUSIVE = _contract_schema._VERDICT_INCONCLUSIVE
_VERDICT_INVALID = _contract_schema._VERDICT_INVALID
_CHECK_KINDS = _contract_schema._CHECK_KINDS
VALIDATION_MANIFEST_SCHEMA = _contract_schema.VALIDATION_MANIFEST_SCHEMA
_default_hermes_root = _contract_schema._default_hermes_root
_resolve_root = _contract_schema._resolve_root
_contract_error = _contract_schema._contract_error
_prompt_meta = _contract_schema._prompt_meta
_truncate = _contract_schema._truncate
_audit_call = _contract_schema._audit_call
_clean_text = _contract_schema._clean_text
_string_list = _contract_schema._string_list
_workspace_list = _contract_schema._workspace_list
_profile_list = _contract_schema._profile_list
_capability_requirement = _contract_schema._capability_requirement
_forbidden_list = _contract_schema._forbidden_list
_resolve_artifact_paths = _contract_schema._resolve_artifact_paths
_artifact_list = _contract_schema._artifact_list
_test_list = _contract_schema._test_list
_review_requirements = _contract_schema._review_requirements
_completion_criteria = _contract_schema._completion_criteria
_canonical_contract = _contract_schema._canonical_contract
_contract_sha256 = _contract_schema._contract_sha256
_parse_contract = _contract_schema._parse_contract
_validation_manifest = _contract_schema._validation_manifest
_contract_from_validation_manifest = _contract_schema._contract_from_validation_manifest
_surface_contract = _contract_observations._surface_contract
_observed_kanban_runs = _contract_observations._observed_kanban_runs
_observed_delegations = _contract_observations._observed_delegations
_observed_runs = _contract_observations._observed_runs
_observed_audit = _contract_observations._observed_audit
_check_run_state = _contract_checks._check_run_state
_admitted_artifact_evidence = _contract_checks._admitted_artifact_evidence
_check_artifacts = _contract_checks._check_artifacts
_check_tests = _contract_checks._check_tests
_assignee_identity = _contract_checks._assignee_identity
_check_review = _contract_checks._check_review
_attributable_identities = _contract_checks._attributable_identities
_auto_fabric_lineage = _contract_checks._auto_fabric_lineage
_check_forbidden = _contract_checks._check_forbidden
_check_authorization = _contract_checks._check_authorization


def _validate_manifest_impl(
    manifest: dict[str, Any],
    runner: Callable[..., tuple[int, str, str]] | None,
    hermes_root: Path,
) -> dict[str, Any]:
    contract, sha = _contract_from_validation_manifest(manifest)
    return _validate_impl(contract, sha, runner, hermes_root)


def _validate_impl(
    contract: dict[str, Any],
    sha: str,
    runner: Callable[..., tuple[int, str, str]] | None,
    hermes_root: Path,
) -> dict[str, Any]:
    """Run the six checks and aggregate a deterministic verdict (D7)."""
    checks: list[dict[str, Any]] = [
        _check_run_state(contract, hermes_root),
        _check_artifacts(contract, sha, hermes_root),
        _check_tests(contract, runner, hermes_root),
        _check_review(contract, sha, hermes_root),
        _check_forbidden(contract, hermes_root, sha),
        _check_authorization(contract),
    ]

    required = {
        "run_state": True,
        "artifacts": contract["completion_criteria"]["artifacts_present"],
        "tests": contract["completion_criteria"]["tests_pass"],
        "review": contract["review_requirements"]["required"],
        "forbidden": contract["completion_criteria"]["no_forbidden_actions"],
        "authorization": True,
    }

    failed = [c for c in checks if c["status"] == "FAIL" and required[c["kind"]]]
    unverified = [
        c for c in checks if c["status"] == "UNVERIFIED" and required[c["kind"]]
    ]

    rejected: list[str] = []
    false_done = False
    if failed:
        verdict = _VERDICT_NOT_SATISFIED
        false_done = True
        rejected = [f"{c['kind']} FAIL: {c['detail']}" for c in failed]
    elif unverified:
        # Fail-closed: required checks that cannot be positively verified never pass.
        run_unverified = any(c["kind"] == "run_state" for c in unverified)
        other_unverified = [c for c in unverified if c["kind"] != "run_state"]
        if run_unverified and not other_unverified:
            # Valid contract + no observed run at all -> INCONCLUSIVE (D7 §7.3).
            verdict = _VERDICT_INCONCLUSIVE
            rejected = [
                "no observed run/outcome for "
                f"{contract['task_id']}; cannot confirm completion (fail-closed)"
            ]
            false_done = True
        else:
            verdict = _VERDICT_NOT_SATISFIED
            false_done = True
            rejected = [f"{c['kind']} UNVERIFIED: {c['detail']}" for c in unverified]
    else:
        verdict = _VERDICT_SATISFIED
        false_done = False

    evidence: dict[str, Any] = {
        "run": _observed_runs(contract["task_id"], hermes_root)[:5],
        "artifacts": [],
        "audit_count": len(_observed_audit(hermes_root)),
    }
    for c in checks:
        if c.get("evidence"):
            evidence["artifacts"].extend(
                c["evidence"] if isinstance(c["evidence"], list) else [c["evidence"]]
            )
    evidence["artifacts"] = evidence["artifacts"][:10]

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_contract_validate",
        "surface": "contract_validate",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "verdict": verdict,
        "satisfied": verdict == _VERDICT_SATISFIED,
        "checks": [
            {
                "kind": c["kind"],
                "status": c["status"],
                "detail": c.get("detail", ""),
            }
            for c in checks
        ],
        "false_done_detected": false_done,
        "rejected_reasons": rejected,
        "evidence": evidence,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": op.new_trace_id(),
    }


def hermes_contract_define(contract_json: str, hermes_root: Path | None = None) -> str:
    """Validate + canonicalize a contract document (read-only, pure).

    Returns the canonical contract (redacted per D8) + ``contract_sha256``.
    """
    tool = "hermes_contract_define"
    tid = op.new_trace_id()
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except PermissionError as exc:
        payload = _contract_error(
            code="CONTRACT_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the denied path / authorization and re-define.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="define denied",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and re-define.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    result = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "contract_define",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "contract": _surface_contract(contract),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(
        tool=tool,
        dry_run=True,
        success=True,
        changed=False,
        summary=f"defined contract task={contract['task_id']}",
        contract_sha256=sha,
        task_id=contract["task_id"],
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


def hermes_contract_dispatch(
    contract_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_bin: str | None = None,
    authority_manifest: Path | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Submit a contract as a fleet work order (workspace level, dry-run-first).

    Reuses ``operator_fleet.hermes_fleet_dispatch_work_order`` for authority,
    live peer verification, dry-run/confirm gates, and audit.
    """
    tool = "hermes_contract_dispatch"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
    except PermissionError as exc:
        payload = _contract_error(
            code="FLEET_POLICY_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable workspace-level Operator Mode before dispatching contracts.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="dispatch denied",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and re-dispatch.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    task_id = contract["task_id"]
    capability_req = contract.get("capability_req")
    if isinstance(capability_req, dict):
        try:
            skill_resolution.require_required_skills(
                capability_req["profile"], capability_req.get("skills", []), root
            )
        except skill_resolution.SkillRequirementsError as exc:
            payload = _contract_error(
                code="SKILL_REQUIREMENTS_REJECTED",
                safe_message=str(exc),
                suggested_action=(
                    "Install the required skills in the assigned Hermes profile "
                    "before dispatching the contract."
                ),
                trace_id=tid,
                extra={"skill_validation": exc.rejection},
            )
            _audit_call(
                tool=tool,
                dry_run=dry_run,
                success=False,
                changed=False,
                summary="skill requirements rejected",
                task_id=task_id,
            )
            return json.dumps(payload, ensure_ascii=False, indent=2)

    # Uniqueness invariant (design §6.3): task_id must be unique for the dispatch.
    if _observed_runs(task_id, root):
        payload = _contract_error(
            code="CONTRACT_ALREADY_DISPATCHED",
            safe_message=f"task_id {task_id!r} already has an observed run; use a unique task_id.",
            suggested_action="Pick a new task_id for this contract.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="duplicate task_id",
            contract_sha256=sha,
            task_id=task_id,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Dispatch through the selected execution backend. Contracts without an
    # explicit selector continue to use the legacy fleet path.
    payload = op_runners.dispatch_contract(
        contract,
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        hermes_root=root,
        runner=runner,
        hermes_bin=hermes_bin,
        authority_manifest=authority_manifest,
    )

    # Augment the backend envelope with the contract identity; keep shape bounded.
    payload["contract_sha256"] = sha
    payload["contract_task_id"] = task_id
    _audit_call(
        tool=tool,
        dry_run=bool(payload.get("dry_run", True)),
        success=bool(payload.get("success", False)),
        changed=bool(payload.get("changed", False)),
        summary=(
            f"contract dispatch plan task={task_id}"
            if payload.get("dry_run")
            else f"contract dispatched task={task_id}"
        ),
        contract_sha256=sha,
        task_id=task_id,
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def hermes_contract_validate(
    contract_json: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Return a deterministic verdict against **observed** Mission Control state.

    The worker's own ``result``/``completion_bundle`` is never accepted as proof
    (D4). Test execution is individually gated at workspace + direct (D6).
    """
    tool = "hermes_contract_validate"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "contract_validate",
            "task_id": "",
            "contract_sha256": "",
            "verdict": _VERDICT_INVALID,
            "satisfied": False,
            "checks": [],
            "false_done_detected": False,
            "rejected_reasons": [
                f"INVALID_CONTRACT: {op.redact_output(str(exc))[:300]}"
            ],
            "evidence": {},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    payload = _validate_impl(contract, sha, runner, root)
    _audit_call(
        tool=tool,
        dry_run=True,
        success=payload["satisfied"],
        changed=False,
        summary=f"validate task={contract['task_id']} verdict={payload['verdict']}",
        contract_sha256=sha,
        task_id=contract["task_id"],
        extra={"verdict": payload["verdict"]},
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def hermes_contract_status(contract_json: str, hermes_root: Path | None = None) -> str:
    """Link a contract to its observed run/delegation state (read-only)."""
    tool = "hermes_contract_status"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and retry.",
            trace_id=tid,
        )
        _audit_call(
            tool=tool,
            dry_run=True,
            success=False,
            changed=False,
            summary="invalid contract",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    kanban = _observed_kanban_runs(contract["task_id"], root)
    delegations = _observed_delegations(contract["task_id"], root)
    payload = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "contract_status",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "observed": {
            "kanban_runs": kanban[:10],
            "delegations": delegations[:10],
        },
        "summary": (
            f"{len(kanban)} kanban run(s), {len(delegations)} delegation(s) observed "
            f"for task_id {contract['task_id']}"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(
        tool=tool,
        dry_run=True,
        success=True,
        changed=False,
        summary=f"status task={contract['task_id']} runs={len(kanban)} delegations={len(delegations)}",
        contract_sha256=sha,
        task_id=contract["task_id"],
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
