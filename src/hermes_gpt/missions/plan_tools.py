"""MCP-facing MissionPlan operations and operator review surfaces."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.missions import runtime as mission
from hermes_gpt.missions.plan_schema import (
    KIND_APPROVAL,
    NODE_STATES,
    NODE_TRANSITIONS,
    PLAN_STATUS_DRAFT,
    PLAN_STATUSES,
    SCHEMA_VERSION,
    TERMINAL_NODE_STATES,
    _canonical_node,
    _canonical_plan,
    _clean_text,
    _parse_plan,
    _plan_sha256,
    decompose_mission,
)
from hermes_gpt.missions.plan_store import (
    PlanInFlightError,
    PlanVersionConflict,
    _audit,
    _begin_write,
    _connect,
    _db_path,
    _error,
    _get_plan_row,
    _nodes_rows,
    _now,
    _plan_view,
)
from hermes_gpt.policy import authorization as op
from hermes_gpt.skills import resolution as skill_resolution


def _validate_plan_skill_requirements(
    plan: dict[str, Any], hermes_root: Path | None
) -> None:
    """Check every node against its logical profile before plan use."""
    for node in plan.get("nodes", []):
        capability = node.get("capability_req") or {}
        rejection = skill_resolution.validate_required_skills(
            capability.get("profile", ""),
            capability.get("skills", []),
            hermes_root,
        )
        if rejection is not None:
            rejection = {**rejection, "node_id": node.get("node_id", "")}
            raise skill_resolution.SkillRequirementsError(rejection)


def hermes_plan_create(
    mission_id: str,
    plan_json: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    status: str = PLAN_STATUS_DRAFT,
    hermes_root: Path | None = None,
) -> str:
    """Create (or replace-version) a MissionPlan for a mission. Additive.

    Never dispatches work or mutates the Mission lifecycle. If ``plan_json`` is
    empty, the plan is derived deterministically from the mission spec
    (canonical Swarm shape). ``status`` is the plan review status.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if status not in PLAN_STATUSES:
            raise ValueError(f"plan status must be one of {list(PLAN_STATUSES)}")

        if plan_json.strip():
            _canonical, plan, plan_sha = _parse_plan(plan_json)
        else:
            # MissionSpec -> bounded DAG, deterministic.
            raw = json.loads(_read_spec(mission_id, hermes_root))
            plan = decompose_mission(
                mission_id,
                objective=raw.get("objective", ""),
                owner_profile=raw.get("owner_profile", "default"),
                use_canonical=True,
            )
            _canonical, plan, plan_sha = _parse_plan(json.dumps(plan))

        # Capability requirements are validated against the target profile
        # before any plan row or node row can be created/replaced.
        _validate_plan_skill_requirements(plan, hermes_root)

        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan creation requires confirm=true")

        if effective_dry:
            path = _db_path(hermes_root)
            if path.exists():
                with _connect(path, write=False) as db:
                    tables = db.execute("SELECT name FROM sqlite_master WHERE name='mission_plans'").fetchone()
                    if tables and db.execute("SELECT 1 FROM mission_plans WHERE mission_id=?", (mission_id,)).fetchone():
                        _assert_replaceable(db, mission_id, hermes_root)
            _audit("hermes_plan_create", policy, dry_run=True, success=True, changed=False,
                   mission_id=mission_id, extra={"node_count": len(plan["nodes"]), "plan_sha256": plan_sha})
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_create",
                "mission_id": mission_id, "version": int(plan.get("version", 1)),
                "plan_sha256": plan_sha, "node_count": len(plan["nodes"]),
                "status": status, "changed": False, "dry_run": True,
            })

        path = _db_path(hermes_root)
        with _replacement_lease(mission_id, hermes_root), _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(
                db, mission_id
            )  # verify mission exists (raises LookupError)
            now = _now()
            existing = db.execute(
                "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
            if existing:
                _assert_replaceable(db, mission_id, hermes_root)
            new_version = int(existing["version"]) + 1 if existing else int(plan.get("version", 1))
            store_plan = dict(plan)
            store_plan.pop("status", None)
            if existing:
                db.execute(
                    "UPDATE mission_plans SET plan_json=?, version=?, status=?, plan_sha256=?, decomposition=?, updated_at=? WHERE mission_id=?",
                    (
                        json.dumps(store_plan, sort_keys=True),
                        new_version,
                        status,
                        plan_sha,
                        plan.get("decomposition", ""),
                        now,
                        mission_id,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO mission_plans(mission_id,plan_json,version,status,plan_sha256,decomposition,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        mission_id,
                        json.dumps(store_plan, sort_keys=True),
                        new_version,
                        status,
                        plan_sha,
                        plan.get("decomposition", ""),
                        now,
                        now,
                    ),
                )
            db.execute("DELETE FROM plan_nodes WHERE mission_id=?", (mission_id,))
            for node in plan["nodes"]:
                db.execute(
                    "INSERT INTO plan_nodes(mission_id,node_id,contract_sha256,capability_req,budget,deps,state,lease_lock,lease_expires,epoch,failure_kind,retries,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mission_id,
                        node["node_id"],
                        node["contract_sha256"],
                        json.dumps(node["capability_req"], sort_keys=True),
                        json.dumps(node["budget"], sort_keys=True),
                        json.dumps(node["parents"], sort_keys=True),
                        node.get("state", "pending"),
                        "",
                        "",
                        0,
                        "",
                        0,
                        now,
                        now,
                    ),
                )
            db.commit()

        _audit("hermes_plan_create", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, extra={"version": new_version, "node_count": len(plan["nodes"]), "plan_sha256": plan_sha})
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_create",
            "mission_id": mission_id, "version": new_version, "plan_sha256": plan_sha,
            "node_count": len(plan["nodes"]), "status": status, "changed": True, "dry_run": False,
        })
    except PlanInFlightError as exc:
        _audit("hermes_plan_create", policy, dry_run=dry_run, success=False, changed=False, mission_id=mission_id)
        return _error(exc, "PLAN_IN_FLIGHT", "Stop the scheduler and resolve unfinished work before replacing the plan.")
    except skill_resolution.SkillRequirementsError as exc:
        _audit(
            "hermes_plan_create",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "PLAN_SKILL_REQUIREMENTS_REJECTED",
            "Install the required skills in the requested Hermes profile before creating the plan.",
            extra={"skill_validation": exc.rejection},
        )
    except (ValueError, TypeError, PermissionError, LookupError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        _audit("hermes_plan_create", policy, dry_run=dry_run, success=False, changed=False, mission_id=mission_id)
        return _error(exc, "PLAN_CREATE_REJECTED", "Check mission id, plan schema, and Operator workspace/direct policy.")


def _read_spec(mission_id: str, hermes_root: Path | None) -> str:
    """Return a mission spec JSON string (used by decompose). Read-only."""
    with mission._connect(_db_path(hermes_root), write=False) as db:
        row = mission._get_row(db, mission_id)
        return row["spec_json"]


def hermes_plan_get(mission_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            row = _get_plan_row(db, mission_id)
            value = _plan_view(db, row)
        value["success"] = True
        _audit(
            "hermes_plan_get",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
        )
        return json.dumps(value)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "PLAN_READ_FAILED", "Check mission id and Operator read access."
        )


def hermes_plan_list(
    status: str = "", limit: int = 50, hermes_root: Path | None = None
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if status and status not in PLAN_STATUSES:
            raise ValueError("plan status filter is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "plans": [],
                    "count": 0,
                }
            )
        with _connect(path, write=False) as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM mission_plans WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM mission_plans ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            plans = []
            for row in rows:
                view = _plan_view(db, row, include_nodes=False)
                view.pop("nodes", None)
                plans.append(view)
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "plans": plans,
                "count": len(plans),
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "PLAN_LIST_FAILED", "Check status/limit and Operator read access."
        )


