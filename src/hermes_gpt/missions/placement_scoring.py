"""Deterministic capability filtering, scoring, and decision construction."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hermes_gpt.missions.placement_common import AUTH_RANK, CLASS_ASSIGNED, CLASS_HUMAN, CLASS_NO_TARGET, DECISION_SCHEMA, EXECUTOR_KINDS, SCHEMA_VERSION, WEIGHTS, _now, _sanitize, _stance


def _auth_rank_of(target: dict[str, Any]) -> int | None:
    ceiling = target.get("authorization_ceiling", "")
    if not ceiling:
        return None
    return AUTH_RANK.get(ceiling)


def _apply_hard_filters(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> list[str]:
    """Return the list of hard-filter optout codes for a target (empty = pass).

    Codes (stable, testable):
    - disabled / unreachable / identity_unconfigured
    - required_features_missing
    - auth_ceiling_exceeded
    - profile_out_of_scope
    - workspace_out_of_scope
    - concurrency_full
    - forbidden_action
    """
    optouts: list[str] = []

    # F1 enabled + reachable + identity.
    if not target.get("enabled"):
        optouts.append("disabled")
    if not target.get("reachable"):
        optouts.append("unreachable")
    if not target.get("identity_configured"):
        optouts.append("identity_unconfigured")

    # F2 required features present.
    required_features = requirement.get("features", [])
    if required_features:
        target_features = set(target.get("features") or [])
        if not set(required_features) <= target_features:
            optouts.append("required_features_missing")

    # F3 authorization ceiling not exceeded.
    ceiling_rank = _auth_rank_of(target)
    required_rank = AUTH_RANK[
        requirement.get("authorization_class", "reversible_write")
    ]
    if ceiling_rank is not None and ceiling_rank < required_rank:
        optouts.append("auth_ceiling_exceeded")

    # F4: the profile must be in scope.
    required_profile = requirement.get("profile", "")
    if required_profile and required_profile not in (
        target.get("allowed_profiles") or []
    ):
        optouts.append("profile_out_of_scope")

    # F5 workspace in scope (known workspaces only; unavailable passes with a flag).
    required_workspace = requirement.get("workspace", "")
    if required_workspace:
        workspaces = target.get("workspaces") or []
        if workspaces and required_workspace not in workspaces:
            optouts.append("workspace_out_of_scope")

    # F6 per-profile concurrency headroom.
    concurrency = ctx.get("concurrency", {})
    key = target.get("name") or target.get("entity_id")
    entry = concurrency.get(key) or concurrency.get(target.get("profile", ""))
    if isinstance(entry, dict):
        cap = float(entry.get("max_in_progress", 0))
        running = float(entry.get("in_progress", 0) or 0)
        if cap > 0 and running >= cap:
            optouts.append("concurrency_full")

    # F7 forbidden-action policy.
    forbidden = ctx.get("forbidden_actions") or []
    if "public" in forbidden and target.get("allow_public_actions"):
        optouts.append("forbidden_action")
    return optouts


def _score_capability_fit(target: dict[str, Any], requirement: dict[str, Any]) -> float:
    features = requirement.get("features", [])
    if features:
        have = set(target.get("features") or [])
        return round(len(have & set(features)) / len(features), 6)
    # No explicit feature requirement: an executor kind is position-capable.
    return 1.0 if target.get("kind") in EXECUTOR_KINDS else 0.4


def _score_authorization_match(
    target: dict[str, Any], requirement: dict[str, Any]
) -> float:
    ceiling_rank = _auth_rank_of(target)
    required_rank = AUTH_RANK[
        requirement.get("authorization_class", "reversible_write")
    ]
    if ceiling_rank is None:
        return 0.5  # ceiling unknown -> neutral (recorded in caveats)
    excess = ceiling_rank - required_rank
    if excess <= 0:
        return 1.0  # just enough
    return round(max(0.0, 1.0 - 0.4 * excess), 6)


def _score_affinity(target: dict[str, Any], requirement: dict[str, Any]) -> float:
    skills = requirement.get("skills", [])
    if skills:
        have = set(target.get("skills") or [])
        return round(len(have & set(skills)) / len(skills), 6)
    # No explicit skill requirement: reward the manifest owner of this scope.
    return 1.0 if target.get("name") == requirement.get("profile") else 0.5


def _score_load_headroom(target: dict[str, Any], ctx: dict[str, Any]) -> float:
    entry = ctx.get("concurrency", {}).get(target.get("name")) or ctx.get(
        "concurrency", {}
    ).get(target.get("entity_id"))
    if not isinstance(entry, dict):
        return 1.0  # no concurrency data -> assume idle (recorded)
    cap = float(entry.get("max_in_progress", 0) or 0)
    running = float(entry.get("in_progress", 0) or 0)
    if cap <= 0:
        return 1.0  # unlimited
    return round(max(0.0, 1.0 - running / cap), 6)


def _score_health(target: dict[str, Any], ctx: dict[str, Any]) -> float:
    entry = ctx.get("health", {}).get(target.get("name")) or ctx.get("health", {}).get(
        target.get("entity_id")
    )
    if not isinstance(entry, dict) or float(entry.get("samples", 0) or 0) <= 0:
        return 0.5  # no reputation -> neutral
    rate = float(entry.get("success_rate", 0.0) or 0.0)
    return round(max(0.0, min(1.0, rate)), 6)


def _score_cost_priority(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> float:
    bctx = ctx.get("budget")
    if not isinstance(bctx, dict):
        return 1.0  # no budget constraint -> no cost pressure
    est = int(requirement.get("budget", {}).get("est_tokens", 0) or 0)
    remaining = float(bctx.get("remaining_tokens") or 0.0)
    if est <= 0:
        return 1.0
    if remaining >= est:
        budget_fit = 1.0
    else:
        budget_fit = round(max(0.0, remaining / est), 6)
    priority = int(ctx.get("priority", 0) or 0)
    priority_norm = round(min(1.0, max(0.0, priority / 9.0)), 6)
    # A high-priority, budget-fitting candidate is worth the spend.
    return round(budget_fit * (0.5 + 0.5 * priority_norm), 6)


def _score_dims(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, float]:
    return {
        "capability_fit": _score_capability_fit(target, requirement),
        "authorization_match": _score_authorization_match(target, requirement),
        "affinity": _score_affinity(target, requirement),
        "load_headroom": _score_load_headroom(target, ctx),
        "health": _score_health(target, ctx),
        "cost_priority": _score_cost_priority(target, requirement, ctx),
    }


def _weigh(scores: dict[str, float], weights: dict[str, float]) -> float:
    total = 0.0
    for dim, weight in weights.items():
        total += weight * scores.get(dim, 0.0)
    return round(total, 6)


def _normalize_weights(raw: dict[str, Any] | None) -> dict[str, float]:
    weights = dict(WEIGHTS)
    if isinstance(raw, dict):
        for key in weights:
            if key in raw:
                try:
                    value = float(raw[key])
                except (TypeError, ValueError):
                    raise ValueError(f"weight {key} must be a number") from None
                if value < 0:
                    raise ValueError(f"weight {key} must be non-negative")
                weights[key] = value
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    # Never return a zero total (would collapse ordering); deterministic scale.
    return weights


def _caveats(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> list[str]:
    caveats: list[str] = []
    if _auth_rank_of(target) is None:
        caveats.append("authorization_ceiling_unavailable")
    if requirement.get("workspace") and not (target.get("workspaces") or []):
        caveats.append("workspace_scope_unavailable")
    if not ctx.get("concurrency"):
        caveats.append("load_headroom_unavailable")
    if not ctx.get("health"):
        caveats.append("health_unavailable")
    return caveats


def score_targets(
    requirement: dict[str, Any],
    targets: list[dict[str, Any]],
    ctx: dict[str, Any] | None = None,
    *,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic filter-and-score over candidate targets.

    ``requirement`` is a bounded work-unit requirement (see ``_stance``);
    ``targets`` are normalized manifest targets (``_target_from_entity``);
    ``ctx`` optionally carries ``concurrency`` / ``health`` / ``budget`` /
    ``priority`` / ``forbidden_actions`` / ``weights``. Pure and I/O-free.

    Returns: classification, assigned_agent, top_candidate, candidate_set,
    score_breakdown, filter_optouts.
    """
    req = dict(requirement)
    if "profile" not in req:
        req = _stance(requirement)
    ctx = dict(ctx or {})
    wts = _normalize_weights(ctx.get("weights") or weights)

    # Sort targets for determinism.
    ordered = sorted(targets, key=lambda t: t.get("entity_id", ""))
    optouts: dict[str, list[str]] = {}
    survivors: list[dict[str, Any]] = []
    for target in ordered:
        codes = _apply_hard_filters(target, req, ctx)
        if codes:
            optouts[target.get("entity_id", "")] = codes
        else:
            survivors.append(target)

    breakdown: dict[str, dict[str, Any]] = {}
    scored: list[dict[str, Any]] = []
    for target in survivors:
        dims = _score_dims(target, req, ctx)
        total = _weigh(dims, wts)
        ceiling_rank = _auth_rank_of(target)
        required_rank = AUTH_RANK[req.get("authorization_class", "reversible_write")]
        excess = (ceiling_rank - required_rank) if ceiling_rank is not None else None
        entry = {
            "entity_id": target.get("entity_id", ""),
            "name": target.get("name", ""),
            "kind": target.get("kind", ""),
            "capability_sha256": _sanitize(target.get("capability_sha256", ""), 64),
            "scores": dims,
            "total": total,
            "authorization_excess": excess if excess is not None else None,
            "caveats": _caveats(target, req, ctx),
        }
        entry["_tie"] = excess if excess is not None else 9999
        breakdown[target.get("entity_id", "")] = entry
        scored.append(entry)

    # Deterministic rank: total desc, authorization_excess asc, entity_id asc.
    scored.sort(key=lambda e: (-e["total"], e["_tie"], e["entity_id"]))
    for e in scored:
        e.pop("_tie", None)

    top = scored[0] if scored else None
    agent = "auto" if top else ""
    if req.get("kind") == "approval":
        classification = CLASS_HUMAN
        agent = "owner"
    elif top is None:
        classification = CLASS_NO_TARGET
    else:
        classification = CLASS_ASSIGNED

    return {
        "classification": classification,
        "assigned_agent": agent,
        "top_candidate": top,
        "candidate_set": scored,  # survivors with scores
        "score_breakdown": breakdown,
        "filter_optouts": optouts,
    }


