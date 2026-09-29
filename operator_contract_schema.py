"""Work Contract schema normalization and durable validation manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import operator_command_utils as op_command
import operator_fleet as op_fleet
import operator_policy as op
import operator_runners as op_runners
import operator_workspace_files as op_workspace_files

SCHEMA_VERSION = "0.6-wc.1"

CONTRACT_SCHEMA = "hermes.work-contract/v1"

_AGENT_RE = op_fleet._AGENT_RE

_PROFILE_RE = op_fleet._PROFILE_RE

_TASK_ID_RE = op_fleet._TASK_ID_RE

_FORBIDDEN_CLASSES = frozenset({"LOW", "MED", "HIGH"})

_AUTH_CLASSES = op_fleet._AUTH_CLASSES

_MAX_OBJECTIVE_BYTES = 8_000

_MAX_ARTIFACTS = 32

_MAX_TESTS = 16

_MAX_FORBIDDEN_ACTIONS = 32

_MAX_SCOPE_WORKSPACES = 8

_MAX_SCOPE_PROFILES = 16

_MAX_CAPABILITY_SKILLS = 32

_MAX_SKILL_NAME = 128

_MAX_REVIEW_EVIDENCE_SCAN = 500

_VERDICT_SATISFIED = "SATISFIED"

_VERDICT_NOT_SATISFIED = "NOT_SATISFIED"

_VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

_VERDICT_INVALID = "INVALID_CONTRACT"

_CHECK_KINDS = (
    "run_state",
    "artifacts",
    "tests",
    "review",
    "forbidden",
    "authorization",
)


def _default_hermes_root() -> Path | None:
    """Return the default Hermes data root (mirrors operator_mission)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def _resolve_root(hermes_root: Path | None) -> Path:
    return hermes_root or _default_hermes_root() or Path.home() / ".hermes"