def hermes_plan_validate(
    plan_json: str, hermes_root: Path | None = None
) -> str:
    """Pure read-only validation of a MissionPlan. Never writes."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        _canonical, plan, plan_sha = _parse_plan(plan_json)
        _validate_plan_skill_requirements(plan, hermes_root)
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_validate",
            "valid": True, "plan_sha256": plan_sha, "mission_id": plan["mission_id"],
            "version": int(plan["version"]), "decomposition": plan.get("decomposition", ""),
            "node_count": len(plan["nodes"]),
            "nodes": [{"node_id": n["node_id"], "kind": n["kind"], "state": "pending"} for n in plan["nodes"]],
        })
    except skill_resolution.SkillRequirementsError as exc:
        payload = json.loads(
            _error(
                exc,
                "PLAN_SKILL_REQUIREMENTS_REJECTED",
                "Install the required skills in the requested Hermes profile before validating the plan.",
                extra={"skill_validation": exc.rejection},
            )
        )
        payload.update({
            "schema_version": SCHEMA_VERSION,
            "tool": "hermes_plan_validate",
            "valid": False,
        })
        return json.dumps(payload)
    except (ValueError, PermissionError, json.JSONDecodeError) as exc:
        return json.dumps(
            {
                "success": False,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_plan_validate",
                "valid": False,
                "error": _error(exc, "PLAN_VALIDATE_FAILED", "Check plan schema."),
            }
        )


def hermes_plan_decompose(mission_id: str, hermes_root: Path | None = None) -> str:
    """Read-only: deterministic MissionSpec -> bounded plan DAG (not persisted)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        raw = json.loads(_read_spec(mission_id, hermes_root))
        plan = decompose_mission(
            mission_id,
            objective=raw.get("objective", ""),
            owner_profile=raw.get("owner_profile", "default"),
            use_canonical=True,
        )
        plan["success"] = True
        plan["schema_version"] = SCHEMA_VERSION
        plan["tool"] = "hermes_plan_decompose"
        _audit(
            "hermes_plan_decompose",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"node_count": len(plan["nodes"])},
        )
        return json.dumps(plan)
    except (
        ValueError,
        LookupError,
        PermissionError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        return _error(
            exc, "PLAN_DECOMPOSE_FAILED", "Check mission id and Operator read access."
        )


def _ready_node_ids(nodes: list[dict[str, Any]]) -> list[str]:
    """Return pending nodes whose complete parent set succeeded."""
    by_id = {n["node_id"]: n for n in nodes}
    return [
        n["node_id"]
        for n in nodes
        if n["state"] == "pending"
        and all(
            parent_id in by_id and by_id[parent_id]["state"] == "completed"
            for parent_id in n.get("parents", [])
        )
    ]


def hermes_plan_review(mission_id: str, hermes_root: Path | None = None) -> str:
    """Operator review surface (read-only): the bounded DAG + node state."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            row = _get_plan_row(db, mission_id)
            value = _plan_view(db, row)
            nodes = value["nodes"]
            # Topological order (Kahn) for operator readability.
            value["topological_order"] = _topological_order(
                [{"node_id": n["node_id"], "parents": n["parents"]} for n in nodes]
            )
            value["terminal_nodes"] = [
                n["node_id"] for n in nodes if n["state"] in TERMINAL_NODE_STATES
            ]
            # Ready means every declared parent completed successfully.
            value["ready_nodes"] = _ready_node_ids(nodes)
        value["success"] = True
        value["tool"] = "hermes_plan_review"
        _audit(
            "hermes_plan_review",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
        )
        return json.dumps(value)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "PLAN_REVIEW_FAILED", "Check mission id and Operator read access."
        )


def _topological_order(nodes: list[dict[str, Any]]) -> list[str]:
    indegree: dict[str, int] = {n["node_id"]: 0 for n in nodes}
    for n in nodes:
        for parent in n.get("parents") or []:
            indegree[n["node_id"]] += 1
    queue = [nid for nid in indegree if indegree[nid] == 0]
    order: list[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for n in nodes:
            if cur in (n.get("parents") or []):
                indegree[n["node_id"]] -= 1
                if indegree[n["node_id"]] == 0:
                    queue.append(n["node_id"])
    return order


def hermes_plan_node_transition(
    mission_id: str,
    node_id: str,
    target_state: str,
    reason: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    expected_plan_version: int | None = None,
    bump_retries: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Advance a plan node through the validated state machine (design §5.2).

    This mutates only the plan node's own state; it never dispatches a worker
    and never completes/approves a Mission (read-only slice).

    ``expected_plan_version`` is an optional compare-and-swap: when given, the
    transition is refused with ``PLAN_VERSION_CONFLICT`` unless
    ``mission_plans.version`` still equals it, checked inside the same
    ``BEGIN IMMEDIATE`` transaction as the write (and before the dry-run
    return, so a dry run can pre-validate it). ``None`` keeps the historical
    unchecked behavior.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if target_state not in NODE_STATES:
            raise ValueError(f"target node state must be one of {list(NODE_STATES)}")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct node transition requires confirm=true")

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            if expected_plan_version is not None:
                plan_row = db.execute(
                    "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
                ).fetchone()
                actual_version = int(plan_row["version"]) if plan_row else None
                if actual_version != int(expected_plan_version):
                    raise PlanVersionConflict(int(expected_plan_version), actual_version)
            row = db.execute(
                "SELECT state FROM plan_nodes WHERE mission_id=? AND node_id=?", (mission_id, node_id)
            ).fetchone()
            if row is None:
                raise LookupError(f"plan node {node_id!r} not found for mission {mission_id!r}")
            current = row["state"]
            if current not in NODE_TRANSITIONS or target_state not in NODE_TRANSITIONS[current]:
                raise ValueError(f"illegal node transition {current} -> {target_state} for node {node_id!r}")
            if effective_dry:
                db.rollback()
                _audit("hermes_plan_node_transition", policy, dry_run=True, success=True, changed=False,
                       mission_id=mission_id, node_id=node_id, extra={"from": current, "to": target_state})
                return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_node_transition",
                                   "mission_id": mission_id, "node_id": node_id, "from_state": current,
                                   "to_state": target_state, "changed": False, "dry_run": True})
            now = _now()
            db.execute(
                "UPDATE plan_nodes SET state=?, lease_lock='', lease_expires='', retries=retries+?, updated_at=? WHERE mission_id=? AND node_id=?",
                (target_state, 1 if bump_retries else 0, now, mission_id, node_id),
            )
            db.commit()
        _audit("hermes_plan_node_transition", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, node_id=node_id, extra={"from": current, "to": target_state, "reason": _clean_text(reason, field="reason", maximum=200)})
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_node_transition",
                           "mission_id": mission_id, "node_id": node_id, "from_state": current,
                           "to_state": target_state, "changed": True, "dry_run": False})
    except PlanVersionConflict as exc:
        _audit("hermes_plan_node_transition", policy, dry_run=dry_run, success=False, changed=False,
               mission_id=mission_id, node_id=node_id)
        return _error(
            exc, "PLAN_VERSION_CONFLICT",
            "The plan was replaced since it was read; re-read the plan and recompute before writing.",
            extra={"expected_plan_version": exc.expected, "actual_plan_version": exc.actual},
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit("hermes_plan_node_transition", policy, dry_run=dry_run, success=False, changed=False,
               mission_id=mission_id, node_id=node_id)
        return _error(exc, "PLAN_NODE_TRANSITION_REJECTED", "Check node id, target state, and state-machine legality.")


def hermes_plan_set_status(
    mission_id: str,
    status: str,
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Set the plan-level review status (operator-reviewable). Read-only re mission."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if status not in PLAN_STATUSES:
            raise ValueError(f"plan status must be one of {list(PLAN_STATUSES)}")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan status update requires confirm=true")
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = db.execute(
                "SELECT status FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"mission plan {mission_id!r} not found")
            if effective_dry:
                db.rollback()
                _audit(
                    "hermes_plan_set_status",
                    policy,
                    dry_run=True,
                    success=True,
                    changed=False,
                    mission_id=mission_id,
                    extra={"to": status},
                )
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "tool": "hermes_plan_set_status",
                        "mission_id": mission_id,
                        "status": status,
                        "changed": False,
                        "dry_run": True,
                    }
                )
            db.execute(
                "UPDATE mission_plans SET status=?, updated_at=? WHERE mission_id=?",
                (status, _now(), mission_id),
            )
            db.commit()
        _audit(
            "hermes_plan_set_status",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={"to": status},
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_plan_set_status",
                "mission_id": mission_id,
                "status": status,
                "changed": True,
                "dry_run": False,
            }
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit(
            "hermes_plan_set_status",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(exc, "PLAN_STATUS_REJECTED", "Check mission id and plan status.")


def apply_rework_patch(
    mission_id: str,
    failed_node_id: str,
    *,
    expected_plan_version: int,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Replace a *failed* node by a fresh clone, in place, without touching history.

    Level B recovery. The failed node stays ``failed`` (evidence preserved); a
    clone ``<node>-rN`` with the same definition and parents is added as
    ``pending``; the failed node's direct children are re-parented onto the
    clone. ``mission_plans.version`` is deliberately **not** bumped: the plan was
    extended, not replaced, so dispatch identities of in-flight nodes stay
    valid. Concurrent replacement by ``hermes_plan_create`` still bumps the
    version and is caught by ``expected_plan_version``.

    Refused (``ValueError``/``PlanVersionConflict``) unless: the version matches;
    the node is ``failed`` and not an approval node or ``high_impact``; every
    direct child is still ``pending``; and the patched plan re-validates as a
    whole (DAG, caps, secret scan) with the clone's skills re-resolved. Never
    touches a completed node, an authorization class, or an approval node.
    """
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        _begin_write(db)
        row = _get_plan_row(db, mission_id)
        if int(row["version"]) != int(expected_plan_version):
            raise PlanVersionConflict(int(expected_plan_version), int(row["version"]))
        plan = json.loads(row["plan_json"])
        old_nodes = plan["nodes"]
        states = {r["node_id"]: r["state"] for r in _nodes_rows(db, mission_id)}
        by_id = {n["node_id"]: n for n in old_nodes}
        failed = by_id.get(failed_node_id)
        if failed is None or states.get(failed_node_id) != "failed":
            raise ValueError("rework requires an existing failed node")
        if failed["kind"] == KIND_APPROVAL or failed["capability_req"].get("authorization_class") == "high_impact":
            raise ValueError("a human-gated node is never replanned around")
        children = [n for n in old_nodes if failed_node_id in n["parents"]]
        started = [n["node_id"] for n in children if states.get(n["node_id"]) != "pending"]
        if started:
            raise ValueError(f"rework refused: descendant already started: {started}")
        clone_id = _rework_clone_id(set(by_id), failed_node_id)
        child_ids = {c["node_id"] for c in children}

        clone = _canonical_node({**failed, "node_id": clone_id, "contract_sha256": ""})
        new_nodes: list[dict[str, Any]] = []
        for node in old_nodes:
            if node["node_id"] in child_ids:
                reparented = [clone_id if p == failed_node_id else p for p in node["parents"]]
                node = _canonical_node({**node, "parents": reparented, "contract_sha256": ""})
            new_nodes.append(node)
        new_nodes.append(clone)
        _assert_rework_invariants(old_nodes, new_nodes, states, failed_id=failed_node_id,
                                  clone_id=clone_id, child_ids=child_ids)

        raw_plan = {k: v for k, v in plan.items() if k != "nodes"}
        raw_plan["nodes"] = new_nodes
        canonical_json, canonical = _canonical_plan(raw_plan)  # DAG, caps, secret scan on the whole result
        _validate_plan_skill_requirements({"nodes": [clone]}, hermes_root)
        canon_by = {n["node_id"]: n for n in canonical["nodes"]}
        now = _now()
        db.execute(
            "UPDATE mission_plans SET plan_json=?, plan_sha256=?, updated_at=? WHERE mission_id=?",
            (json.dumps({k: v for k, v in canonical.items() if k != "status"}, sort_keys=True),
             _plan_sha256(canonical_json), now, mission_id),
        )
        db.execute(
            "INSERT INTO plan_nodes(mission_id,node_id,contract_sha256,capability_req,budget,deps,state,lease_lock,lease_expires,epoch,failure_kind,retries,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mission_id, clone_id, clone["contract_sha256"], json.dumps(clone["capability_req"], sort_keys=True),
             json.dumps(clone["budget"], sort_keys=True), json.dumps(clone["parents"], sort_keys=True),
             "pending", "", "", 0, "", 0, now, now),
        )
        for child_id in sorted(child_ids):
            child = canon_by[child_id]
            db.execute(
                "UPDATE plan_nodes SET deps=?, contract_sha256=?, updated_at=? WHERE mission_id=? AND node_id=?",
                (json.dumps(child["parents"], sort_keys=True), child["contract_sha256"], now, mission_id, child_id),
            )
        db.commit()
    _audit("hermes_plan_rework_patch", op.OperatorPolicy(), dry_run=False, success=True, changed=True,
           mission_id=mission_id, node_id=failed_node_id,
           extra={"clone": clone_id, "reparented": sorted(child_ids), "plan_version": int(expected_plan_version)})
    return {"clone_node_id": clone_id, "reparented": sorted(child_ids), "plan_version": int(expected_plan_version)}