def _decision_digest(
    mission_id: str, node_id: str, requirement: dict[str, Any], verdict: dict[str, Any]
) -> str:
    skeleton = {
        "mission_id": mission_id,
        "node_id": node_id,
        "requirement": requirement,
        "classification": verdict["classification"],
        "assigned_agent": verdict["assigned_agent"],
        "top_candidate": verdict["top_candidate"],
        "candidate_set": verdict["candidate_set"],
        "filter_optouts": verdict["filter_optouts"],
    }
    enc = json.dumps(
        skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def build_decision(
    mission_id: str,
    node_id: str,
    requirement: dict[str, Any],
    targets: list[dict[str, Any]],
    ctx: dict[str, Any] | None = None,
    *,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete, deterministic placement decision envelope (dry-run)."""
    req = _stance(requirement)
    verdict = score_targets(req, targets, ctx, weights=weights)
    digest = _decision_digest(mission_id, node_id, req, verdict)
    escalate = None
    if verdict["classification"] == CLASS_NO_TARGET:
        escalate = {
            "would_raise_signal": "capability",
            "classification": CLASS_NO_TARGET,
            "path": "stubbed",
            "note": "no_capable_target: candidate set empty after hard filters; escalate to Orchestrator (never auto-resolve).",
        }
    return {
        "schema": DECISION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_placement_score",
        "mission_id": mission_id,
        "node_id": node_id,
        "requirement": req,
        "classification": verdict["classification"],
        "assigned_agent": verdict["assigned_agent"],
        "owner": req.get("owner", ""),
        "assigned_agent_note": "auto is distinct from stage owner; the controller proposes, dispatch goes through existing authority",
        "top_candidate": verdict["top_candidate"],
        "candidate_set": verdict["candidate_set"],
        "score_breakdown": verdict["score_breakdown"],
        "filter_optouts": verdict["filter_optouts"],
        "decision_sha256": digest,
        "would_assign": False,
        "dry_run": True,
        "escalate": escalate,
        "generated_at": _now(),
    }
