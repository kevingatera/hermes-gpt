"""Validate Swarm workflows and build their Work Contract documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from hermes_gpt.execution import contract as contract_mod
from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.policy import authorization as op

# ---------------------------------------------------------------------------
# Workflow schema and lifecycle constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.6-sw.1"
WORKFLOW_SCHEMA = "hermes.swarm-workflow/v1"
CONTRACT_SCHEMA = contract_mod.CONTRACT_SCHEMA

# Identity regexes (reused from the fleet / contract layer).
_WORKFLOW_ID_RE = re.compile(r"^sw-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_AGENT_RE = op_fleet._AGENT_RE
_PROFILE_RE = op_fleet._PROFILE_RE
_TASK_ID_RE = op_fleet._TASK_ID_RE

# Stage / workflow lifecycle statuses (design §9).
STAGE_STATUS_TODO = "todo"
STAGE_STATUS_RUNNING = "running"
STAGE_STATUS_VALIDATED = "validated"
STAGE_STATUS_REWORK = "returned_for_rework"
STAGE_STATUS_BLOCKED = "blocked"
STAGE_STATUS_DONE = "done"
STAGE_STATUSES = (
    STAGE_STATUS_TODO,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_VALIDATED,
    STAGE_STATUS_REWORK,
    STAGE_STATUS_BLOCKED,
    STAGE_STATUS_DONE,
)

WORKFLOW_STATUS_RUNNING = "running"
WORKFLOW_STATUS_BLOCKED = "blocked"
WORKFLOW_STATUS_DONE = "done"
WORKFLOW_STATUS_AWAITING = "awaiting_approval"
WORKFLOW_STATUSES = (
    WORKFLOW_STATUS_RUNNING,
    WORKFLOW_STATUS_BLOCKED,
    WORKFLOW_STATUS_DONE,
    WORKFLOW_STATUS_AWAITING,
)

# D-SW3 caps (defaults; overridable per workflow and via env).
DEFAULT_MAX_PARALLEL = 3
DEFAULT_BOARD_CAP = 4
DEFAULT_MAX_STAGES = 12
ENV_MAX_PARALLEL = "HERMES_GPT_SWARM_MAX_PARALLEL"
ENV_BOARD_CAP = "HERMES_GPT_SWARM_BOARD_CAP"
ENV_MAX_STAGES = "HERMES_GPT_SWARM_MAX_STAGES"
HARD_MAX_PARALLEL = 8
HARD_BOARD_CAP = 16
HARD_MAX_STAGES = 64

# Bound on stage definitions (mirrors M1 limits).
_MAX_TITLE_BYTES = 500
_MAX_OBJECTIVE_BYTES = 8_000
_MAX_ARTIFACTS = 32
_MAX_TESTS = 16
_MAX_FORBIDDEN_ACTIONS = 32
_MAX_HANDOFFS = 64

# Retention note included in every workflow record (risk-review P2-2).
RETENTION_NOTE = (
    "Worktrees and Codex verdict/transcript artifacts persist for review; "
    "default cleans workflow worktrees and Codex job files after the release "
    "gate (operator_codex RETENTION_DAYS=30 for job files)."
)

# Worktree plan shape (NG5 / D-SW2). The engine computes the plan and passes
# the worktree path as the contract's allowed workspace; upstream kanban
# materializes it. Never call git from here.
WORKTREE_PROJECT_LINKED = "project-linked"
WORKTREE_PLAIN = "worktree"


def _env_cap(name: str, default: int, hard: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, hard))


def workflow_caps(workflow: dict[str, Any]) -> tuple[int, int, int]:
    """Return (max_parallel, board_cap, max_stages) for a workflow.

    Per-workflow document values override env defaults; env caps the hard
    bound. Design Q1: engine reads caps from config/env, not hard-coded.
    """
    max_parallel = int(
        workflow.get("max_parallel")
        or _env_cap(ENV_MAX_PARALLEL, DEFAULT_MAX_PARALLEL, HARD_MAX_PARALLEL)
    )
    board_cap = int(
        workflow.get("board_cap")
        or _env_cap(ENV_BOARD_CAP, DEFAULT_BOARD_CAP, HARD_BOARD_CAP)
    )
    max_stages = int(
        workflow.get("max_stages")
        or _env_cap(ENV_MAX_STAGES, DEFAULT_MAX_STAGES, HARD_MAX_STAGES)
    )
    return (
        max(1, min(max_parallel, HARD_MAX_PARALLEL)),
        max(1, min(board_cap, HARD_BOARD_CAP)),
        max(1, min(max_stages, HARD_MAX_STAGES)),
    )


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    return op_fleet._clean_text(value, field=field, maximum=maximum, required=required)


def _string_list(value: Any, *, field: str) -> list[str]:
    return op_fleet._string_list(value, field=field)


def _stage_id_list(value: Any, *, known: set[str], field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of stage ids")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _STAGE_ID_RE.fullmatch(item):
            raise ValueError(f"{field} contains an invalid stage id")
        if item not in known:
            raise ValueError(f"{field} references unknown stage {item!r}")
        if item in out:
            raise ValueError(f"{field} contains duplicate stage {item!r}")
        out.append(item)
    return out


def _validate_stage_defs(stages: Any, workflow: dict[str, Any]) -> None:
    """Validate the stage DAG: ids, owners, parents, cycles, caps."""
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty list")
    _, board_cap, max_stages = workflow_caps(workflow)
    if len(stages) > max_stages:
        raise ValueError(
            f"stage count {len(stages)} exceeds max_stages cap ({max_stages})"
        )

    ids: list[str] = []
    known: set[str] = set()
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stage[{idx}] must be an object")
        stage_id = _clean_text(stage.get("id"), field="stage id", maximum=64)
        if not _STAGE_ID_RE.fullmatch(stage_id):
            raise ValueError(f"stage id {stage_id!r} is invalid")
        if stage_id in known:
            raise ValueError(f"duplicate stage id {stage_id!r}")
        known.add(stage_id)
        ids.append(stage_id)

    # Second pass: validate owners/kinds/objectives/parents now that every
    # stage id is known (parents may reference later stages).
    for idx, stage in enumerate(stages):
        stage_id = stage["id"]
        kind = str(stage.get("kind", "single")).strip().lower()
        if kind not in ("single", "parallel", "approval"):
            raise ValueError(
                f"stage {stage_id!r} kind must be single|parallel|approval"
            )

        # D-SW4: exactly-one owner, required and immutable.
        owner = _clean_text(
            stage.get("owner"), field=f"stage {stage_id} owner", maximum=64
        )
        if not _AGENT_RE.fullmatch(owner) or not _PROFILE_RE.fullmatch(owner):
            raise ValueError(f"stage {stage_id!r} owner {owner!r} is invalid")
        if kind == "approval" and owner not in ("owner", "tony"):
            raise ValueError(f"approval stage {stage_id!r} owner must be 'owner'")

        parents = _stage_id_list(
            stage.get("parents") or [], known=known, field=f"stage {stage_id} parents"
        )

        objective = _clean_text(
            stage.get("objective"),
            field=f"stage {stage_id} objective",
            maximum=_MAX_OBJECTIVE_BYTES,
        )
        if not objective:
            raise ValueError(f"stage {stage_id!r} objective is required")

        # Parallel fan-out group bound (D-SW3): the group that fans out from a
        # common parent must not exceed the workflow's per-workflow cap.
        if kind == "parallel":
            key = tuple(sorted(parents))
            group = [
                s["id"]
                for s in stages
                if isinstance(s, dict) and tuple(sorted(s.get("parents") or [])) == key
            ]
            if len(group) > board_cap:
                raise ValueError(
                    f"parallel fan-out group {sorted(group)} exceeds board cap ({board_cap})"
                )

    # Acyclicity: topological sort (Kahn).
    indegree = {sid: 0 for sid in ids}
    edges: dict[str, list[str]] = {sid: [] for sid in ids}
    for stage in stages:
        for parent in stage.get("parents") or []:
            if parent == stage["id"]:
                raise ValueError(f"stage {stage['id']!r} cannot be its own parent")
            edges[parent].append(stage["id"])
            indegree[stage["id"]] += 1
    queue = [sid for sid in ids if indegree[sid] == 0]
    ordered: list[str] = []
    while queue:
        sid = queue.pop(0)
        ordered.append(sid)
        for nxt in edges[sid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(ids):
        cycle = sorted(sid for sid in ids if indegree[sid] > 0)
        raise ValueError(f"workflow DAG contains a cycle involving stages: {cycle}")


def _canonical_workflow(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a workflow document.

    Returns ``(canonical_json, workflow_dict)``. Raises ValueError /
    PermissionError on schema, DAG, or cap violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("workflow must be a JSON object")
    if raw.get("schema") != WORKFLOW_SCHEMA:
        raise ValueError(f"workflow schema must be {WORKFLOW_SCHEMA!r}")

    workflow_id = _clean_text(
        raw.get("workflow_id", ""), field="workflow_id", maximum=128, required=False
    )
    if workflow_id and not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise ValueError("workflow_id must match sw-<name> (letters/digits/_-)")
    title = _clean_text(raw.get("title"), field="title", maximum=_MAX_TITLE_BYTES)
    workspace = _clean_text(raw.get("workspace"), field="workspace", maximum=1000)
    if op.is_denied_path(workspace):
        raise PermissionError("workspace is denied by the secret-path policy")

    project_raw = raw.get("project") or {}
    project: dict[str, Any] = {}
    if isinstance(project_raw, dict):
        slug = _clean_text(
            project_raw.get("slug", ""),
            field="project.slug",
            maximum=128,
            required=False,
        )
        repo = _clean_text(
            project_raw.get("repo", ""),
            field="project.repo",
            maximum=1000,
            required=False,
        )
        if slug and repo:
            if op.is_denied_path(repo):
                raise PermissionError(
                    "project.repo is denied by the secret-path policy"
                )
            project = {"slug": slug, "repo": repo}

    stages = raw.get("stages")
    _validate_stage_defs(stages, raw)
    assert isinstance(stages, list)

    # Per-stage M1-contract shape validation happens lazily at dispatch
    # (the engine builds contracts then); workflow_validate can call
    # stage_contract for a full contract check. Here we bound + keep the
    # raw stage fields for the engine to build contracts from.
    cleaned_stages: list[dict[str, Any]] = []
    for stage in stages:
        cleaned = dict(stage)
        artifacts = stage.get("expected_artifacts") or []
        if not isinstance(artifacts, list) or len(artifacts) > _MAX_ARTIFACTS:
            raise ValueError("expected_artifacts must be a list (<= 32)")
        for art in artifacts:
            if not isinstance(art, dict):
                raise ValueError("expected artifact must be an object")
        cleaned["expected_artifacts"] = list(artifacts)

        tests = stage.get("tests") or []
        if not isinstance(tests, list) or len(tests) > _MAX_TESTS:
            raise ValueError("tests must be a list (<= 16)")
        for t in tests:
            if not isinstance(t, dict):
                raise ValueError("test must be an object")
        cleaned["tests"] = list(tests)

        forbidden = stage.get("forbidden_actions") or []
        if not isinstance(forbidden, list) or len(forbidden) > _MAX_FORBIDDEN_ACTIONS:
            raise ValueError("forbidden_actions must be a list (<= 32)")
        cleaned["forbidden_actions"] = list(forbidden)
        cleaned_stages.append(cleaned)

    workflow: dict[str, Any] = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": workflow_id,
        "title": title,
        "workspace": workspace,
        "project": project,
        "max_parallel": int(
            raw.get("max_parallel")
            or _env_cap(ENV_MAX_PARALLEL, DEFAULT_MAX_PARALLEL, HARD_MAX_PARALLEL)
        ),
        "board_cap": int(
            raw.get("board_cap")
            or _env_cap(ENV_BOARD_CAP, DEFAULT_BOARD_CAP, HARD_BOARD_CAP)
        ),
        "max_stages": int(
            raw.get("max_stages")
            or _env_cap(ENV_MAX_STAGES, DEFAULT_MAX_STAGES, HARD_MAX_STAGES)
        ),
        "stages": cleaned_stages,
    }
    canonical = json.dumps(
        workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(canonical.encode("utf-8")) > 64_000:
        raise ValueError("canonical workflow exceeds 64 KB")
    return canonical, workflow


def _workflow_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def parse_workflow(workflow_json: str) -> tuple[str, dict[str, Any], str]:
    """Parse + canonicalize a workflow JSON string.

    Returns ``(canonical_json, workflow, workflow_sha256)``.
    """
    if not isinstance(workflow_json, str) or not workflow_json.strip():
        raise ValueError("workflow_json is required")
    try:
        raw = json.loads(workflow_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"workflow_json is not valid JSON: {exc}") from exc
    canonical, workflow = _canonical_workflow(raw)
    return canonical, workflow, _workflow_sha256(canonical)


def _workflow_id(workflow: dict[str, Any]) -> str:
    return workflow["workflow_id"]


def worktree_plan(
    workflow: dict[str, Any], stage: dict[str, Any], task_id: str
) -> dict[str, Any] | None:
    """Compute the upstream kanban worktree plan for a stage, or None.

    Mirrors upstream Hermes kanban: project-linked tasks anchor under the
    project's primary repo as ``<repo>/.worktrees/<task-id>`` on branch
    ``<slug>/<task-id>``; plain worktree tasks use ``wt/<task-id>``.
    """
    stage_worktree = stage.get("worktree")
    if not isinstance(stage_worktree, dict) or not stage_worktree.get("enabled"):
        return None
    project = workflow.get("project") or {}
    if project.get("slug") and project.get("repo"):
        branch = f"{project['slug']}/{task_id}"
        path = str(Path(project["repo"]) / ".worktrees" / task_id)
        kind = WORKTREE_PROJECT_LINKED
    else:
        branch = f"wt/{task_id}"
        path = str(Path(workflow.get("workspace", ".")) / ".worktrees" / task_id)
        kind = WORKTREE_PLAIN
    return {"kind": kind, "task_id": task_id, "branch": branch, "path": path}


def stage_contract(
    workflow: dict[str, Any], stage: dict[str, Any], *, task_id: str | None = None
) -> dict[str, Any]:
    """Build the M1 contract document for a stage (design §6)."""
    stage_id = stage["id"]
    if task_id is None:
        task_id = f"{workflow['workflow_id']}-{stage_id}"
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"generated task_id {task_id!r} is invalid")

    workspace = workflow["workspace"]
    # Implementation stages run inside their planned worktree (NG5); others
    # use the workflow workspace as allowed scope.
    plan = worktree_plan(workflow, stage, task_id)
    allowed_workspaces = [plan["path"]] if plan else [workspace]

    artifacts = list(stage.get("expected_artifacts") or [])
    tests = list(stage.get("tests") or [])
    forbidden = list(stage.get("forbidden_actions") or [])
    review = stage.get("review_requirements") or {
        "required": False,
        "reviewer": "",
        "evidence": "",
        "approval_required": False,
    }
    criteria = stage.get("completion_criteria") or {
        "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
        "artifacts_present": bool(artifacts),
        "tests_pass": bool(tests),
        "review_satisfied": bool(review.get("required", False)),
        "no_forbidden_actions": True,
    }
    auth = stage.get("authorization") or {
        "class": "reversible_write",
        "approved": True,
        "approved_by": "Tony",
        "approval_reference": "G4-swarm",
    }

    execution = stage.get("execution")
    assigned_agent = (
        "auto"
        if isinstance(execution, dict)
        and str(execution.get("backend") or "").strip().lower() == "auto"
        else stage["owner"]
    )

    contract = {
        "schema": CONTRACT_SCHEMA,
        "task_id": task_id,
        # Auto placement owns the execution target while the stage owner remains
        # the assigned profile/authority principal. Keeping these concepts
        # separate lets Swarm compose with Fabric without silently pinning an
        # auto-routed stage back to its orchestration owner.
        "assigned_agent": assigned_agent,
        "assigned_profile": stage["owner"],
        "objective": stage["objective"],
        "allowed_scope": {
            "workspaces": allowed_workspaces,
            "profiles": [stage["owner"]],
        },
        "forbidden_actions": forbidden,
        "expected_artifacts": artifacts,
        "tests": tests,
        "review_requirements": review,
        "completion_criteria": criteria,
        "inputs": _string_list(stage.get("inputs") or [], field="inputs"),
        "constraints": _string_list(
            stage.get("constraints") or [], field="constraints"
        ),
        "authorization": auth,
    }
    capability_req = stage.get("capability_req")
    if capability_req is not None:
        if not isinstance(capability_req, dict):
            raise TypeError("stage capability_req must be an object")
        contract["capability_req"] = dict(capability_req)
    if execution is not None:
        contract["execution"] = execution
    return contract


def stage_contract_sha(contract: dict[str, Any]) -> str:
    try:
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
        return sha
    except (ValueError, PermissionError):
        return ""
