"""Choose ready Swarm stages while enforcing workflow and board caps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import operator_swarm_model as model
import operator_swarm_store as store


def stage_ready(record: dict[str, Any], stage: dict[str, Any]) -> tuple[bool, str]:
    """A stage is ready when all fan-in parents are done and it is todo/rework."""
    st = store.stage_state(record, stage["id"])
    if st is None:
        return False, "unknown stage"
    if st["status"] not in (
        model.STAGE_STATUS_TODO,
        model.STAGE_STATUS_REWORK,
    ):
        return False, f"stage not dispatchable (status={st['status']})"
    for parent_id in stage.get("parents") or []:
        parent = store.stage_state(record, parent_id)
        if parent is None or parent["status"] != model.STAGE_STATUS_DONE:
            return False, f"parent {parent_id} not done"
    return True, "ready"


def running_count(record: dict[str, Any]) -> int:
    return sum(
        1
        for stage in record.get("stages", [])
        if stage.get("status") == model.STAGE_STATUS_RUNNING
    )


def board_running_count(hermes_root: Path) -> int:
    """Global board-level running count across all workflows (D-SW3)."""
    total = 0
    for rec in store.list_records(hermes_root):
        total += running_count(rec)
    return total


def next_ready_stages(
    record: dict[str, Any], workflow: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stages that can dispatch now, in declaration order, respecting caps.

    Enforces the per-workflow concurrency cap (D-SW3). The global board cap
    is applied by callers that know ``hermes_root``.
    """
    max_parallel, _, _ = model.workflow_caps(workflow)
    running = running_count(record)
    budget = max(0, max_parallel - running)
    if budget <= 0:
        return []
    ready: list[dict[str, Any]] = []
    for stage in workflow["stages"]:
        ok, _reason = stage_ready(record, stage)
        if ok:
            ready.append(stage)
        if len(ready) >= budget:
            break
    return ready
