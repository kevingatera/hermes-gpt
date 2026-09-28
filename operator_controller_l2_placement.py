"""Placement scoring and work-contract preparation for controller L2."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from operator_controller_store import _root, _sanitize
import operator_contract as contract_mod
import operator_placement as placement

REFUSED_UNSUPPORTED_ACTION = "unsupported_action"
REFUSED_SECRET_REQUIREMENT = "secret_like_requirement"


def _l2_placement_decision(
    db: sqlite3.Connection,
    path: Path,
    hermes_root: Path | None,
    mission_id: str,
    node_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """§3 placement-informed dispatch: score the node requirement.

    Consults the same scoring core ``hermes_placement_score`` uses (dry-run
    path: hard filters + soft scores, no scoring/filter/classification change)
    so the top candidate becomes the dispatch target. Returns
    ``(decision_or_None, refusal_code)``; fail-closed: an invalid or
    secret-like requirement refuses execution.
    """
    try:
        base = placement._read_node_requirement(db, mission_id, node_id)
        node_def = placement._node_def(path, mission_id, node_id)
        base["kind"] = node_def["kind"]
        base["owner"] = node_def["owner"]
        ctx: dict[str, Any] = {
            "priority": placement._read_mission_priority(db, mission_id)
        }
        bctx = placement._read_budget_context(db, mission_id)
        if bctx:
            ctx["budget"] = bctx
        targets = placement.load_manifest_targets(hermes_root)
        decision = placement.build_decision(mission_id, node_id, base, targets, ctx)
        return decision, ""
    except PermissionError:
        # Secret-like requirement values never cross the dispatch surface.
        return None, REFUSED_SECRET_REQUIREMENT
    except (
        ValueError,
        TypeError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ):
        return None, REFUSED_UNSUPPORTED_ACTION



def _l2_work_contract(
    mission_id: str,
    node_id: str,
    *,
    requirement: dict[str, Any],
    target_name: str,
    idempotency_key: str,
    attempt_seq: int,
    hermes_root: Path | None,
) -> dict[str, Any]:
    """Build the bounded M1 work contract for the L2 dispatch.

    INV-9: the controller never reads the raw node objective (the plan store
    keeps only its hash), so the contract objective is a deterministic bounded
    pointer and no expected artifacts are fabricated — no raw prompt/objective/
    secret text is invented or persisted here. Authority metadata mirrors the
    node's own authorization class (high-impact is refused earlier — the
    controller approves nothing), and the completion criteria stay
    unclaimed (``tests_pass``/``review_satisfied`` False): the controller can
    never assert evidence it did not observe.
    """
    workspace = str(_root(hermes_root) / "missions")
    auth_class = str(requirement.get("authorization_class", "reversible_write"))
    agent = (
        target_name if contract_mod._AGENT_RE.fullmatch(target_name or "") else "auto"
    )
    return {
        "schema": contract_mod.CONTRACT_SCHEMA,
        "task_id": f"ctl-{mission_id[:40]}-{node_id[:32]}-{idempotency_key[:16]}",
        "assigned_agent": agent,
        "assigned_profile": str(requirement.get("profile", "")),
        "objective": (
            f"controller-l2 dispatch: mission={mission_id} node={node_id} "
            f"attempt={int(attempt_seq)}"
        ),
        "allowed_scope": {
            "workspaces": [workspace],
            "profiles": [str(requirement.get("profile", ""))],
        },
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {
            "class": auth_class,
            "approved": True,
            "approved_by": "mission-owner",
            "approval_reference": f"mission:{mission_id}",
        },
    }



def _l2_target_binding(
    decision: dict[str, Any], requirement: dict[str, Any]
) -> tuple[str, str, str]:
    """§3: resolve the scored top candidate onto an existing dispatch identity.

    Returns ``(assigned_agent, assigned_profile, refusal_code)``. Only a
    ``fleet_peer`` candidate names an agent that exists in the fleet authority
    manifest — the identity the delegation/fleet dispatch surface authorizes.
    ``profile`` / ``provider`` / ``fabric_node`` candidates carry no
    dispatchable agent identity, so the rung refuses (fail closed) instead of
    inventing one; resolving those to a peer is a later slice's job.
    """
    top = decision.get("top_candidate") or {}
    kind = str(top.get("kind", ""))
    name = _sanitize(top.get("name", ""), 64)
    profile = _sanitize(requirement.get("profile", ""), 64)
    if kind == "fleet_peer" and name:
        return name, profile, ""
    return "", "", REFUSED_UNSUPPORTED_ACTION
