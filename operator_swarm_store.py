"""Persist bounded Swarm workflow state under the Hermes data root."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_swarm_model import (
    RETENTION_NOTE,
    SCHEMA_VERSION,
    STAGE_STATUS_TODO,
    WORKFLOW_SCHEMA,
    WORKFLOW_STATUS_RUNNING,
)


def default_hermes_root() -> Path | None:
    """Resolve the user's Hermes data root for Swarm state."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for candidate in (
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return Path.home() / ".hermes"


def resolve_root(hermes_root: Path | None) -> Path:
    """Use an explicit Hermes root or resolve the user's configured root."""
    return hermes_root or default_hermes_root() or Path.home() / ".hermes"


def _workflows_dir(hermes_root: Path) -> Path:
    return Path(hermes_root) / "swarm-workflows"


def _workflow_path(hermes_root: Path, workflow_id: str) -> Path:
    return _workflows_dir(hermes_root) / f"{workflow_id}.json"


def load_workflow(hermes_root: Path, workflow_id: str) -> dict[str, Any] | None:
    path = _workflow_path(hermes_root, workflow_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_workflow(hermes_root: Path, record: dict[str, Any]) -> None:
    path = _workflow_path(hermes_root, record["workflow_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Operational state file (like codex-jobs). Never contains raw bodies on
    # any surface; objective text is stored for contract rebuilds only.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def list_records(hermes_root: Path) -> list[dict[str, Any]]:
    root = _workflows_dir(hermes_root)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        for path in sorted(root.glob("sw-*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("schema") == WORKFLOW_SCHEMA:
                records.append(rec)
    except OSError:
        pass
    return records


def new_record(workflow: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    """Create a fresh workflow state record from a canonical workflow.

    The record keeps the full canonical workflow definition (used by the
    engine to rebuild stage contracts at dispatch/advance) plus per-stage
    runtime state. Objective text lives in the operational registry only —
    never on any surface (D-SW11) and never in the audit log.
    """
    stages_state: list[dict[str, Any]] = []
    for stage in workflow["stages"]:
        stages_state.append(
            {
                "id": stage["id"],
                "kind": stage.get("kind", "single"),
                "owner": stage["owner"],
                "parents": list(stage.get("parents") or []),
                "status": STAGE_STATUS_TODO,
                "task_id": "",
                "contract_sha256": "",
                "verdict": "",
                "rework_count": 0,
                "handoffs": [],
                "worktree_plan": None,
                "started_at": None,
                "ended_at": None,
                "blocked_reason": "",
            }
        )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": WORKFLOW_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow["workflow_id"],
        "title": workflow["title"],
        "workspace": workflow["workspace"],
        "project": workflow.get("project", {}),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "status": WORKFLOW_STATUS_RUNNING,
        "definition": workflow,
        "stages": stages_state,
        "approval": {
            "approved": False,
            "approved_by": "",
            "approval_reference": "",
            "approved_at": "",
        },
        "retention_note": RETENTION_NOTE,
        "created_at": now,
        "updated_at": now,
    }


def stage_state(record: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for stage in record.get("stages", []):
        if stage["id"] == stage_id:
            return stage
    return None


def workflow_stages_for_dispatch(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the stage definitions from the stored workflow definition.

    The record keeps the full canonical definition (``record["definition"]``)
    so the engine can rebuild M1 contracts at dispatch/advance with the real
    objective/artifacts/tests. Falls back to the runtime stage summary only
    if the definition is missing (defensive; should not happen).
    """
    definition = record.get("definition") or {}
    stages = definition.get("stages")
    if isinstance(stages, list) and stages:
        return stages
    out: list[dict[str, Any]] = []
    for st in record.get("stages", []):
        stage: dict[str, Any] = {
            "id": st["id"],
            "kind": st.get("kind", "single"),
            "owner": st["owner"],
            "parents": list(st.get("parents") or []),
            "objective": st.get("objective", ""),
            "expected_artifacts": st.get("expected_artifacts", []),
            "tests": st.get("tests", []),
            "forbidden_actions": st.get("forbidden_actions", []),
            "review_requirements": st.get("review_requirements"),
            "completion_criteria": st.get("completion_criteria"),
            "authorization": st.get("authorization"),
            "execution": st.get("execution"),
            "worktree": st.get("worktree"),
            "inputs": st.get("inputs", []),
            "constraints": st.get("constraints", []),
        }
        out.append(stage)
    return out