@contextlib.contextmanager
def _replacement_lease(mission_id: str, hermes_root: Path | None):
    # Serialize with both schedulers, including the dispatch-before-node-write
    # window. Checking node state alone cannot close that window.
    from hermes_gpt.missions import controller

    lock = f"plan-replace-{os.getpid()}-{secrets.token_hex(8)}"
    with controller._connect(controller._db_path(hermes_root), write=True) as db:
        lease = controller.acquire_lease(db, mission_id, "operator_request", ttl=60, lease_lock=lock)
        if not lease.get("acquired"):
            raise PlanInFlightError("A scheduler pass is active; stop and drain work before replacing the plan")
        try:
            yield
        finally:
            controller.release_lease(db, mission_id, lock)


def _assert_replaceable(db: sqlite3.Connection, mission_id: str, hermes_root: Path | None) -> None:
    from hermes_gpt.missions import autopilot

    active = db.execute(
        "SELECT node_id FROM plan_nodes WHERE mission_id=? AND state IN "
        "('dispatched','running','awaiting_review','validated','awaiting_approval','paused') LIMIT 1",
        (mission_id,),
    ).fetchone()
    run = autopilot._read_run(mission_id, hermes_root)
    if active or (run and run.get("state") not in autopilot.TERMINAL_STATES):
        raise PlanInFlightError("Stop Autopilot and resolve unfinished plan nodes before replacing the plan")
    current = mission._row_to_mission(db, mission._get_row(db, mission_id))
    root = mission._root(hermes_root)
    for attachment in current["attachments"]:
        if attachment["kind"] == "delegation":
            state, _, _ = mission._delegation_state(root, mission_id, attachment)
            if state not in ("succeeded", "failed", "cancelled"):
                raise PlanInFlightError("Resolve unfinished delegations before replacing the plan")


