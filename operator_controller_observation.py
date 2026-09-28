"""Build the controller's bounded, authoritative observation envelope."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

import operator_delegations as deleg
import operator_failure_semantics as fs
import operator_mission_runtime as mission
import operator_runners as runners
from operator_controller_store import _sanitize

MAX_NODES = 512
INFlight_ORDER = ("failed", "dispatched", "running", "awaiting_review", "validated")
READY_STATES = ("pending", "blockable")
NODE_TERMINAL = frozenset({"completed", "failed"})


class HostObservationAdapter(Protocol):
    """Read host-owned worker, breaker, failure, and capability signals."""

    def worker_exit(self, mission_id: str, node_id: str) -> dict[str, Any] | None: ...
    def breaker(self, mission_id: str) -> dict[str, Any]: ...
    def last_failure_error(self, mission_id: str, node_id: str) -> str: ...
    def capability(self, mission_id: str, node_id: str) -> dict[str, Any] | None: ...


def _read_nodes(db: sqlite3.Connection, mission_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT node_id,state,deps,contract_sha256,failure_kind,retries,epoch "
        "FROM plan_nodes WHERE mission_id=? ORDER BY node_id LIMIT ?",
        (mission_id, MAX_NODES),
    ).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        try:
            dependencies = json.loads(row["deps"] or "[]")
        except json.JSONDecodeError:
            dependencies = []
        nodes.append(
            {
                "node_id": row["node_id"],
                "state": row["state"],
                "deps": dependencies if isinstance(dependencies, list) else [],
                "contract_sha256": row["contract_sha256"],
                "failure_kind": row["failure_kind"],
                "retries": int(row["retries"] or 0),
                "epoch": int(row["epoch"] or 0),
            }
        )
    return nodes


def _parent_done(nodes: list[dict[str, Any]], node: dict[str, Any]) -> bool:
    parents = [parent_id for parent_id in node["deps"] if parent_id]
    if not parents:
        return True
    nodes_by_id = {item["node_id"]: item for item in nodes}
    for parent_id in parents:
        parent = nodes_by_id.get(parent_id)
        if parent is None or parent["state"] != "completed":
            return False
    return True


def _all_terminal(nodes: list[dict[str, Any]]) -> bool:
    return bool(nodes) and all(node["state"] in NODE_TERMINAL for node in nodes)


def _frontier(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the highest-priority active node, then the first ready node."""
    for state in INFlight_ORDER:
        for node in nodes:
            if node["state"] == state:
                return node
    for node in nodes:
        if node["state"] in READY_STATES and _parent_done(nodes, node):
            return node
    return None


def _replan_attempts_used(db: sqlite3.Connection, mission_id: str) -> int:
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM controller_plan "
            "WHERE mission_id=? AND row_key='escalate_semantic'",
            (mission_id,),
        ).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0


def _latest_delegation(
    hermes_root: Path | None, mission_id: str, contract_sha256: str = ""
) -> dict[str, Any] | None:
    """Read authoritative delegation state, optionally bound to a contract.

    A known contract hash prevents an unrelated parallel node's latest
    delegation from being used as evidence for this node.
    """
    database_path = deleg._db_path(hermes_root)
    if not database_path.is_file():
        return None
    try:
        with deleg._connect(database_path, write=False) as db:
            if contract_sha256:
                row = db.execute(
                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "
                    "FROM delegations WHERE mission_id=? AND contract_sha256=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (mission_id, contract_sha256),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "
                    "FROM delegations WHERE mission_id=? ORDER BY updated_at DESC LIMIT 1",
                    (mission_id,),
                ).fetchone()
            return dict(row) if row else None
    except (sqlite3.Error, FileNotFoundError):
        return None


def _runner_observation(
    hermes_root: Path | None, task_id: str
) -> dict[str, Any] | None:
    """Read the latest available runner status without failing the pass."""
    if not task_id:
        return None
    try:
        runs = runners.observed_runs(task_id, hermes_root=hermes_root)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        sqlite3.Error,
    ):
        runs = []
    latest = runs[-1] if isinstance(runs, list) and runs else None
    if not isinstance(latest, dict):
        return None
    return {
        "status": _sanitize(latest.get("status", ""), 64),
        "outcome": _sanitize(latest.get("outcome", ""), 64),
        "error": _sanitize(latest.get("error", ""), fs.MAX_ERROR_TEXT),
    }


def build_observation(
    db: sqlite3.Connection,
    hermes_root: Path | None,
    mission_id: str,
    host: HostObservationAdapter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the bounded observation envelope and the current plan frontier."""
    mission_row = mission._get_row(db, mission_id)
    spec = json.loads(mission_row["spec_json"])
    nodes = _read_nodes(db, mission_id)
    frontier = _frontier(nodes) or {}
    node_id = frontier.get("node_id", "")
    parent_done = _parent_done(nodes, frontier) if frontier else False
    all_terminal = _all_terminal(nodes)
    replan_used = _replan_attempts_used(db, mission_id)

    delegation_state = None
    if node_id and frontier.get("contract_sha256"):
        delegation_state = _latest_delegation(
            hermes_root, mission_id, str(frontier["contract_sha256"])
        )

    delegation = None
    runner = None
    if delegation_state:
        delegation = {
            "state": delegation_state.get("state", ""),
            "backend_state": delegation_state.get("backend_state", ""),
            "outcome": delegation_state.get("outcome", ""),
            "validation_verdict": delegation_state.get("validation_verdict", ""),
        }
        task_id = str(delegation_state.get("task_id") or "")
        runner = _runner_observation(hermes_root, task_id)

    worker_exit = host.worker_exit(mission_id, node_id)
    breaker = host.breaker(mission_id)
    last_error = host.last_failure_error(mission_id, node_id)
    capability = host.capability(mission_id, node_id)

    env: dict[str, Any] = {
        "mission": {
            "status": mission_row["status"],
            "final_approval_required": bool(spec.get("final_approval_required", True)),
        },
        "plan": {
            "node_state": frontier.get("state", ""),
            "parent_done": parent_done,
            "all_children_terminal": all_terminal,
            "retries": int(frontier.get("retries", 0) or 0),
            "replan_attempts_used": replan_used,
        },
        "delegation": delegation,
        "runner": runner,
        "worker_exit": worker_exit,
        "last_failure_error": last_error,
        "capability": capability,
        "breaker": breaker,
    }
    return env, frontier
