"""Validate authoritative observations and produce deterministic decisions.

This module performs no I/O. Structural input errors raise ``ObservationError``;
unknown enum values become the fail-closed ``unknown`` classification. Error
text is token-matched, and decisions retain only matched token identifiers.
Classifications propose actions but never execute them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from hermes_gpt.missions.failure_catalog import _CLASS_TO_ROW, _PII_STRIP, AUTHORITY_TOKENS, CAPABILITY_TOKENS, CLASS_AMBIGUOUS, CLASS_AUTHORITY, CLASS_CAPABILITY, CLASS_ENVIRONMENT, CLASS_NONE, CLASS_NONE_APPROVAL, CLASS_NONE_COMPLETION, CLASS_NONE_DISPATCHABLE, CLASS_SEMANTIC, CLASS_TERMINAL, CLASS_TRANSIENT, CLASS_UNKNOWN, CLASS_WAITING, DECISION_SCHEMA, DELEGATION_STATES, ENVIRONMENT_TOKENS, INFLIGHT_DELEGATION_STATES, MATRIX, MAX_ERROR_TEXT, MAX_REASONS, MAX_REPLAN_ATTEMPTS_DEFAULT, MAX_STRING, MISSION_STATES, NODE_STATES, SCHEMA_VERSION, SEMANTIC_TOKENS, TRANSIENT_TOKENS, VERDICTS, WORKER_EXIT_KINDS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(text: Any, limit: int = MAX_STRING) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _normalize_tokens(text: Any) -> set[str]:
    """Word and underscore-joined phrase n-grams of an error string (matching only).

    ``quota exceeded`` yields ``quota``, ``exceeded``, ``quota_exceeded`` so
    both single-word vocabulary (``quota``) and multiword vocabulary
    (``rate_limit`` ← "rate limit") match deterministically.
    """
    if not text:
        return set()
    folded = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    if not folded:
        return set()
    words = [w for w in folded.split("_") if w]
    grams: set[str] = set(words)
    for n in (2, 3, 4):
        for i in range(len(words) - n + 1):
            grams.add("_".join(words[i : i + n]))
    return grams


def _match_tokens(text: Any, vocabulary: tuple[str, ...]) -> list[str]:
    tokens = _normalize_tokens(text)
    return sorted({v for v in vocabulary if v in tokens})


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Observation envelope validation (fail-closed on malformed input)
# ---------------------------------------------------------------------------


class ObservationError(ValueError):
    """Raised when the envelope is structurally unusable (caller error, not a
    classification outcome). Malformed *enum values inside* a structurally
    valid envelope are NOT this — they classify `unknown` (fail-closed)."""


def _validate_envelope(observation: dict[str, Any]) -> dict[str, Any]:
    """Structural validation. Returns the normalized envelope.

    Raises ObservationError for structurally invalid input (wrong types,
    unknown top-level keys, oversized payloads).
    """
    if not isinstance(observation, dict):
        raise ObservationError("observation must be a JSON object")
    allowed = {
        "delegation",
        "runner",
        "worker_exit",
        "last_failure_error",
        "capability",
        "plan",
        "mission",
        "breaker",
    }
    unknown_keys = set(observation) - allowed
    if unknown_keys:
        raise ObservationError(f"unknown observation keys: {sorted(unknown_keys)}")

    env: dict[str, Any] = {}

    delegation = observation.get("delegation")
    if delegation is not None:
        if not isinstance(delegation, dict):
            raise ObservationError("delegation must be an object or null")
        for key in delegation:
            if key not in {"state", "backend_state", "outcome", "validation_verdict"}:
                raise ObservationError(f"unknown delegation key: {key}")
        env["delegation"] = {
            "state": delegation.get("state", ""),
            "backend_state": delegation.get("backend_state", ""),
            "outcome": delegation.get("outcome", ""),
            "validation_verdict": delegation.get("validation_verdict", ""),
        }

    runner = observation.get("runner")
    if runner is not None:
        if not isinstance(runner, dict):
            raise ObservationError("runner must be an object or null")
        for key in runner:
            if key not in {"status", "outcome", "error"}:
                raise ObservationError(f"unknown runner key: {key}")
        env["runner"] = {
            "status": runner.get("status", ""),
            "outcome": runner.get("outcome", ""),
            "error": runner.get("error", ""),
        }

    worker_exit = observation.get("worker_exit")
    if worker_exit is not None:
        if not isinstance(worker_exit, dict):
            raise ObservationError("worker_exit must be an object or null")
        for key in worker_exit:
            if key not in {"kind", "code"}:
                raise ObservationError(f"unknown worker_exit key: {key}")
        env["worker_exit"] = {
            "kind": worker_exit.get("kind", ""),
            "code": worker_exit.get("code"),
        }

    error_text = observation.get("last_failure_error") or ""
    if not isinstance(error_text, str):
        raise ObservationError("last_failure_error must be a string")
    env["last_failure_error"] = error_text[:MAX_ERROR_TEXT]

    capability = observation.get("capability")
    if capability is not None:
        if not isinstance(capability, dict):
            raise ObservationError("capability must be an object or null")
        for key in capability:
            if key not in {"ok", "reasons"}:
                raise ObservationError(f"unknown capability key: {key}")
        reasons = capability.get("reasons") or []
        if not isinstance(reasons, list) or len(reasons) > MAX_REASONS:
            raise ObservationError("capability.reasons must be a list (<=16)")
        env["capability"] = {
            "ok": bool(capability.get("ok", True)),
            "reasons": [_sanitize(r, 64) for r in reasons],
        }

    plan = observation.get("plan") or {}
    if not isinstance(plan, dict):
        raise ObservationError("plan must be an object")
    for key in plan:
        if key not in {
            "node_state",
            "parent_done",
            "all_children_terminal",
            "retries",
            "replan_attempts_used",
        }:
            raise ObservationError(f"unknown plan key: {key}")
    env["plan"] = {
        "node_state": plan.get("node_state", ""),
        "parent_done": bool(plan.get("parent_done", False)),
        "all_children_terminal": bool(plan.get("all_children_terminal", False)),
        "retries": int(plan.get("retries", 0) or 0),
        "replan_attempts_used": int(plan.get("replan_attempts_used", 0) or 0),
    }

    mission_obs = observation.get("mission") or {}
    if not isinstance(mission_obs, dict):
        raise ObservationError("mission must be an object")
    for key in mission_obs:
        if key not in {"status", "final_approval_required"}:
            raise ObservationError(f"unknown mission key: {key}")
    env["mission"] = {
        "status": mission_obs.get("status", ""),
        "final_approval_required": bool(mission_obs.get("final_approval_required", True)),
    }

    breaker = observation.get("breaker") or {}
    if not isinstance(breaker, dict):
        raise ObservationError("breaker must be an object")
    for key in breaker:
        if key not in {"consecutive_failures", "limit", "gave_up"}:
            raise ObservationError(f"unknown breaker key: {key}")
    env["breaker"] = {
        "consecutive_failures": int(breaker.get("consecutive_failures", 0) or 0),
        "limit": int(breaker.get("limit", 3) or 3),
        "gave_up": bool(breaker.get("gave_up", False)),
    }

    return env


def _enums_valid(env: dict[str, Any]) -> tuple[bool, str]:
    """True when every populated enum value is inside the authoritative vocab."""
    delegation = env.get("delegation")
    if delegation:
        if delegation["state"] and delegation["state"] not in DELEGATION_STATES:
            return False, f"delegation.state={delegation['state']!r}"
        if (
            delegation["validation_verdict"]
            and delegation["validation_verdict"] not in VERDICTS
        ):
            return False, "delegation.validation_verdict invalid"
    worker_exit = env.get("worker_exit")
    if worker_exit and worker_exit["kind"] and worker_exit["kind"] not in WORKER_EXIT_KINDS:
        return False, f"worker_exit.kind={worker_exit['kind']!r}"
    node_state = env["plan"]["node_state"]
    if node_state and node_state not in NODE_STATES:
        return False, f"plan.node_state={node_state!r}"
    mission_status = env["mission"]["status"]
    if mission_status and mission_status not in MISSION_STATES:
        return False, f"mission.status={mission_status!r}"
    return True, ""


def _token_evidence(env: dict[str, Any]) -> dict[str, list[str]]:
    """Matched token ids per class (the ONLY thing retained from raw error text)."""
    texts = [env["last_failure_error"]]
    runner = env.get("runner")
    if runner:
        texts.append(runner.get("error") or "")
    blob = "\n".join(t for t in texts if t)
    return {
        "authority": _match_tokens(blob, AUTHORITY_TOKENS),
        "transient": _match_tokens(blob, TRANSIENT_TOKENS),
        "environment": _match_tokens(blob, ENVIRONMENT_TOKENS),
        "semantic": _match_tokens(blob, SEMANTIC_TOKENS),
        "capability": _match_tokens(blob, CAPABILITY_TOKENS),
    }


# ---------------------------------------------------------------------------
# The classifier — pure function, fixed ladder, fail-closed.
# ---------------------------------------------------------------------------


def classify(mission_id: str, node_id: str, env: dict[str, Any]) -> dict[str, Any]:
    """Classify a validated observation envelope.

    Pure: no I/O, no clock inside the hashed core. Deterministic ladder
    (fail-closed first, then never-auto-retry classes, then flavors, then
    progress rows). Returns the decision dict (without generated_at).
    """
    evidence = _token_evidence(env)
    matched: list[str] = []
    for cls in sorted(evidence):
        matched.extend(f"{cls}:{t}" for t in evidence[cls])

    delegation = env.get("delegation")
    runner = env.get("runner")
    worker_exit = env.get("worker_exit")
    capability = env.get("capability")
    plan = env["plan"]
    mission_obs = env["mission"]
    breaker = env["breaker"]

    def decide(
        classification: str,
        *,
        uncertainty: str = "",
        reason: str = "",
        replan: bool = False,
        failure: bool = True,
        row_override: str = "",
    ) -> dict[str, Any]:
        failure_class = classification if failure else ""
        row_key = row_override or _CLASS_TO_ROW[classification]
        if row_override and row_override not in MATRIX:
            raise AssertionError(f"unknown matrix row override: {row_override}")
        row = MATRIX[row_key]
        breaker_open = breaker["gave_up"] or (
            breaker["limit"] > 0 and breaker["consecutive_failures"] >= breaker["limit"]
        )
        # §8.1: the hard retry ceiling overrides any retry-flavored proposal.
        # Only `transient` proposes an auto-retry; every other class already
        # parks/escalates/observes. A *dispatch* progress row is not a retry
        # and is never breaker-gated here.
        if classification == CLASS_TRANSIENT and breaker_open:
            row_key = "breaker_exhausted"
            row = MATRIX[row_key]
            reason = reason or "retry ceiling reached"
        decision: dict[str, Any] = {
            "schema": DECISION_SCHEMA,
            "mission_id": mission_id,
            "node_id": node_id,
            "classification": classification,
            "failure_class": failure_class,
            "row_key": row_key,
            "proposed_action": row["smallest_action"],
            "proposed_tool": row["proposed_tool"],
            "verify": row["verify"],
            "auto_retry": bool(row["auto_retry"]) and not breaker_open,
            "would_execute": False,  # decision output only — hard constant
            "need_attention": classification
            in (CLASS_UNKNOWN, CLASS_AUTHORITY, CLASS_CAPABILITY, CLASS_ENVIRONMENT)
            or row_key in ("breaker_exhausted", "fail_closed_evidence"),
            "matched_tokens": matched,
            "reason": _sanitize(reason, 200),
            "breaker_open": breaker_open,
        }
        if uncertainty:
            decision["classification_uncertainty"] = uncertainty
        if replan:
            decision["replan_proposal"] = {
                "eligible": True,
                "attempts_used": plan["replan_attempts_used"],
                "max_attempts": MAX_REPLAN_ATTEMPTS_DEFAULT,
                "path": "hermes_plan_decompose / hermes_swarm_stage_advance (existing tools; audited)",
                "executed": False,  # D8: proposal only, never executed here
            }
        return decision

    # ---- 0. Malformed enum anywhere → fail-closed unknown -------------------
    ok, bad = _enums_valid(env)
    if not ok:
        return decide(
            CLASS_UNKNOWN,
            uncertainty=f"invalid_observation_enum:{_sanitize(bad, 64)}",
            reason="observation carries a value outside the authoritative vocabulary",
        )

    dl_state = delegation["state"] if delegation else ""
    dl_backend = delegation["backend_state"] if delegation else ""
    dl_outcome = delegation["outcome"] if delegation else ""
    dl_verdict = delegation["validation_verdict"] if delegation else ""
    node_state = plan["node_state"]
    mission_status = mission_obs["status"]

    # ---- 1. Operator terminal intent ---------------------------------------
    if dl_state == "cancelled" or node_state == "cancelled":
        return decide(CLASS_TERMINAL, failure=True, reason="operator cancelled")
    if mission_status in ("paused", "blocked") and not node_state:
        return decide(
            CLASS_TERMINAL,
            failure=True,
            reason=f"mission {mission_status} by operator; terminal, no action",
        )

    # ---- 2. Missing observation fail-closed --------------------------------
    # A subject that claims in-flight progress must be observable through at
    # least one run channel; otherwise the state is un-verifiable → unknown.
    claims_inflight = (
        dl_state in INFLIGHT_DELEGATION_STATES
        or node_state in {"dispatched", "running"}
    )
    has_channel = runner is not None or worker_exit is not None or dl_state != ""
    if claims_inflight and not has_channel:
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:no_run_channel",
            reason="in-flight subject with no runner/worker-exit/delegation observation",
        )
    if (
        worker_exit is not None
        and worker_exit["kind"] == "nonzero_exit"
        and not env["last_failure_error"]
        and not (runner and runner.get("error"))
    ):
        # rc != 0 with zero flavor evidence: no basis to choose semantic vs
        # transient vs environment. Fail closed rather than guess.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:no_failure_flavor",
            reason="nonzero worker exit with no error text to flavor the failure",
        )

    # §11.2 row 2: child running + worker dead (exit un-observable) → the
    # smallest action is reclaim + bounded retry via the host kanban surface.
    if (
        node_state == "running"
        and worker_exit is not None
        and worker_exit["kind"] == "unknown"
    ):
        return decide(
            CLASS_TRANSIENT,
            reason="worker dead with unobservable exit; reclaim + bounded retry is smallest",
            row_override="reclaim_dead_worker",
        )

    # ---- 3. Ambiguous (never auto-redispatch; do not fabricate) -------------
    if (
        dl_state == "reconciling"
        or dl_backend == "ambiguous"
        or dl_outcome == "submission_may_have_succeeded"
    ):
        return decide(
            CLASS_AMBIGUOUS,
            reason="delegation outcome unknown; observe only (§7.4 non-idempotent)",
        )

    # ---- 4. Authority / policy (never auto-retry) ---------------------------
    if evidence["authority"]:
        return decide(
            CLASS_AUTHORITY,
            reason="credential/quota/policy blocker in authoritative error channel",
        )

    # ---- 5. Capability (no capable target; park + escalate) -----------------
    if (capability is not None and not capability["ok"]) or evidence["capability"]:
        return decide(
            CLASS_CAPABILITY,
            reason="capability negotiation failed or no capable target",
        )

    # ---- 6. Environment ------------------------------------------------------
    if evidence["environment"]:
        return decide(
            CLASS_ENVIRONMENT,
            reason="environment fault (workspace/store/manifest/lease)",
        )

    # ---- 7. Semantic failure (implementation/QA defect) ---------------------
    semantic_evidence = (
        dl_verdict in ("NOT_SATISFIED", "INVALID_CONTRACT")
        or bool(evidence["semantic"])
        or (worker_exit is not None and worker_exit["kind"] == "clean_exit")
    )
    if semantic_evidence:
        replan_ok = plan["replan_attempts_used"] < MAX_REPLAN_ATTEMPTS_DEFAULT
        return decide(
            CLASS_SEMANTIC,
            reason=(
                "validation verdict not satisfied"
                if dl_verdict in ("NOT_SATISFIED", "INVALID_CONTRACT")
                else "worker protocol violation (clean exit while running)"
                if worker_exit is not None and worker_exit["kind"] == "clean_exit"
                else "defect-flavored failure evidence"
            ),
            replan=replan_ok,
        )

    # ---- 8. Transient (backoff + retry, breaker respected) ------------------
    # Only exit kinds + explicit throughput token evidence flavor transient.
    # A bare runner "failed" outcome carries no class basis → fail closed
    # (unknown), never an optimistic transient retry.
    if worker_exit is not None and worker_exit["kind"] in ("rate_limited", "signaled"):
        return decide(
            CLASS_TRANSIENT,
            reason="throughput/network-flavored failure; backoff + bounded retry",
        )
    if evidence["transient"]:
        return decide(
            CLASS_TRANSIENT,
            reason="throughput/network-flavored failure; backoff + bounded retry",
        )
    if runner is not None and runner.get("outcome") == "failed":
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_runner_failure",
            reason="runner failed with no classifiable evidence; refusing to guess a class",
        )

    # ---- 9. Evidence gate (fail-closed before any success row) --------------
    if dl_state == "failed":
        # Delegation failed but nothing flavored it above → unclassifiable.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_delegation_failure",
            reason="delegation failed with no classifiable evidence",
        )
    if node_state == "failed" and dl_state == "":
        # Node failed but no delegation/runner/exit observation flavored it.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_node_failure",
            reason="plan node failed with no classifiable evidence",
        )
    if dl_verdict == "INCONCLUSIVE":
        return decide(
            CLASS_UNKNOWN,
            uncertainty="inconclusive_validation",
            reason="validation INCONCLUSIVE is never success; fail closed",
            row_override="fail_closed_evidence",
        )

    # ---- 10. Waiting --------------------------------------------------------
    if mission_status == "awaiting_approval":
        return decide(CLASS_WAITING, failure=False, reason="owner approval pending")
    if node_state and not plan["all_children_terminal"]:
        if node_state == "pending" and not plan["parent_done"]:
            return decide(
                CLASS_WAITING, failure=False, reason="parent dependency not done"
            )
        if node_state in ("dispatched", "running", "awaiting_review", "validated"):
            return decide(
                CLASS_WAITING, failure=False, reason="work in flight; no failure observed"
            )
    if dl_state in ("queued", "running", "reserved"):
        return decide(
            CLASS_WAITING, failure=False, reason="delegation in flight; no failure observed"
        )

    # ---- 11. Progress rows (non-failure; §11.2 R1/R6/R7) --------------------
    if plan["all_children_terminal"]:
        if dl_verdict == "" and dl_state == "succeeded":
            return decide(
                CLASS_UNKNOWN,
                uncertainty="missing_observation:unverified_evidence",
                reason="terminal success without validation verdict (§11.2 fail-closed row)",
                row_override="fail_closed_evidence",
            )
        if mission_obs["final_approval_required"]:
            return decide(
                CLASS_NONE_APPROVAL,
                failure=False,
                reason="all children terminal; owner approval required (controller stops)",
            )
        return decide(
            CLASS_NONE_COMPLETION,
            failure=False,
            reason="all children terminal; verified evidence; no approval flag",
        )
    if node_state in ("pending", "blockable") and plan["parent_done"] and mission_status == "running":
        return decide(
            CLASS_NONE_DISPATCHABLE,
            failure=False,
            reason="ready child with satisfied dependencies; dispatch is the smallest action",
        )

    # ---- 12. Steady state ----------------------------------------------------
    return decide(
        CLASS_NONE,
        failure=False,
        reason="no failure signal and no progress row applies; recheck next trigger",
    )


def finalize(decision: dict[str, Any]) -> dict[str, Any]:
    """Stamp the decision digest over the canonical core (no clock inside).

    ``schema_version`` participates in the digest so the dry-run and recorded
    paths produce the identical ``decision_sha256`` for identical inputs.
    """
    decision["schema_version"] = SCHEMA_VERSION
    core = {k: v for k, v in decision.items() if k != "generated_at"}
    decision["decision_sha256"] = hashlib.sha256(_canonical(core).encode()).hexdigest()
    decision["generated_at"] = _now()
    return decision
