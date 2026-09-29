"""Bounded Work Contract surfaces and read-only evidence collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import operator_mission_work as mission_work
import operator_policy as op
import operator_runners as op_runners
from operator_contract_schema import (
    _MAX_REVIEW_EVIDENCE_SCAN,
    _prompt_meta,
    _resolve_root,
)


def _surface_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Redact a canonical contract for surfaces.

    - objective -> ``{prompt_len, prompt_sha256}``
    - forbidden actions -> ``{action, class}`` (+ redacted reason)
    - artifacts -> basename + must_exist + min_bytes
    """
    surf = dict(contract)
    surf["objective"] = _prompt_meta(contract.get("objective"))
    surf["forbidden_actions"] = [
        {
            "action": fa["action"],
            "class": fa["class"],
            "reason": op.redact_output(fa.get("reason", "")),
        }
        for fa in contract.get("forbidden_actions", [])
    ]
    surf["expected_artifacts"] = [
        {
            "basename": Path(a["path"]).name,
            "path": a["path"],
            "must_exist": a["must_exist"],
            "min_bytes": a["min_bytes"],
        }
        for a in contract.get("expected_artifacts", [])
    ]
    if isinstance(contract.get("execution"), dict):
        options = contract["execution"].get("options") or {}
        surf["execution"] = {
            "backend": contract["execution"].get("backend"),
            "option_keys": sorted(str(k) for k in options.keys()),
        }
    return surf


def _observed_kanban_runs(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    warnings: list[str] = []
    try:
        runs = mission_work._kanban_runs_for(hermes_root, warnings)
    except Exception:
        return []
    return [
        {
            "task_id": r.get("task_id"),
            "board": r.get("board"),
            "assignee": r.get("assignee"),
            "status": r.get("status"),
            "outcome": r.get("outcome"),
            "error": r.get("error"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
        }
        for r in runs
        if r.get("task_id") == task_id
    ]


def _observed_delegations(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    for profile in op.list_existing_profiles(hermes_root):
        try:
            home = op.resolve_profile_home(profile, hermes_root)
            for d in mission_work._async_delegations_for(home, warnings, profile):
                if d.get("delegation_id") == task_id:
                    out.append(
                        {
                            "delegation_id": d.get("delegation_id"),
                            "state": d.get("state"),
                            "dispatched_at": d.get("dispatched_at"),
                            "completed_at": d.get("completed_at"),
                            "scope": d.get("scope"),
                        }
                    )
        except Exception:
            continue
    return out


def _observed_runs(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    """All observed run/outcome records for a task id.

    Existing Mission Control sources remain authoritative for legacy/fleet work;
    pluggable runner jobs contribute their own bounded durable state.
    """
    return (
        _observed_kanban_runs(task_id, hermes_root)
        + _observed_delegations(task_id, hermes_root)
        + op_runners.observed_runs(task_id, hermes_root=hermes_root)
    )


def _observed_audit(
    hermes_root: Path,
    limit: int = _MAX_REVIEW_EVIDENCE_SCAN,
) -> list[dict[str, Any]]:
    """Read audit evidence only from the selected Hermes root."""
    log_path = _resolve_root(hermes_root) / "logs" / "hermes_gpt_operator_audit.jsonl"
    records: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records[-limit:] if limit > 0 else records