def _contract_error(
    *,
    code: str,
    safe_message: str,
    suggested_action: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = op.make_error_envelope(
        layer="operator",
        code=code,
        safe_message=safe_message,
        suggested_action=suggested_action,
        trace_id=trace_id,
        extra=extra,
    )
    env["schema_version"] = SCHEMA_VERSION
    return env


def _prompt_meta(text: str | None) -> dict[str, Any]:
    """Return ``{prompt_len, prompt_sha256}`` for a contract objective."""
    if text is None:
        return {"prompt_len": 0, "prompt_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "prompt_len": len(data),
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
    }


def _truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _audit_call(
    *,
    tool: str,
    dry_run: bool,
    success: bool,
    changed: bool,
    summary: str,
    contract_sha256: str = "",
    task_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a contract call in the operator audit log (D9).

    Never includes the objective text; only ``contract_sha256`` + ``task_id``.
    """
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=bool(dry_run),
            success=bool(success),
            changed=bool(changed),
            summary=_truncate(summary, 500),
            extra={
                "contract_sha256": contract_sha256,
                "task_id": task_id,
                **(extra or {}),
            },
        )
    except Exception:
        pass


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    """Bound + strip a text field (mirrors operator_fleet._clean_text)."""
    return op_fleet._clean_text(value, field=field, maximum=maximum, required=required)


def _string_list(value: Any, *, field: str) -> list[str]:
    return op_fleet._string_list(value, field=field)


def _workspace_list(value: Any) -> list[Path]:
    """Validate allowed_scope.workspaces: absolute, non-denied paths."""
    if not isinstance(value, list) or not value or len(value) > _MAX_SCOPE_WORKSPACES:
        raise ValueError("allowed_scope.workspaces must be a non-empty list (<= 8)")
    workspaces: list[Path] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("workspace must be a non-empty path string")
        p = op._normalize_path(item)
        if op.is_denied_path(p):
            raise PermissionError(
                f"workspace {item!r} is denied by the secret-path policy"
            )
        if not p.is_absolute():
            raise ValueError(f"workspace {item!r} must be an absolute path")
        workspaces.append(p)
    return workspaces


def _profile_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_SCOPE_PROFILES:
        raise ValueError("allowed_scope.profiles must be a non-empty list (<= 16)")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("profile must be a string")
        out.append(op.validate_profile_name(item))
    return out


def _capability_requirement(
    value: Any, *, assigned_profile: str
) -> dict[str, Any] | None:
    """Normalize optional profile-local skills carried through dispatch."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("capability_req must be an object")  # noqa: TRY004 -- contract schema failures use ValueError envelopes.
    profile = op.validate_profile_name(
        _clean_text(value.get("profile"), field="capability_req.profile", maximum=64)
    )
    if profile != assigned_profile:
        raise ValueError("capability_req.profile must match assigned_profile")
    skills = value.get("skills") or []
    if not isinstance(skills, list) or len(skills) > _MAX_CAPABILITY_SKILLS:
        raise ValueError(
            f"capability_req.skills must be a list (<= {_MAX_CAPABILITY_SKILLS})"
        )
    normalized: list[str] = []
    for item in skills:
        name = _clean_text(item, field="capability skill", maximum=_MAX_SKILL_NAME)
        if op.redact_output(name) != name:
            raise PermissionError("capability skill name contains secret-like data")
        if name in normalized:
            raise ValueError(f"duplicate capability skill {name!r}")
        normalized.append(name)
    return {"profile": profile, "skills": normalized}


def _forbidden_list(value: Any) -> list[dict[str, Any]]:
    """Normalize forbidden_actions to ``{action, reason, class}`` (LOW/MED/HIGH)."""
    if not isinstance(value, list) or len(value) > _MAX_FORBIDDEN_ACTIONS:
        raise ValueError(
            f"forbidden_actions must be a list (<= {_MAX_FORBIDDEN_ACTIONS})"
        )
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("forbidden action must be an object")
        action = _clean_text(item.get("action"), field="forbidden action", maximum=128)
        reason = _clean_text(
            item.get("reason", ""),
            field="forbidden reason",
            maximum=500,
            required=False,
        )
        klass = str(item.get("class", "HIGH")).upper()
        if klass not in _FORBIDDEN_CLASSES:
            raise ValueError(
                f"forbidden action class must be one of {sorted(_FORBIDDEN_CLASSES)}"
            )
        out.append({"action": action, "reason": reason, "class": klass})
    return out


def _resolve_artifact_paths(path: str, workspaces: list[Path]) -> list[Path]:
    """Resolve a (possibly relative) artifact path against every workspace.

    No ``..`` escaping: the resolved path must remain under its workspace.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("artifact path must be a non-empty string")
    resolved: list[Path] = []
    for ws in workspaces:
        p = Path(path)
        if not p.is_absolute():
            p = ws / p
        r = p.resolve()
        if op.is_denied_path(r):
            raise PermissionError(
                f"artifact path {path!r} is denied by the secret-path policy"
            )
        if not op.path_under_allowed(r, [ws]):
            raise ValueError(f"artifact path {path!r} escapes its workspace")
        resolved.append(r)
    return resolved


def _artifact_list(value: Any, workspaces: list[Path]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _MAX_ARTIFACTS:
        raise ValueError(f"expected_artifacts must be a list (<= {_MAX_ARTIFACTS})")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected artifact must be an object")
        path = _clean_text(item.get("path"), field="artifact path", maximum=1000)
        _resolve_artifact_paths(path, workspaces)  # validates no escape / no denied
        must_exist = bool(item.get("must_exist", True))
        min_bytes = int(item.get("min_bytes", 0))
        if min_bytes < 0:
            raise ValueError("min_bytes must be >= 0")
        out.append({"path": path, "must_exist": must_exist, "min_bytes": min_bytes})
    return out


def _test_list(value: Any) -> list[dict[str, Any]]:
    """Validate tests against the workspace allowlist (D6)."""
    if not isinstance(value, list) or len(value) > _MAX_TESTS:
        raise ValueError(f"tests must be a list (<= {_MAX_TESTS})")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("test must be an object")
        name = _clean_text(item.get("name"), field="test name", maximum=200)
        command = _clean_text(item.get("command"), field="test command", maximum=1000)
        try:
            argv = op_command._split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"test command is not parseable: {exc}") from exc
        allowed, reason = op_workspace_files._is_allowed_test_command(argv)
        if not allowed:
            raise ValueError(f"test {name!r} is not in the allowlist: {reason}")
        workdir = _clean_text(
            item.get("workdir", ""), field="test workdir", maximum=1000, required=False
        )
        out.append({"name": name, "command": command, "workdir": workdir})
    return out


def _review_requirements(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "required": False,
            "reviewer": "",
            "evidence": "",
            "approval_required": False,
        }
    if not isinstance(value, dict):
        raise ValueError("review_requirements must be an object")
    required = bool(value.get("required", False))
    reviewer = _clean_text(
        value.get("reviewer", ""), field="reviewer", maximum=128, required=False
    )
    evidence = _clean_text(
        value.get("evidence", ""), field="review evidence", maximum=500, required=False
    )
    approval_required = bool(value.get("approval_required", required))
    return {
        "required": required,
        "reviewer": reviewer,
        "evidence": evidence,
        "approval_required": approval_required,
    }


def _completion_criteria(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("completion_criteria must be an object")
    run_state = value.get("run_state")
    if not isinstance(run_state, dict):
        raise ValueError("completion_criteria.run_state must be an object")
    outcome_ok = run_state.get("outcome_ok")
    if (
        not isinstance(outcome_ok, list)
        or not outcome_ok
        or any(not isinstance(x, str) for x in outcome_ok)
    ):
        raise ValueError(
            "completion_criteria.run_state.outcome_ok must be a non-empty string list"
        )
    terminal = bool(run_state.get("terminal", True))
    return {
        "run_state": {"terminal": terminal, "outcome_ok": [str(x) for x in outcome_ok]},
        "artifacts_present": bool(value.get("artifacts_present", True)),
        "tests_pass": bool(value.get("tests_pass", False)),
        "review_satisfied": bool(value.get("review_satisfied", False)),
        "no_forbidden_actions": bool(value.get("no_forbidden_actions", True)),
    }


def _canonical_contract(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a contract document.

    Returns ``(canonical_json, contract_dict)``. Raises ValueError /
    PermissionError on schema, scope, or authorization violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("contract must be a JSON object")
    if raw.get("schema") != CONTRACT_SCHEMA:
        raise ValueError(f"contract schema must be {CONTRACT_SCHEMA!r}")

    task_id = _clean_text(raw.get("task_id"), field="task_id", maximum=128)
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    assigned_agent = _clean_text(
        raw.get("assigned_agent"), field="assigned_agent", maximum=64
    )
    if not _AGENT_RE.fullmatch(assigned_agent):
        raise ValueError("assigned_agent is invalid (must be a fleet peer name)")
    assigned_profile = _clean_text(
        raw.get("assigned_profile"), field="assigned_profile", maximum=64
    )
    if not _PROFILE_RE.fullmatch(assigned_profile):
        raise ValueError("assigned_profile is invalid")
    capability_req = _capability_requirement(
        raw.get("capability_req"), assigned_profile=assigned_profile
    )
    objective = _clean_text(
        raw.get("objective"), field="objective", maximum=_MAX_OBJECTIVE_BYTES
    )

    scope = raw.get("allowed_scope")
    if not isinstance(scope, dict):
        raise ValueError("allowed_scope must be an object")
    workspaces = _workspace_list(scope.get("workspaces"))
    profiles = _profile_list(scope.get("profiles"))

    forbidden = _forbidden_list(raw.get("forbidden_actions") or [])
    artifacts = _artifact_list(raw.get("expected_artifacts") or [], workspaces)
    tests = _test_list(raw.get("tests") or [])
    review = _review_requirements(raw.get("review_requirements"))
    criteria = _completion_criteria(raw.get("completion_criteria"))

    # Authorization is the fleet authorization metadata (class/approved/approved_by).
    auth_value = raw.get("authorization")
    if auth_value is None and isinstance(raw.get("completion_criteria"), dict):
        auth_value = raw["completion_criteria"].get("authorization")
    authorization = op_fleet._authorization(auth_value)
    execution = op_runners.normalize_execution(raw.get("execution"))

    contract: dict[str, Any] = {
        "schema": CONTRACT_SCHEMA,
        "task_id": task_id,
        "assigned_agent": assigned_agent,
        "assigned_profile": assigned_profile,
        "objective": objective,
        "allowed_scope": {
            "workspaces": [str(w) for w in workspaces],
            "profiles": profiles,
        },
        "forbidden_actions": forbidden,
        "expected_artifacts": artifacts,
        "tests": tests,
        "review_requirements": review,
        "completion_criteria": criteria,
        "inputs": _string_list(raw.get("inputs") or [], field="inputs"),
        "constraints": _string_list(raw.get("constraints") or [], field="constraints"),
        "authorization": authorization,
    }
    if capability_req is not None:
        contract["capability_req"] = capability_req
    # Backward compatibility: omit the default fleet selector from canonical
    # contracts unless the caller explicitly supplied an execution block. This
    # preserves hashes for pre-runner work contracts.
    if execution is not None:
        contract["execution"] = execution
    canonical = json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return canonical, contract


def _contract_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_contract(contract_json: str) -> tuple[str, dict[str, Any], str]:
    """Parse + canonicalize a contract JSON string.

    Returns ``(canonical_json, contract, contract_sha256)``.
    """
    if not isinstance(contract_json, str) or not contract_json.strip():
        raise ValueError("contract_json is required")
    try:
        raw = json.loads(contract_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"contract_json is not valid JSON: {exc}") from exc
    canonical, contract = _canonical_contract(raw)
    return canonical, contract, _contract_sha256(canonical)


VALIDATION_MANIFEST_SCHEMA = "hermes.contract-validation-manifest/v2"


def _validation_manifest(
    contract: dict[str, Any], contract_sha256: str
) -> dict[str, Any]:
    """Return the prompt-free subset required by the observed-state validator.

    The objective, inputs, constraints, and backend request/response bodies are
    intentionally excluded.  The returned values are sufficient to run the
    exact same validation algorithm as ``hermes_contract_validate``.
    """
    context = {
        "task_id": contract["task_id"],
        "assigned_agent": contract["assigned_agent"],
        "assigned_profile": contract["assigned_profile"],
        "allowed_scope": contract["allowed_scope"],
        "forbidden_actions": [
            {"action": item["action"], "class": item["class"]}
            for item in contract["forbidden_actions"]
        ],
        "expected_artifacts": contract["expected_artifacts"],
        "tests": contract["tests"],
        "review_requirements": {
            key: value
            for key, value in contract["review_requirements"].items()
            if key != "evidence"
        },
        "completion_criteria": contract["completion_criteria"],
        "authorization": contract["authorization"],
    }
    if "capability_req" in contract:
        context["capability_req"] = contract["capability_req"]
    context["execution"] = None
    if isinstance(contract.get("execution"), dict):
        # Validation needs the canonical backend selector to distinguish local,
        # explicit Fabric, and auto-routed execution. Backend-specific options
        # are dispatch inputs and are deliberately not durable validation data.
        context["execution"] = {"backend": contract["execution"]["backend"]}
    encoded = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    # Durable validation context must never contain a token which our normal
    # output policy would redact.  Reject instead of silently persisting a
    # lossy manifest that could later validate different authority.
    if op.redact_output(encoded) != encoded:
        raise PermissionError("validation manifest contains secret-like durable values")
    return {
        "schema": VALIDATION_MANIFEST_SCHEMA,
        "contract_sha256": contract_sha256,
        "context_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "context": context,
    }


def _contract_from_validation_manifest(
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != VALIDATION_MANIFEST_SCHEMA
    ):
        raise ValueError("validation manifest schema is invalid")
    sha = str(manifest.get("contract_sha256") or "")
    context_sha = str(manifest.get("context_sha256") or "")
    context = manifest.get("context")
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or not re.fullmatch(
        r"[0-9a-f]{64}", context_sha
    ):
        raise ValueError("validation manifest digest is invalid")
    if not isinstance(context, dict):
        raise ValueError("validation manifest context is invalid")
    encoded = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != context_sha:
        raise ValueError("validation manifest context digest mismatch")
    if op.redact_output(encoded) != encoded:
        raise PermissionError("validation manifest contains secret-like durable values")
    required = {
        "task_id",
        "assigned_agent",
        "assigned_profile",
        "allowed_scope",
        "forbidden_actions",
        "expected_artifacts",
        "tests",
        "review_requirements",
        "completion_criteria",
        "authorization",
        "execution",
    }
    optional = {"capability_req"}
    if set(context) not in (required, required | optional):
        raise ValueError("validation manifest context fields are invalid")
    contract = dict(context)
    execution = context["execution"]
    if execution is None:
        contract.pop("execution")
    elif (
        not isinstance(execution, dict)
        or set(execution) != {"backend"}
        or not isinstance(execution.get("backend"), str)
    ):
        raise ValueError("validation manifest execution selector is invalid")
    else:
        normalized_execution = op_runners.normalize_execution(
            {
                "backend": execution["backend"],
                "options": {},
            }
        )
        if normalized_execution is None:
            raise ValueError("validation manifest execution selector is invalid")
        contract["execution"] = normalized_execution
    contract.update(
        {"schema": CONTRACT_SCHEMA, "objective": "", "inputs": [], "constraints": []}
    )
    return contract, sha
