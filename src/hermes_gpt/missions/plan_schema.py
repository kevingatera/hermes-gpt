"""Validation, canonicalization, and deterministic MissionPlan decomposition."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op
from hermes_gpt.execution.swarm_workflows import CANONICAL_STAGE_SPECS, DEFAULT_OWNERS

SCHEMA_VERSION = "0.9-plan.1"
PLAN_SCHEMA = "hermes.mission-plan/v1"
NODE_SCHEMA = "hermes.plan-node/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")

KIND_SINGLE = "single"
KIND_PARALLEL = "parallel"
KIND_APPROVAL = "approval"
KINDS = (KIND_SINGLE, KIND_PARALLEL, KIND_APPROVAL)

MAX_NODES = 64
MAX_DEPTH_DEPENDS = 8
MAX_OBJECTIVE_BYTES = 8_000
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BASENAME = 200
MAX_SKILLS = 32
MAX_SKILL_NAME = 128
MAX_BUDGET_MINUTES = 100_000
MAX_BUDGET_TOKENS = 100_000_000
MAX_DECOMP = 64
AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")

# Plan-level review status (operator-reviewable; distinct from a Mission status).
PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_REVIEW = "review"
PLAN_STATUS_APPROVED = "approved"
PLAN_STATUS_REJECTED = "rejected"
PLAN_STATUSES = (
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_REVIEW,
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_REJECTED,
)

# Plan node state machine (design §5.2).
NODE_STATES = (
    "pending",
    "blockable",
    "dispatched",
    "running",
    "awaiting_review",
    "validated",
    "awaiting_approval",
    "completed",
    "failed",
    "paused",
)
NODE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"blockable", "dispatched", "failed", "paused"},
    "blockable": {"dispatched", "failed", "paused"},
    "dispatched": {"running", "failed", "paused"},
    "running": {"awaiting_review", "failed", "paused"},
    "awaiting_review": {"validated", "failed", "paused"},
    "validated": {"awaiting_approval", "failed", "paused"},
    "awaiting_approval": {"completed", "failed", "paused"},
    "completed": set(),
    "failed": set(),
    "paused": {"running", "blockable", "failed"},
}
TERMINAL_NODE_STATES = frozenset({"completed", "failed"})


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _objective_meta(text: str | None) -> dict[str, Any]:
    """Redaction metadata for an objective; never the raw text (INV-9)."""
    if not text:
        return {"objective_len": 0, "objective_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "objective_len": len(data),
        "objective_sha256": hashlib.sha256(data).hexdigest(),
    }


def _clean_profile(value: Any, *, field: str, allow_owner: bool = False) -> str:
    name = _clean_text(value, field=field, maximum=64, required=True)
    if allow_owner and name in ("owner", "tony"):
        return name
    return op.validate_profile_name(name)


def _clean_skills(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SKILLS:
        raise ValueError(f"capability_req.skills must be a list (<= {MAX_SKILLS})")
    out: list[str] = []
    for item in value:
        name = _clean_text(
            item, field="capability skill", maximum=MAX_SKILL_NAME, required=True
        )
        if name in out:
            raise ValueError(f"duplicate capability skill {name!r}")
        out.append(name)
    return out


def _clean_capability_req(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("capability_req must be an object")  # noqa: TRY004 -- validation handlers serialize shape errors as ValueError.
    profile = _clean_profile(value.get("profile"), field="capability_req.profile")
    skills = _clean_skills(value.get("skills"))
    klass = str(value.get("authorization_class", "reversible_write")).strip().lower()
    if klass not in AUTH_CLASSES:
        raise ValueError(
            f"capability_req.authorization_class must be one of {list(AUTH_CLASSES)}"
        )
    return {"profile": profile, "skills": skills, "authorization_class": klass}


def _clean_budget(value: Any) -> dict[str, int]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("budget must be an object")  # noqa: TRY004 -- validation handlers serialize shape errors as ValueError.
    minutes = int(value.get("est_minutes", 0) or 0)
    tokens = int(value.get("est_tokens", 0) or 0)
    if minutes < 0 or minutes > MAX_BUDGET_MINUTES:
        raise ValueError(f"budget.est_minutes out of range (0..{MAX_BUDGET_MINUTES})")
    if tokens < 0 or tokens > MAX_BUDGET_TOKENS:
        raise ValueError(f"budget.est_tokens out of range (0..{MAX_BUDGET_TOKENS})")
    return {"est_minutes": minutes, "est_tokens": tokens}


def _clean_artifacts(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS:
        raise ValueError(f"expected_artifacts must be a list (<= {MAX_ARTIFACTS})")
    out: list[str] = []
    for item in value:
        name = _clean_text(
            item,
            field="expected artifact",
            maximum=MAX_ARTIFACT_BASENAME,
            required=True,
        )
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"expected artifact {name!r} must be a basename")
        out.append(name)
    return out


def _node_contract_signature(node: dict[str, Any]) -> str:
    """Deterministic contract signature for a node (a hash, never raw content)."""
    skeleton = {
        "node_id": node["node_id"],
        "kind": node["kind"],
        "owner": node["owner"],
        "parents": node["parents"],
        "objective_sha256": node["objective_sha256"],
        "capability_req": node["capability_req"],
        "budget": node["budget"],
        "expected_artifacts": node["expected_artifacts"],
    }
    enc = json.dumps(
        skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def _validate_node_dag(nodes: list[dict[str, Any]], *, uuid: set[str]) -> None:
    """Validate node ids, deps known, no self/dupe, and acyclicity (Kahn)."""
    for idx, node in enumerate(nodes):
        node_id = node["node_id"]
        parents = node["parents"]
        for parent in parents:
            if parent not in uuid:
                raise ValueError(
                    f"node {node_id!r} references unknown parent {parent!r}"
                )
    indegree = {n["node_id"]: 0 for n in nodes}
    edges: dict[str, list[str]] = {n["node_id"]: [] for n in nodes}
    for node in nodes:
        for parent in node["parents"]:
            if parent == node["node_id"]:
                raise ValueError(f"node {node['node_id']!r} cannot be its own parent")
            edges[parent].append(node["node_id"])
            indegree[node["node_id"]] += 1
    queue = [nid for nid in indegree if indegree[nid] == 0]
    ordered: list[str] = []
    while queue:
        cur = queue.pop(0)
        ordered.append(cur)
        for nxt in edges[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(nodes):
        cycle = sorted(nid for nid in indegree if indegree[nid] > 0)
        raise ValueError(f"plan DAG contains a cycle involving nodes: {cycle}")


def _canonical_node(raw: Any) -> dict[str, Any]:
    """Validate + canonicalize one plan node (definition only — no state)."""
    if not isinstance(raw, dict):
        raise ValueError("each plan node must be an object")  # noqa: TRY004 -- validation handlers serialize shape errors as ValueError.
    node_id = _clean_text(
        raw.get("node_id"), field="node_id", maximum=64, required=True
    )
    if not NODE_ID_RE.fullmatch(node_id):
        raise ValueError(f"node_id {node_id!r} is invalid")
    kind = str(raw.get("kind", KIND_SINGLE)).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"node {node_id!r} kind must be one of {list(KINDS)}")
    owner = _clean_profile(
        raw.get("owner"),
        field=f"node {node_id} owner",
        allow_owner=(kind == KIND_APPROVAL),
    )
    if kind == KIND_APPROVAL and owner not in ("owner", "tony"):
        raise ValueError(f"approval node {node_id!r} owner must be 'owner' or 'tony'")

    parents = raw.get("parents") or []
    if not isinstance(parents, list) or len(parents) > MAX_DEPTH_DEPENDS:
        raise ValueError(
            f"node {node_id!r} parents must be a list (<= {MAX_DEPTH_DEPENDS})"
        )
    parents = [
        _clean_text(p, field=f"node {node_id} parent", maximum=64, required=True)
        for p in parents
    ]
    for p in parents:
        if not NODE_ID_RE.fullmatch(p):
            raise ValueError(f"node {node_id!r} has invalid parent {p!r}")
    if len(set(parents)) != len(parents):
        raise ValueError(f"node {node_id!r} parents contain duplicates")

    # INV-9: never store the raw objective. If raw text is present, hash it;
    # if only hash metadata is present (an already-canonical node), preserve it.
    if raw.get("objective") is not None:
        objective_meta = _objective_meta(raw["objective"])
    else:
        o_sha = _clean_text(
            raw.get("objective_sha256"),
            field=f"node {node_id} objective_sha256",
            maximum=64,
        )
        if o_sha:
            if not SHA_RE.fullmatch(o_sha):
                raise ValueError(
                    f"node {node_id!r} objective_sha256 must be lowercase SHA-256"
                )
            objective_meta = {
                "objective_len": int(raw.get("objective_len") or 0),
                "objective_sha256": o_sha,
            }
        else:
            objective_meta = {"objective_len": 0, "objective_sha256": ""}
    cap = _clean_capability_req(raw.get("capability_req"))
    budget = _clean_budget(raw.get("budget"))
    artifacts = _clean_artifacts(raw.get("expected_artifacts"))
    if kind == KIND_APPROVAL and artifacts:
        raise ValueError(
            f"approval node {node_id!r} must not declare expected_artifacts"
        )

    contract_ref = _clean_text(
        raw.get("contract_ref"), field=f"node {node_id} contract_ref", maximum=256
    )
    if contract_ref and not REF_RE.fullmatch(contract_ref):
        raise ValueError(
            f"node {node_id!r} contract_ref contains unsupported characters"
        )
    contract_sha = _clean_text(
        raw.get("contract_sha256"), field=f"node {node_id} contract_sha256", maximum=64
    )
    if contract_sha and not SHA_RE.fullmatch(contract_sha):
        raise ValueError(f"node {node_id!r} contract_sha256 must be lowercase SHA-256")

    node: dict[str, Any] = {
        "schema": NODE_SCHEMA,
        "node_id": node_id,
        "kind": kind,
        "owner": owner,
        "parents": parents,
        **objective_meta,
        "contract_ref": contract_ref,
        "contract_sha256": contract_sha
        or _node_contract_signature(
            {
                "node_id": node_id,
                "kind": kind,
                "owner": owner,
                "parents": parents,
                "objective_sha256": objective_meta["objective_sha256"],
                "capability_req": cap,
                "budget": budget,
                "expected_artifacts": artifacts,
            }
        ),
        "capability_req": cap,
        "budget": budget,
        "expected_artifacts": artifacts,
    }
    return node


def _canonical_plan(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a MissionPlan document.

    Returns ``(canonical_json, plan_dict)``. Raises ValueError / PermissionError
    on schema, DAG, cap, or containment violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")  # noqa: TRY004 -- validation handlers serialize shape errors as ValueError.
    if raw.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA!r}")

    mission_id = _clean_text(
        raw.get("mission_id"), field="mission_id", maximum=68, required=True
    )
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    version = int(raw.get("version", 1) or 1)
    if version < 1:
        raise ValueError("plan version must be >= 1")
    decomposition = _clean_text(
        raw.get("decomposition") or "operator-provided",
        field="decomposition",
        maximum=MAX_DECOMP,
    )

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError("plan nodes must be a non-empty list")
    if len(nodes_raw) > MAX_NODES:
        raise ValueError(f"plan node count {len(nodes_raw)} exceeds cap ({MAX_NODES})")

    if raw.get("objective") is not None:
        mission_meta = _objective_meta(raw["objective"])
    else:
        o_sha = _clean_text(
            raw.get("objective_sha256"), field="objective_sha256", maximum=64
        )
        if o_sha:
            if not SHA_RE.fullmatch(o_sha):
                raise ValueError("objective_sha256 must be lowercase SHA-256")
            mission_meta = {
                "objective_len": int(raw.get("objective_len") or 0),
                "objective_sha256": o_sha,
            }
        else:
            mission_meta = {"objective_len": 0, "objective_sha256": ""}
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(nodes_raw):
        node = _canonical_node(item)
        if node["node_id"] in seen:
            raise ValueError(f"duplicate node id {node['node_id']!r}")
        seen.add(node["node_id"])
        nodes.append(node)
    _validate_node_dag(nodes, uuid=seen)

    canonical: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mission_id": mission_id,
        "version": version,
        "decomposition": decomposition,
        **mission_meta,
        "nodes": nodes,
    }
    # INV-9: a stored plan must never carry a raw objective / prompt-like field.
    if op.redact_output(json.dumps(canonical)) != json.dumps(canonical):
        raise PermissionError("plan contains secret-like durable values")
    enc = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(enc.encode("utf-8")) > 64_000:
        raise ValueError("canonical plan exceeds 64 KB")
    return enc, canonical


def _plan_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_plan(plan_json: str) -> tuple[str, dict[str, Any], str]:
    if not isinstance(plan_json, str) or not plan_json.strip():
        raise ValueError("plan_json is required")
    try:
        raw = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc
    canonical, plan = _canonical_plan(raw)
    return canonical, plan, _plan_sha256(canonical)


def decompose_mission(
    mission_id: str,
    *,
    objective: str = "",
    owner_profile: str = "default",
    use_canonical: bool = True,
) -> dict[str, Any]:
    """Deterministically decompose a MissionSpec into a bounded plan DAG.

    Reuses the canonical Swarm stage shape (research -> architecture ->
    implementation/tests/docs -> integration_review -> codex_review ->
    acceptance_validation -> human_approval) and Work Contract refs. No LLM is
    used; each node carries bounded metadata and hashes only (INV-9).
    """
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    if not use_canonical:
        raise ValueError(
            "decomposition requires use_canonical=True; an operator-provided plan is accepted via plan_create"
        )

    mission_objective = _objective_meta(objective)
    nodes: list[dict[str, Any]] = []
    for stage_id, kind, parents in CANONICAL_STAGE_SPECS:
        if len(nodes) >= MAX_NODES:
            break
        owner = DEFAULT_OWNERS.get(stage_id, owner_profile)
        if kind == KIND_APPROVAL:
            owner = "owner"
        default_budget = _default_budget(kind)
        node_raw = {
            "node_id": stage_id,
            "kind": kind,
            "owner": owner,
            "parents": list(parents),
            "objective": f"{stage_id.replace('_', ' ')} stage",
            "capability_req": {
                "profile": owner if kind != KIND_APPROVAL else "owner",
                "skills": [],
                "authorization_class": "high_impact"
                if kind == KIND_APPROVAL
                else "reversible_write",
            },
            "budget": default_budget,
            "expected_artifacts": []
            if kind == KIND_APPROVAL
            else _default_artifacts(stage_id),
            "contract_sha256": "",
            "contract_ref": f"contract:{stage_id}",
        }
        nodes.append(_canonical_node(node_raw))
    # The canonical nodes carry redaction metadata from the stage objective; the
    # mission-level objective is attached separately.
    raw: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mission_id": mission_id,
        "version": 1,
        "decomposition": "canonical-swarm-v1",
        "objective": objective,
        "nodes": nodes,
    }
    canonical, plan = _canonical_plan(raw)
    # Recompute mission/plan objective metadata from the raw input objective.
    plan["objective_len"] = mission_objective["objective_len"]
    plan["objective_sha256"] = mission_objective["objective_sha256"]
    plan["node_count"] = len(plan["nodes"])
    plan["plan_sha256"] = _plan_sha256(canonical)
    # Remove raw objective from the returned plan (it is redacted in canonical).
    plan.pop("objective", None)
    return plan


def _default_budget(kind: str) -> dict[str, int]:
    if kind == KIND_APPROVAL:
        return {"est_minutes": 0, "est_tokens": 0}
    if kind == KIND_PARALLEL or kind == KIND_SINGLE:
        return {"est_minutes": 60, "est_tokens": 200_000}
    return {"est_minutes": 30, "est_tokens": 50_000}


def _default_artifacts(stage_id: str) -> list[str]:
    return ["work-contract.json", "evidence.json"]


def validate_node_transition(from_state: str, to_state: str) -> bool:
    """Return whether ``from_state -> to_state`` is a legal §5.2 transition."""
    return from_state in NODE_TRANSITIONS and to_state in NODE_TRANSITIONS[from_state]
