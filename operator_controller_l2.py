"""L2 controller execution planning and delegation helpers.

This module owns the action gate, idempotency checks, and dispatch. Placement
scoring and work-contract preparation live in ``operator_controller_l2_placement``.
The controller supplies the no-target callback because it owns the attention
spool.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from operator_controller_l2_placement import (
    REFUSED_SECRET_REQUIREMENT,
    REFUSED_UNSUPPORTED_ACTION,
    _l2_placement_decision,
    _l2_target_binding,
    _l2_work_contract,
)
from operator_controller_store import _sanitize
import operator_delegations as deleg
import operator_placement as placement
import operator_policy as op

CONTROLLER_EXECUTE_ENV = "HERMES_GPT_CONTROLLER_EXECUTE"

# §2.2 supported execution targets: dispatch-style proposals only, sent through
# the EXISTING work-contract / delegation authority surfaces. Everything else
# is either attention-style (needs a human; behaves exactly as today) or
# refused fail-closed as unsupported.
EXECUTABLE_ROW_KEYS = frozenset({"dispatch_ready_child"})
# §2.2 attention-style rows: "needs a human" (park/escalate/observe-only). They
# are never executable at L2 — they behave exactly as today (spool + proposal).
ATTENTION_ROW_KEYS = frozenset(
    {
        "park_authority",
        "park_capability",
        "breaker_exhausted",
        "unknown_fail_closed",
        "fail_closed_evidence",
        "escalate_semantic",
        "signal_awaiting_approval",
        "observe_reconciling",
    }
)

# §2.1/§2.2 stable refusal codes (bounded enums only).
REFUSED_CONFIRM_REQUIRED = "confirm_required"
REFUSED_DRY_RUN = "dry_run"
REFUSED_POLICY = "operator_policy_required"
REFUSED_WORKSPACE = "workspace_required"
REFUSED_ALREADY_EXECUTED = "already_executed"
REFUSED_ATTENTION = "not_executable_attention"
REFUSED_NO_TARGET = "no_capable_target"
REFUSED_NO_ACTION = "no_action"
REFUSED_APPROVAL_GATE = "approval_gate"
REFUSED_AUTH_CLASS = "authorization_class_not_supported"
EXECUTION_RESULTS = ("dispatched", "refused", "failed")
EXECUTION_REFUSAL_CODES = (
    REFUSED_CONFIRM_REQUIRED,
    REFUSED_DRY_RUN,
    REFUSED_POLICY,
    REFUSED_WORKSPACE,
    REFUSED_UNSUPPORTED_ACTION,
    REFUSED_ALREADY_EXECUTED,
    REFUSED_ATTENTION,
    REFUSED_NO_TARGET,
    REFUSED_NO_ACTION,
    REFUSED_APPROVAL_GATE,
    REFUSED_AUTH_CLASS,
    REFUSED_SECRET_REQUIREMENT,
)

# §2.2: at most ONE executed action per pass (smallest first, existing order).
# The durable plan-row execution states; ``intent`` is written BEFORE the
# dispatch call so a crash mid-execution reconciles fail-closed.
EXECUTION_STATE_INTENT = "intent"
EXECUTION_STATE_DISPATCHED = "dispatched"
EXECUTION_STATE_FAILED = "failed"
EXECUTION_PRIOR_STATES = (
    EXECUTION_STATE_INTENT,
    EXECUTION_STATE_DISPATCHED,
    EXECUTION_STATE_FAILED,
)

# §2.3 prohibition guard: the controller never self-authorizes high-impact work
# (it approves nothing); those proposals stay escalation-only.
L2_FORBIDDEN_AUTH_CLASSES = frozenset({"high_impact"})


def _execute_enabled() -> bool:
    """Global machine gate (live read, never cached). Default OFF."""
    return os.environ.get(CONTROLLER_EXECUTE_ENV, "").strip() == "1"


def _execution_block(
    *,
    executed: bool,
    action_kind: str,
    idempotency_key: str,
    result: str,
    refused_reason: str | None,
    placement_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the additive L2-rung execution envelope member (§2.2 shape)."""
    return {
        "enabled": True,
        "executed": bool(executed),
        "action_kind": _sanitize(action_kind, 64),
        "idempotency_key": _sanitize(idempotency_key, 64),
        "result": result,
        "refused_reason": refused_reason,
        "placement": placement_view,
    }