def _rework_clone_id(existing: set[str], failed_id: str) -> str:
    for n in range(1, 100):
        candidate = f"{failed_id[:58]}-r{n}"
        if candidate not in existing:
            return candidate
    raise ValueError("too many rework clones for this node")


def _assert_rework_invariants(
    old_nodes: list[dict[str, Any]], new_nodes: list[dict[str, Any]], states: dict[str, str],
    *, failed_id: str, clone_id: str, child_ids: set[str],
) -> None:
    """Belt-and-braces re-check that the patch touched only what it may touch.

    The patch is built to satisfy these by construction; asserting them again
    on the finished result means a future edit that breaks one fails loudly
    instead of silently rewriting history.
    """
    old_by = {n["node_id"]: n for n in old_nodes}
    new_by = {n["node_id"]: n for n in new_nodes}
    if set(new_by) != set(old_by) | {clone_id}:
        raise ValueError("rework patch may only add the clone node")
    for node_id, old in old_by.items():
        new = new_by[node_id]
        if states.get(node_id) == "completed" and new != old:
            raise ValueError(f"rework patch may not rewrite completed node {node_id!r}")
        if node_id == failed_id and new != old:
            raise ValueError("rework patch may not rewrite the failed node (its history is preserved)")
        if node_id not in child_ids and new != old:
            raise ValueError(f"rework patch may not change unrelated node {node_id!r}")
        for key in ("kind", "owner", "capability_req", "budget", "expected_artifacts", "objective_sha256"):
            if new[key] != old[key]:
                raise ValueError(f"rework patch may not change {key} of node {node_id!r}")
        if new.get("artifact_requirements") != old.get("artifact_requirements"):
            raise ValueError("rework patch may not change artifact acceptance requirements")
    clone, failed = new_by[clone_id], old_by[failed_id]
    if clone.get("artifact_requirements") != failed.get("artifact_requirements"):
        raise ValueError("rework clone must preserve artifact acceptance requirements")
    for key in ("kind", "owner", "capability_req", "budget", "expected_artifacts", "objective_sha256", "parents"):
        if clone[key] != failed[key]:
            raise ValueError(f"rework clone must copy {key} of the failed node")
    if sum(1 for n in new_nodes if n["kind"] == KIND_APPROVAL) != sum(1 for n in old_nodes if n["kind"] == KIND_APPROVAL):
        raise ValueError("rework patch may not add or remove an approval node")
