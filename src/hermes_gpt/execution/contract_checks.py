"""Observed-state checks for Work Contract completion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from hermes_gpt.policy import authorization as op
from hermes_gpt.execution import runners as op_runners
from hermes_gpt.workspace import tools as op_workspace
from hermes_gpt.execution.contract_observations import _observed_audit, _observed_runs
from hermes_gpt.execution.contract_schema import _resolve_artifact_paths, _resolve_root, _truncate


def _check_run_state(contract: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    task_id = contract["task_id"]
    outcome_ok = set(contract["completion_criteria"]["run_state"]["outcome_ok"])
    runs = _observed_runs(task_id, hermes_root)
    if not runs:
        return {
            "kind": "run_state",
            "status": "UNVERIFIED",
            "detail": f"no observed run/outcome for task_id {task_id}",
        }

    # Retries produce multiple records for a task. Select the latest observed
    # record by stable keys rather than relying on SQLite/list traversal order.
    # A newer retry is authoritative; an older successful attempt cannot mask a
    # currently-running or failed retry.
    def run_sort_key(run: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        return (
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or ""),
            str(run.get("outcome") or run.get("state") or ""),
            str(run.get("board") or run.get("scope") or ""),
            str(run.get("error") or ""),
        )

    primary = max(runs, key=run_sort_key)
    status = str(primary.get("status") or "")
    outcome = str(primary.get("outcome") or primary.get("state") or "")
    error = primary.get("error")
    source = primary.get("board") or primary.get("scope") or "observed"
    if error:
        return {
            "kind": "run_state",
            "status": "FAIL",
            "detail": f"observed {source} run errored: {_truncate(str(error), 200)}",
        }
    if outcome:
        outcome_passes = outcome in outcome_ok
    else:
        outcome_passes = status in outcome_ok
    if outcome_passes:
        return {
            "kind": "run_state",
            "status": "PASS",
            "detail": f"observed {source} status={status} outcome={outcome}",
        }
    return {
        "kind": "run_state",
        "status": "FAIL",
        "detail": f"observed {source} status={status} outcome={outcome} not in outcome_ok",
    }


def _admitted_artifact_evidence(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> list[dict[str, Any]]:
    """Read coordinator-verified remote artifact metadata from capable runners.

    This is intentionally metadata-only. Active remote content remains in the
    Fabric admission store and is never copied into the Work Contract workspace
    merely to make the artifact check pass.
    """
    task_id = str(contract.get("task_id") or "")
    try:
        backend = op_runners.get_backend("fabric")
    except LookupError:
        return []
    observer = getattr(backend, "observed_artifacts", None)
    if not callable(observer):
        return []
    try:
        value = observer(
            task_id,
            contract_sha256=contract_sha256,
            hermes_root=hermes_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _check_artifacts(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> dict[str, Any]:
    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    artifacts = contract["expected_artifacts"]
    if not artifacts:
        return {
            "kind": "artifacts",
            "status": "PASS",
            "detail": "no artifacts required",
        }
    if not workspaces:
        return {
            "kind": "artifacts",
            "status": "UNVERIFIED",
            "detail": "no allowed workspace",
        }

    admitted = _admitted_artifact_evidence(contract, contract_sha256, hermes_root)
    admitted_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in admitted:
        name = item.get("logical_name")
        if isinstance(name, str):
            admitted_by_name.setdefault(name, []).append(item)

    missing: list[str] = []
    evidence: list[dict[str, Any]] = []
    for art in artifacts:
        if not art["must_exist"]:
            continue
        candidates = []
        try:
            candidates = _resolve_artifact_paths(art["path"], workspaces)
        except (ValueError, PermissionError):
            missing.append(art["path"])
            continue
        found = None
        for cand in candidates:
            try:
                if cand.is_file() and cand.stat().st_size >= art["min_bytes"]:
                    found = cand
                    break
            except OSError:
                continue
        if found is not None:
            evidence.append({"basename": found.name, "size": found.stat().st_size})
            continue

        remote = next(
            (
                item
                for item in admitted_by_name.get(art["path"], [])
                if isinstance(item.get("size_bytes"), int)
                and item["size_bytes"] >= art["min_bytes"]
                and item.get("provenance") == "coordinator_verified_artifact"
            ),
            None,
        )
        if remote is not None:
            evidence.append(
                {
                    "basename": Path(art["path"]).name,
                    "size": remote["size_bytes"],
                    "sha256": remote.get("sha256", ""),
                    "provenance": "coordinator_verified_artifact",
                }
            )
        else:
            missing.append(art["path"])
    if missing:
        return {
            "kind": "artifacts",
            "status": "FAIL",
            "detail": f"missing artifacts: {', '.join(missing)}",
            "evidence": evidence,
        }
    return {
        "kind": "artifacts",
        "status": "PASS",
        "detail": f"{len(artifacts)} artifact(s) present",
        "evidence": evidence,
    }


def _check_tests(
    contract: dict[str, Any],
    runner: Callable[..., tuple[int, str, str]] | None,
    hermes_root: Path,
) -> dict[str, Any]:
    tests = contract["tests"]
    tests_required = contract["completion_criteria"]["tests_pass"]
    if not tests:
        return {
            "kind": "tests",
            "status": "PASS" if not tests_required else "UNVERIFIED",
            "detail": "no tests declared"
            if not tests_required
            else "tests required but none declared",
        }
    if not tests_required:
        return {
            "kind": "tests",
            "status": "PASS",
            "detail": "tests optional (tests_pass=false)",
        }

    # Test execution individually gated at workspace + direct (D6).
    policy = op.OperatorPolicy()
    can_run = op.has_level("workspace", policy.level) and policy.apply_mode == "direct"
    if not can_run:
        return {
            "kind": "tests",
            "status": "UNVERIFIED",
            "detail": "tests required but workspace+direct not granted (D6)",
        }

    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    results: list[dict[str, Any]] = []
    for t in tests:
        workdir = t.get("workdir") or (str(workspaces[0]) if workspaces else None)
        if workdir and workspaces and not op.path_under_allowed(workdir, workspaces):
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} workdir is not under an allowed workspace",
            }
        try:
            out = op_workspace.hermes_workspace_run_test(
                command=t["command"],
                workdir=workdir,
                timeout=120,
                dry_run=False,
                runner=runner,
            )
            payload = json.loads(out)
        except Exception as exc:
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} refused/errored: {_truncate(op.redact_output(str(exc)), 200)}",
            }
        rc = payload.get("returncode")
        results.append({"name": t["name"], "rc": rc, "command": t["command"]})
        if rc != 0:
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} failed rc={rc}",
                "evidence": results,
            }
    return {
        "kind": "tests",
        "status": "PASS",
        "detail": f"{len(tests)} test(s) passed",
        "evidence": results,
    }


def _assignee_identity(contract: dict[str, Any]) -> str:
    """Return the executing profile identity for attribution-sensitive checks.

    ``assigned_agent`` identifies placement: it may be ``auto`` before routing
    or a Fabric node name after remote placement. ``assigned_profile`` is the
    authority-bearing actor that actually executes the contract, so review
    distinctness and audit attribution must use it whenever present. The
    assigned-agent fallback preserves compatibility with legacy callers that
    construct an incomplete contract outside the normal parser.
    """
    assigned_profile = str(contract.get("assigned_profile") or "").strip()
    if assigned_profile:
        return assigned_profile
    return str(contract["assigned_agent"])


def _check_review(
    contract: dict[str, Any], contract_sha256: str, hermes_root: Path
) -> dict[str, Any]:
    review = contract["review_requirements"]
    assignee_identities = _attributable_identities(contract)
    if not review["required"]:
        return {"kind": "review", "status": "PASS", "detail": "review not required"}

    declared_reviewer = review.get("reviewer") or ""
    if declared_reviewer and declared_reviewer in assignee_identities:
        return {
            "kind": "review",
            "status": "FAIL",
            "detail": "self-review: declared reviewer == assignee identity",
        }

    # Evidence 1: an audit hermes_contract_validate acceptance by a distinct reviewer.
    for rec in _observed_audit(hermes_root):
        if rec.get("tool") != "hermes_contract_validate":
            continue
        if rec.get("contract_sha256") != contract_sha256:
            continue
        verdict = rec.get("verdict")
        if verdict not in ("SATISFIED", "accept", "ACCEPT"):
            continue
        reviewer = rec.get("reviewer") or rec.get("profile") or ""
        if reviewer and reviewer not in assignee_identities:
            return {
                "kind": "review",
                "status": "PASS",
                "detail": f"audit acceptance by reviewer {reviewer} != assignee",
            }
    # Evidence 1b (v0.7 S3): a ReviewAcceptanceRecord in the review-evidence
    # store written via hermes_review_accept. Distinct reviewer re-checked at
    # validate time (ADR-003): the record's reviewer must differ from the
    # contract's assigned_agent.
    try:
        from hermes_gpt.execution import review as op_review

        for rec in op_review.read_review_acceptances(hermes_root):
            if rec.get("contract_sha256") != contract_sha256:
                continue
            if rec.get("verdict") != "SATISFIED":
                continue
            reviewer = rec.get("reviewer") or ""
            if reviewer and reviewer not in assignee_identities:
                return {
                    "kind": "review",
                    "status": "PASS",
                    "detail": f"review-evidence acceptance by reviewer {reviewer} != assignee",
                }
    except Exception:
        pass
    # Evidence 2: human approval reference by someone other than the assignee.
    auth = contract.get("authorization") or {}
    approved_by = auth.get("approved_by") or ""
    if (
        approved_by
        and approved_by not in assignee_identities
        and auth.get("approval_reference")
    ):
        return {
            "kind": "review",
            "status": "PASS",
            "detail": f"human approval by {approved_by} (ref {auth.get('approval_reference')})",
        }
    return {
        "kind": "review",
        "status": "FAIL",
        "detail": "review required but no evidence by a reviewer distinct from the assignee",
    }


def _attributable_identities(contract: dict[str, Any]) -> set[str]:
    """Identities whose audit records are attributable to this contract's execution.

    ``assigned_profile`` is the effective assignee (authority-bearing actor).
    ``assigned_agent`` is the placement identity: after remote auto placement it
    is the Fabric node / dispatcher name that physically executed on the
    assignee's behalf. Task-scoped records recorded under either identity must
    be attributed; records under any other concrete profile stay unattributed
    so unrelated concurrent actors cannot fail or satisfy this contract.
    """
    identities: set[str] = set()
    for value in (contract.get("assigned_profile"), contract.get("assigned_agent")):
        text = str(value or "").strip()
        if text:
            identities.add(text)
    return identities


def _auto_fabric_lineage(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> bool | None:
    """Return whether an auto contract has durable remote Fabric placement lineage.

    ``True`` means at least one matching routing decision selected remote Fabric.
    ``False`` means matching durable routing evidence exists and is local-only.
    ``None`` means placement provenance cannot be proven, which callers must treat
    as unverified rather than assuming local execution.
    """
    execution = contract.get("execution")
    if (
        not isinstance(execution, dict)
        or str(execution.get("backend") or "").strip().lower() != "auto"
    ):
        return False

    journal = _resolve_root(hermes_root) / "fabric" / "routing-decisions.jsonl"
    matched = False
    try:
        with journal.open("rb") as fh:
            for raw_line in fh:
                if not raw_line or len(raw_line) > 128_000:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if (
                    record.get("schema") != "hermes.fabric-routing-decision/v1"
                    or record.get("task_id") != contract.get("task_id")
                    or record.get("original_contract_sha256") != contract_sha256
                ):
                    continue
                selected = record.get("selected")
                if not isinstance(selected, dict):
                    continue
                matched = True
                if (
                    selected.get("remote") is True
                    and selected.get("transport_backend") == "fabric"
                ):
                    return True
    except OSError:
        return None
    return False if matched else None


def _check_forbidden(
    contract: dict[str, Any],
    hermes_root: Path,
    contract_sha256: str = "",
) -> dict[str, Any]:
    forbidden = contract["forbidden_actions"]
    if not forbidden:
        return {
            "kind": "forbidden",
            "status": "PASS",
            "detail": "no forbidden actions declared",
        }

    identities = _attributable_identities(contract)
    task_id = contract["task_id"]
    labels = [fa["action"].lower() for fa in forbidden]
    signals: list[dict[str, Any]] = []

    # Audit trail scan (D5): scope strictly to this contract's task identity.
    # Profile-only matching allowed an unrelated concurrent contract to fail this
    # one; records without a matching task_id are intentionally ignored.
    for rec in _observed_audit(hermes_root):
        if str(rec.get("task_id") or "") != task_id:
            continue
        profile = str(rec.get("profile") or "")
        source = str(rec.get("source_profile") or "").strip()
        attributable = (
            profile in identities
            or profile in ("", "unknown")
            # Records written by a dispatcher/peer on behalf of the assignee.
            or (bool(source) and source in identities)
        )
        if not attributable:
            continue
        tool = str(rec.get("tool") or "").lower()
        summary = str(rec.get("summary") or "").lower()
        extra_forbidden = str(rec.get("forbidden_action") or "").lower()
        for fa in forbidden:
            label = fa["action"].lower()
            if (
                label in tool
                or label in summary
                or (extra_forbidden and label == extra_forbidden)
            ):
                signals.append(
                    {
                        "action": fa["action"],
                        "class": fa["class"],
                        "tool": rec.get("tool"),
                        "summary": _truncate(rec.get("summary")),
                    }
                )
                break

    # Artifact set scan: a forbidden-action label appearing in a produced artifact
    # path is a weak signal (detection, not prevention; NG-WC4).
    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    for art in contract["expected_artifacts"]:
        try:
            candidates = _resolve_artifact_paths(art["path"], workspaces)
        except (ValueError, PermissionError):
            continue
        for cand in candidates:
            try:
                if cand.is_file():
                    low = str(cand).lower()
                    for fa in forbidden:
                        if fa["action"].lower() in low:
                            signals.append(
                                {
                                    "action": fa["action"],
                                    "class": fa["class"],
                                    "tool": "artifact",
                                    "summary": cand.name,
                                }
                            )
                            break
            except OSError:
                continue

    if signals:
        detail = "; ".join(
            f"{s['action']} ({s['class']}) via {s['tool']}" for s in signals[:5]
        )
        return {
            "kind": "forbidden",
            "status": "FAIL",
            "detail": f"forbidden action detected: {detail}",
            "evidence": signals[:10],
        }

    # Fabric v1 admits bounded remote run-state evidence but does not admit a
    # coordinator-verifiable forbidden-action audit trail. New dispatches with
    # non-empty forbidden_actions are rejected at the Fabric boundary. Historical
    # explicit-Fabric contracts therefore remain unverified regardless of whether
    # the observer is currently healthy: absence of remote evidence is not proof.
    execution = contract.get("execution")
    execution_backend = (
        str(execution.get("backend") or "").strip().lower()
        if isinstance(execution, dict)
        else ""
    )
    if execution_backend == "fabric":
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric execution has no coordinator-verifiable forbidden-action evidence",
        }

    # Auto contracts need durable placement lineage before absence can be trusted.
    # A missing/unreadable journal is itself unverified; a competing local runner
    # record for the same task_id must never launder unknown Fabric provenance.
    auto_lineage = _auto_fabric_lineage(contract, contract_sha256, hermes_root)
    if auto_lineage is True:
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric auto placement has no coordinator-verifiable forbidden-action evidence",
        }
    if auto_lineage is None:
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "auto placement provenance is unavailable for forbidden-action verification",
        }

    # Legacy/non-auto callers may still have Fabric history discoverable by the
    # registered backend. Observation failure does not prove Fabric involvement,
    # but any positively observed Fabric run remains fail-closed.
    try:
        fabric_backend = op_runners.get_backend("fabric")
        observer = getattr(fabric_backend, "observed_runs", None)
        fabric_runs = (
            observer(task_id, hermes_root=hermes_root) if callable(observer) else []
        )
    except (LookupError, OSError, RuntimeError, TypeError, ValueError):
        fabric_runs = []
    if any(
        isinstance(run, dict)
        and (
            run.get("backend") == "fabric"
            or str(run.get("scope") or "").startswith("fabric:")
        )
        for run in fabric_runs
    ):
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric run has no coordinator-verifiable forbidden-action evidence",
        }

    return {
        "kind": "forbidden",
        "status": "PASS",
        "detail": "no forbidden actions detected in audit/artifacts",
    }


def _check_authorization(contract: dict[str, Any]) -> dict[str, Any]:
    auth = contract.get("authorization") or {}
    approved = auth.get("approved") is True
    if not approved:
        return {
            "kind": "authorization",
            "status": "FAIL",
            "detail": "authorization not approved",
        }
    if auth.get("class") == "high_impact" and not auth.get("approved_by"):
        return {
            "kind": "authorization",
            "status": "FAIL",
            "detail": "high_impact requires approved_by",
        }
    return {
        "kind": "authorization",
        "status": "PASS",
        "detail": f"authorized ({auth.get('class')})",
    }
