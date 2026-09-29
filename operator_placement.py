"""Deterministic filter-and-score placement over the derived capability manifest.

Implements architecture proposal §8.2 ("Where") / D7 / §17 item 5: a
deterministic, auditable placement decision that filters a work unit's
capability requirement over the derived capability-manifest index (built by
``operator_capability_manifest``), then soft-scores the survivors.

**Phase 2 — dry-run only.** This slice never assigns, never dispatches, never
mutates a Mission or a plan node's lifecycle, and never writes outside its own
allowlisted surface. The decision is *recorded* (``score_breakdown`` +
``candidate_set`` + ``filter_optouts``) both in the returned envelope and, when
the operator policy allows a direct write, in a durable ``placement_decisions``
row in the same ``missions/missions.db`` as the Mission runtime.

Determinism + auditability (D7):
- No LLM in the scoring hot path. Only bounded arithmetic over manifest data.
- The same inputs always produce the same output; iteration is always over
  sorted keys, and ties break on ``(authorization_excess asc, entity_id asc)``.
- ``assigned_agent="auto"`` is kept distinct from the stage/plan node ``owner``;
  the controller proposes, actual dispatch goes through the existing
  ``hermes_contract_dispatch`` / delegation surfaces (authority never overridden).

Invariants:
- **INV-9 data containment.** Only refs, bounded metadata, and content-addressed
  hashes cross the surface. No raw prompt, transcript, memory body, credential,
  or secret-path content is ever emitted; a redaction check rejects
  secret-like requirement/context input.
- **No candidate passes ⇒ ``no_capable_target``** classification (escalate path
  stubbed: a bounded ``would_escalate`` signal, never an actual action).
- Every public call is audited (bounded summary + counts only).

Conventions mirror ``operator_mission_plan`` / ``operator_mission_budget``/
``operator_capability_manifest``: public functions return a JSON ``str`` of a
bounded envelope, read surfaces require ``read_only``, and decision recording
requires ``workspace`` + ``direct`` (dry-run-first).

The pure scoring core (``score_targets`` / ``_apply_hard_filters`` /
``_evaluate_score``) is I/O-free and directly testable with in-memory targets.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import operator_capability_manifest as _cm
import operator_mission_budget as _budget
import operator_mission_runtime as _mission
import operator_placement_common as placement_common
import operator_placement_scoring as placement_scoring
import operator_placement_store as placement_store
import operator_policy as _op

# Keep operator_placement's established names available to controller callers
# while the scoring, input, and persistence code has explicit owners.
cm = _cm
budget = _budget
mission = _mission
op = _op
SCHEMA_VERSION = placement_common.SCHEMA_VERSION
PLACEMENT_SCHEMA = placement_common.PLACEMENT_SCHEMA
DECISION_SCHEMA = placement_common.DECISION_SCHEMA
MISSION_ID_RE = placement_common.MISSION_ID_RE
NODE_ID_RE = placement_common.NODE_ID_RE
SHA_RE = placement_common.SHA_RE
AUTH_ORDER = placement_common.AUTH_ORDER
AUTH_RANK = placement_common.AUTH_RANK
AUTH_CLASSES = placement_common.AUTH_CLASSES
EXECUTOR_KINDS = placement_common.EXECUTOR_KINDS
WEIGHTS = placement_common.WEIGHTS
CLASS_ASSIGNED = placement_common.CLASS_ASSIGNED
CLASS_NO_TARGET = placement_common.CLASS_NO_TARGET
CLASS_HUMAN = placement_common.CLASS_HUMAN
CLASSES = placement_common.CLASSES
MAX_SKILLS = placement_common.MAX_SKILLS
MAX_FEATURES = placement_common.MAX_FEATURES
MAX_TARGETS = placement_common.MAX_TARGETS
MAX_STRING = placement_common.MAX_STRING
_MISSION_MAX = placement_common._MISSION_MAX
_NODE_MAX = placement_common._NODE_MAX
_PII_STRIP = placement_common._PII_STRIP
_now = placement_common._now
_sanitize = placement_common._sanitize
_clean_text = placement_common._clean_text
_clean_str_list = placement_common._clean_str_list
_normalize_auth_class = placement_common._normalize_auth_class
_stance = placement_common._stance
_target_from_entity = placement_common._target_from_entity
load_manifest_targets = placement_common.load_manifest_targets
_auth_rank_of = placement_scoring._auth_rank_of
_apply_hard_filters = placement_scoring._apply_hard_filters
_score_capability_fit = placement_scoring._score_capability_fit
_score_authorization_match = placement_scoring._score_authorization_match
_score_affinity = placement_scoring._score_affinity
_score_load_headroom = placement_scoring._score_load_headroom
_score_health = placement_scoring._score_health
_score_cost_priority = placement_scoring._score_cost_priority
_score_dims = placement_scoring._score_dims
_weigh = placement_scoring._weigh
_normalize_weights = placement_scoring._normalize_weights
_caveats = placement_scoring._caveats
score_targets = placement_scoring.score_targets
_decision_digest = placement_scoring._decision_digest
build_decision = placement_scoring.build_decision
_db_path = placement_store._db_path
_init_placement_tables = placement_store._init_placement_tables
_connect = placement_store._connect
_begin_write = placement_store._begin_write
_audit = placement_store._audit
_error = placement_store._error
_read_node_requirement = placement_store._read_node_requirement
_read_mission_priority = placement_store._read_mission_priority
_read_budget_context = placement_store._read_budget_context
_placement_table_exists = placement_store._placement_table_exists
_upsert_decision = placement_store._upsert_decision
_decision_view = placement_store._decision_view
_node_def = placement_store._node_def


def hermes_placement_score(
    mission_id: str,
    node_id: str,
    *,
    source: str = "",
    features: str = "",
    workspace: str = "",
    backends: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Score + record a plan node's placement (deterministic, dry-run-first).

    Reads the plan node's capability requirement and the derived capability
    manifest, applies the materialized hard filters, soft-scores the survivors,
    and records the decision (``score_breakdown`` + ``candidate_set`` +
    ``filter_optouts``). ``assigned_agent="auto"`` is never a dispatch — this
    slice is dry-run only and returns ``would_assign=False``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        if not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct placement record requires confirm=true")

        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            base = _read_node_requirement(db, mission_id, node_id)
            priority = _read_mission_priority(db, mission_id)
            budget_ctx = _read_budget_context(db, mission_id)

        targets = load_manifest_targets(hermes_root, source=source)
        overrides: dict[str, Any] = {}
        if features:
            overrides["features"] = _clean_str_list(
                [f for f in features.split(",") if f.strip()],
                field="features",
                maximum_items=MAX_FEATURES,
                item_max=128,
            )
        if workspace:
            overrides["workspace"] = _clean_text(
                workspace, field="workspace", maximum=128
            )
        if backends:
            overrides["backends"] = _clean_str_list(
                [b for b in backends.split(",") if b.strip()],
                field="backends",
                maximum_items=64,
                item_max=64,
            )
        requirement = dict(base)
        requirement.update(overrides)
        node_def = _node_def(path, mission_id, node_id)
        requirement["kind"] = node_def["kind"]
        requirement["owner"] = node_def["owner"]

        ctx = {"priority": priority}
        if budget_ctx:
            ctx["budget"] = budget_ctx

        decision = build_decision(mission_id, node_id, requirement, targets, ctx)

        if effective_dry:
            _audit(
                "hermes_placement_score",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                node_id=node_id,
                extra={
                    "classification": decision["classification"],
                    "assigned_agent": decision["assigned_agent"],
                    "candidate_count": len(decision["candidate_set"]),
                    "optout_count": len(decision["filter_optouts"]),
                    "decision_sha256": decision["decision_sha256"],
                },
            )
            return json.dumps(decision, ensure_ascii=False, indent=2)

        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists
            _upsert_decision(db, decision)
            db.commit()
            persisted = {"node_id": node_id, "mission_id": mission_id}

        _audit(
            "hermes_placement_score",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            node_id=node_id,
            extra={
                "classification": decision["classification"],
                "assigned_agent": decision["assigned_agent"],
                "candidate_count": len(decision["candidate_set"]),
                "optout_count": len(decision["filter_optouts"]),
                "decision_sha256": decision["decision_sha256"],
            },
        )
        decision["changed"] = True
        decision["dry_run"] = False
        decision["persisted"] = persisted
        return json.dumps(decision, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        _audit(
            "hermes_placement_score",
            op.OperatorPolicy(),
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
        )
        return _error(
            exc,
            "PLACEMENT_SCORE_REJECTED",
            "Check mission/node id, manifest source, and Operator workspace/direct policy.",
        )


def hermes_placement_candidates(
    profile: str,
    *,
    skills: str = "",
    authorization_class: str = "reversible_write",
    features: str = "",
    workspace: str = "",
    backends: str = "",
    source: str = "",
    hermes_root: Path | None = None,
) -> str:
    """Read-only probe: list candidate targets + per-candidate filter/score.

    Never records, never writes. Enforces ``read_only``. Useful as the
    transparent, deterministic first step before any decision is recorded.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        requirement: dict[str, Any] = {
            "profile": _clean_text(profile, field="profile", maximum=64, required=True),
            "skills": _clean_str_list(
                [s for s in skills.split(",") if s.strip()],
                field="skills",
                maximum_items=MAX_SKILLS,
                item_max=128,
            ),
            "authorization_class": _normalize_auth_class(authorization_class),
        }
        if features:
            requirement["features"] = _clean_str_list(
                [f for f in features.split(",") if f.strip()],
                field="features",
                maximum_items=MAX_FEATURES,
                item_max=128,
            )
        if workspace:
            requirement["workspace"] = _clean_text(
                workspace, field="workspace", maximum=128
            )
        if backends:
            requirement["backends"] = _clean_str_list(
                [b for b in backends.split(",") if b.strip()],
                field="backends",
                maximum_items=64,
                item_max=64,
            )
        targets = load_manifest_targets(hermes_root, source=source)
        verdict = score_targets(_stance(requirement), targets, {})
        envelope = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "surface": "placement_candidates",
            "tool": "hermes_placement_candidates",
            "requirement": _stance(requirement),
            "classification": verdict["classification"],
            "candidate_set": verdict["candidate_set"],
            "score_breakdown": verdict["score_breakdown"],
            "filter_optouts": verdict["filter_optouts"],
            "top_candidate": verdict["top_candidate"],
            "count_candidates": len(verdict["candidate_set"]),
            "count_filtered": len(verdict["filter_optouts"]),
            "count_total": len(targets),
        }
        _audit(
            "hermes_placement_candidates",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={
                "profile": profile[:64],
                "classification": verdict["classification"],
                "candidates": len(verdict["candidate_set"]),
                "filtered": len(verdict["filter_optouts"]),
            },
        )
        return json.dumps(envelope, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        return _error(
            exc,
            "PLACEMENT_CANDIDATES_FAILED",
            "Check profile/authorization_class and Operator read access.",
        )


def hermes_placement_get(
    mission_id: str, node_id: str, *, hermes_root: Path | None = None
) -> str:
    """Read a recorded placement decision (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        if not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
            )
        with _connect(path, write=False) as db:
            if not _placement_table_exists(db):
                return json.dumps(
                    {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
                )
            row = db.execute(
                "SELECT * FROM placement_decisions WHERE mission_id=? AND node_id=?",
                (mission_id, node_id),
            ).fetchone()
        if row is None:
            return json.dumps(
                {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
            )
        view = _decision_view(row)
        view["success"] = True
        view["found"] = True
        _audit(
            "hermes_placement_get",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
            extra={"classification": view.get("classification", "")},
        )
        return json.dumps(view, ensure_ascii=False, indent=2)
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "PLACEMENT_GET_FAILED",
            "Check mission/node id and Operator read access.",
        )


def hermes_placement_list(
    mission_id: str, limit: int = 50, *, hermes_root: Path | None = None
) -> str:
    """List recorded placement decisions for a mission (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "decisions": [],
                    "count": 0,
                }
            )
        with _connect(path, write=False) as db:
            if not _placement_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "decisions": [],
                        "count": 0,
                    }
                )
            rows = db.execute(
                "SELECT * FROM placement_decisions WHERE mission_id=? ORDER BY updated_at DESC, node_id ASC LIMIT ?",
                (mission_id, limit),
            ).fetchall()
        decisions = []
        for row in rows:
            view = _decision_view(row)
            view.pop("candidate_set", None)
            view.pop("score_breakdown", None)
            view.pop("filter_optouts", None)
            decisions.append(view)
        _audit(
            "hermes_placement_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"count": len(decisions)},
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "decisions": decisions,
                "count": len(decisions),
            },
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "PLACEMENT_LIST_FAILED", "Check mission id and Operator read access."
        )


