"""MCP-facing MissionPlan operations and operator review surfaces."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op
from operator_mission_plan_schema import (
    NODE_STATES,
    NODE_TRANSITIONS,
    PLAN_STATUS_DRAFT,
    PLAN_STATUSES,
    SCHEMA_VERSION,
    TERMINAL_NODE_STATES,
    _clean_text,
    _parse_plan,
    decompose_mission,
)
from operator_mission_plan_store import (
    _audit,
    _begin_write,
    _connect,
    _db_path,
    _error,
    _get_plan_row,
    _now,
    _plan_view,
)


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

        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan creation requires confirm=true")

        if effective_dry:
            _audit(
                "hermes_plan_create",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"node_count": len(plan["nodes"]), "plan_sha256": plan_sha},
            )
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "tool": "hermes_plan_create",
                    "mission_id": mission_id,
                    "version": int(plan.get("version", 1)),
                    "plan_sha256": plan_sha,
                    "node_count": len(plan["nodes"]),
                    "status": status,
                    "changed": False,
                    "dry_run": True,
                }
            )

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(
                db, mission_id
            )  # verify mission exists (raises LookupError)
            now = _now()
            existing = db.execute(
                "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
            new_version = (
                int(existing["version"]) + 1
                if existing
                else int(plan.get("version", 1))
            )
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

        _audit(
            "hermes_plan_create",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={
                "version": new_version,
                "node_count": len(plan["nodes"]),
                "plan_sha256": plan_sha,
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_plan_create",
                "mission_id": mission_id,
                "version": new_version,
                "plan_sha256": plan_sha,
                "node_count": len(plan["nodes"]),
                "status": status,
                "changed": True,
                "dry_run": False,
            }
        )
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
            "hermes_plan_create",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "PLAN_CREATE_REJECTED",
            "Check mission id, plan schema, and Operator workspace/direct policy.",
        )


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


def hermes_plan_validate(plan_json: str) -> str:
    """Pure read-only validation of a MissionPlan. Never writes."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        _canonical, plan, plan_sha = _parse_plan(plan_json)
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_plan_validate",
                "valid": True,
                "plan_sha256": plan_sha,
                "mission_id": plan["mission_id"],
                "version": int(plan["version"]),
                "decomposition": plan.get("decomposition", ""),
                "node_count": len(plan["nodes"]),
                "nodes": [
                    {"node_id": n["node_id"], "kind": n["kind"], "state": "pending"}
                    for n in plan["nodes"]
                ],
            }
        )
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
    hermes_root: Path | None = None,
) -> str:
    """Advance a plan node through the validated state machine (design §5.2).

    This mutates only the plan node's own state; it never dispatches a worker
    and never completes/approves a Mission (read-only slice).
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
            row = db.execute(
                "SELECT state FROM plan_nodes WHERE mission_id=? AND node_id=?",
                (mission_id, node_id),
            ).fetchone()
            if row is None:
                raise LookupError(
                    f"plan node {node_id!r} not found for mission {mission_id!r}"
                )
            current = row["state"]
            if (
                current not in NODE_TRANSITIONS
                or target_state not in NODE_TRANSITIONS[current]
            ):
                raise ValueError(
                    f"illegal node transition {current} -> {target_state} for node {node_id!r}"
                )
            if effective_dry:
                db.rollback()
                _audit(
                    "hermes_plan_node_transition",
                    policy,
                    dry_run=True,
                    success=True,
                    changed=False,
                    mission_id=mission_id,
                    node_id=node_id,
                    extra={"from": current, "to": target_state},
                )
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "tool": "hermes_plan_node_transition",
                        "mission_id": mission_id,
                        "node_id": node_id,
                        "from_state": current,
                        "to_state": target_state,
                        "changed": False,
                        "dry_run": True,
                    }
                )
            now = _now()
            db.execute(
                "UPDATE plan_nodes SET state=?, lease_lock='', lease_expires='', updated_at=? WHERE mission_id=? AND node_id=?",
                (target_state, now, mission_id, node_id),
            )
            db.commit()
        _audit(
            "hermes_plan_node_transition",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            node_id=node_id,
            extra={
                "from": current,
                "to": target_state,
                "reason": _clean_text(reason, field="reason", maximum=200),
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_plan_node_transition",
                "mission_id": mission_id,
                "node_id": node_id,
                "from_state": current,
                "to_state": target_state,
                "changed": True,
                "dry_run": False,
            }
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit(
            "hermes_plan_node_transition",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
        )
        return _error(
            exc,
            "PLAN_NODE_TRANSITION_REJECTED",
            "Check node id, target state, and state-machine legality.",
        )


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