def _refusal(
    action_kind: str, idempotency_key: str, reason: str
) -> dict[str, Any]:
    """A refused execution: no execution writes, no dispatch (§2.1)."""
    return _execution_block(
        executed=False,
        action_kind=action_kind,
        idempotency_key=idempotency_key,
        result="refused",
        refused_reason=reason,
    )


def _live_policy_gate() -> str:
    """§2.1 gates 3+4, re-read live (never cached).

    Returns ``""`` when the gate is satisfied, else the stable refusal code.
    """
    policy = op.OperatorPolicy()
    if not policy.enabled or policy.apply_mode != "direct":
        return REFUSED_POLICY
    if op.level_rank(policy.level) < op.level_rank("workspace"):
        return REFUSED_WORKSPACE
    return ""


# ---------------------------------------------------------------------------
# v0.12 slice-2 (Pack B): L2-rung execution engine — placement-informed dispatch of
# the smallest recovery action through the EXISTING authority surfaces.
# ---------------------------------------------------------------------------


def _l2_prior_execution_block(
    db: sqlite3.Connection, mission_id: str, key: str
) -> dict[str, Any] | None:
    """§2.2 idempotency: the prior execution record for this idempotency key.

    Checked over BOTH durable surfaces: the telemetry execution ledger (a row
    only carries ``executed_idempotency_key`` when an execution was ATTEMPTED —
    refusals never write one, so the ledger stays unambiguous) and the
    ``controller_plan`` decision rows (an ``intent`` row counts as executed: a
    crash between the plan-write and the dispatch must reconcile fail-closed
    with no duplicate dispatch).

    Returns the prior block so the caller can carry it forward verbatim: the
    plan row is REPLACE-keyed by ``(mission_id, node_id, decision_sha256)``, so
    a later pass with the same decision would otherwise erase the only evidence
    that this key was already used.
    """
    if not key:
        return None
    try:
        row = db.execute(
            "SELECT executed_result FROM controller_telemetry "
            "WHERE mission_id=? AND executed_idempotency_key=? LIMIT 1",
            (mission_id, key),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        result = _sanitize(row["executed_result"], 32)
        state = (
            EXECUTION_STATE_DISPATCHED
            if result == "dispatched"
            else EXECUTION_STATE_FAILED
        )
        return {
            "state": state,
            "idempotency_key": key,
            "result": result,
            "source": "telemetry",
        }
    try:
        rows = db.execute(
            "SELECT decision_json FROM controller_plan WHERE mission_id=?",
            (mission_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    for r in rows:
        try:
            doc = json.loads(r["decision_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        ex = doc.get("execution") if isinstance(doc, dict) else None
        if (
            isinstance(ex, dict)
            and ex.get("idempotency_key") == key
            and ex.get("state") in EXECUTION_PRIOR_STATES
        ):
            return ex
    return None






def _l2_dispatch(
    contract_doc: dict[str, Any], mission_id: str, hermes_root: Path | None
) -> tuple[bool, str, str, dict[str, Any]]:
    """Dispatch through the EXISTING delegation authority surface.

    Returns ``(executed, result, refused_reason, linkage)``. No retry loop:
    a failed or ambiguous dispatch is terminal for this pass (fail-closed;
    bounded rework on a new attempt_seq gets a new idempotency key).
    """
    try:
        raw = deleg.hermes_delegation_dispatch(
            json.dumps(contract_doc),
            mission_id=mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
        payload = json.loads(raw)
    except (
        ValueError,
        TypeError,
        LookupError,
        PermissionError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        return False, "failed", _sanitize(type(exc).__name__, 32), {}
    if not isinstance(payload, dict):
        return False, "failed", "invalid_response", {}
    linkage: dict[str, Any] = {}
    deleg_row = payload.get("delegation")
    if isinstance(deleg_row, dict):
        linkage = {
            "delegation_id": _sanitize(deleg_row.get("delegation_id", ""), 64),
            "task_id": _sanitize(deleg_row.get("task_id", ""), 64),
            "state": _sanitize(deleg_row.get("state", ""), 32),
        }
    if payload.get("success") is True and payload.get("changed") is not False:
        return True, "dispatched", "", linkage
    if payload.get("submission_may_have_succeeded"):
        # Ambiguous: the delegation surface records `reconciling`; the next
        # pass classifies observe_reconciling (attention; never re-executed).
        return False, "failed", "ambiguous", linkage
    return (
        False,
        "failed",
        _sanitize(str(payload.get("code", "rejected")), 32),
        linkage,
    )




def plan_execution(
    db: sqlite3.Connection,
    path: Path,
    hermes_root: Path | None,
    *,
    mission_id: str,
    node_id: str,
    row_key: str,
    cmds: list[dict[str, Any]],
    confirm: bool,
    attempt_seq: int,
    pass_seq: int,
    escalate_no_target: Callable[..., None],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """§2/§3: decide L2-rung execution for one pass (no execution writes here).

    Returns ``(execution_block, pending, carry)``:

    - a refusal → ``(block, None, None)``: no dispatch, no execution state;
    - a replay of an already-executed key → ``(block, None, prior_block)`` so
      the caller can carry the prior evidence forward (the plan row is
      REPLACE-keyed and would otherwise forget it);
    - an executable action → ``(None, pending, None)`` where ``pending`` holds
      the prepared contract; the caller persists the intent row, dispatches,
      then records the outcome.

    Gate order (§2.1): per-call ``confirm`` → live policy (enabled + direct +
    workspace) → action kind (§2.2) → idempotency (§2.2) → placement (§3).
    """
    key = _sanitize(cmds[0].get("idempotency_key", ""), 64) if cmds else ""
    if not confirm:
        return _refusal(row_key, key, REFUSED_CONFIRM_REQUIRED), None, None
    gate = _live_policy_gate()
    if gate:
        return _refusal(row_key, key, gate), None, None
    if not key or not cmds:
        # No computed smallest action (terminal/wait rows): nothing to execute.
        return _refusal(row_key, key, REFUSED_NO_ACTION), None, None
    if row_key in ATTENTION_ROW_KEYS:
        # §2.2: "needs a human" proposals are never executable.
        return _refusal(row_key, key, REFUSED_ATTENTION), None, None
    if row_key not in EXECUTABLE_ROW_KEYS:
        # Unknown/new action kinds fail closed — never guess.
        return _refusal(row_key, key, REFUSED_UNSUPPORTED_ACTION), None, None
    prior = _l2_prior_execution_block(db, mission_id, key)
    if prior is not None:
        block = _refusal(row_key, key, REFUSED_ALREADY_EXECUTED)
        block["prior_state"] = _sanitize(str(prior.get("state", "")), 32)
        return block, None, prior
    decision, refusal = _l2_placement_decision(
        db, path, hermes_root, mission_id, node_id
    )
    if decision is None:
        return _refusal(row_key, key, refusal or REFUSED_UNSUPPORTED_ACTION), None, None

    requirement = decision.get("requirement") or {}
    classification = str(decision.get("classification", ""))

    def _view(dispatched: bool, reason: str) -> dict[str, Any]:
        return placement.dispatch_view(
            decision,
            dispatched=dispatched,
            idempotency_key=key,
            refused_reason=reason,
        )

    if str(requirement.get("authorization_class", "")) in L2_FORBIDDEN_AUTH_CLASSES:
        # §2.3: the controller approves nothing — high-impact work keeps its
        # human gate; it is never self-authorized by the rung.
        block = _refusal(row_key, key, REFUSED_AUTH_CLASS)
        block["placement"] = _view(False, REFUSED_AUTH_CLASS)
        return block, None, None
    if classification == placement.CLASS_HUMAN:
        # Approval node: escalate-only (prohibition: approve nothing).
        block = _refusal(row_key, key, REFUSED_APPROVAL_GATE)
        block["placement"] = _view(False, REFUSED_APPROVAL_GATE)
        return block, None, None
    if classification == placement.CLASS_NO_TARGET:
        # §3: escalate through the existing spool; never auto-resolve.
        escalate_no_target(
            mission_id=mission_id,
            node_id=node_id,
            pass_seq=pass_seq,
            hermes_root=hermes_root,
        )
        block = _refusal(row_key, key, REFUSED_NO_TARGET)
        block["placement"] = _view(False, REFUSED_NO_TARGET)
        block["escalated"] = True
        return block, None, None

    agent, profile, refusal = _l2_target_binding(decision, requirement)
    if refusal:
        block = _refusal(row_key, key, refusal)
        block["placement"] = _view(False, refusal)
        return block, None, None

    contract_doc = _l2_work_contract(
        mission_id,
        node_id,
        requirement=requirement,
        target_name=agent,
        idempotency_key=key,
        attempt_seq=attempt_seq,
        hermes_root=hermes_root,
    )
    return (
        None,
        {
            "key": key,
            "target": agent,
            "profile": profile,
            "contract": contract_doc,
            "placement": _view(True, ""),
        },
        None,
    )