def validate_requirement(requirement: dict[str, Any]) -> bool:
    """Return whether a requirement canonicalizes without raising."""
    try:
        _stance(requirement)
        return True
    except (ValueError, TypeError, PermissionError):
        return False


def dispatch_view(
    decision: dict[str, Any],
    *,
    dispatched: bool,
    idempotency_key: str = "",
    refused_reason: str = "",
) -> dict[str, Any]:
    """Bounded placement summary for the controller's L2-rung execution envelope.

    Thin wiring only (v0.12 slice-2 Pack B): the scoring math, filters, and
    the ``no_capable_target`` classification logic are untouched. This view
    derives ``would_assign`` truthfully — ``True`` only when an assignment
    was actually dispatched under the full L2 gate set — and carries only
    INV-9-bounded fields (ids, classification, counts, bounded strings).
    """
    top = decision.get("top_candidate") or {}
    return {
        "classification": decision.get("classification", ""),
        "would_assign": bool(dispatched),
        "top_candidate": _sanitize(top.get("entity_id", ""), MAX_STRING),
        "top_kind": _sanitize(top.get("kind", ""), 32),
        "candidate_count": len(decision.get("candidate_set") or []),
        "optout_count": len(decision.get("filter_optouts") or {}),
        "decision_sha256": _sanitize(decision.get("decision_sha256", ""), 64),
        "idempotency_key": _sanitize(idempotency_key, 64),
        "refused_reason": _sanitize(refused_reason, 64),
    }
