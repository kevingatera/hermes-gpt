"""First-class durable Mission runtime for Hermes GPT v0.9.

A Mission is the durable parent object for coordinated work.  It does not
replace Work Contracts, Swarms, Fabric, or delegation runners; it binds those
objects together under one bounded lifecycle while preserving their existing
authority/evidence semantics.

Security properties:
- reads require Operator read_only; mutations require workspace + direct + confirm;
- final approval is Owner-gated when ``final_approval_required`` is true;
- context is reference-only and bounded (no fetched source bodies are persisted);
- skills are explicit bounded manifests;
- child evidence is referenced, never copied;
- restart reconciliation is fail-closed and never fabricates child success.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.missions import observations as mission_observations
from hermes_gpt.missions import spec as mission_spec
from hermes_gpt.missions import store as mission_store
from hermes_gpt.policy import authorization as op

SCHEMA_VERSION = mission_store.SCHEMA_VERSION
MISSION_SCHEMA = mission_store.MISSION_SCHEMA
MISSION_EVENT_SCHEMA = mission_store.MISSION_EVENT_SCHEMA

MISSION_SPEC_SCHEMA = mission_spec.MISSION_SPEC_SCHEMA
MISSION_ID_RE = mission_spec.MISSION_ID_RE
REF_RE = mission_spec.REF_RE
WORKFLOW_REF_RE = mission_spec.WORKFLOW_REF_RE
SHA_RE = mission_spec.SHA_RE

MAX_TITLE = mission_spec.MAX_TITLE
MAX_OBJECTIVE = mission_spec.MAX_OBJECTIVE
MAX_ACCEPTANCE = mission_spec.MAX_ACCEPTANCE
MAX_ACCEPTANCE_ITEM = mission_spec.MAX_ACCEPTANCE_ITEM
MAX_CONTEXT = mission_spec.MAX_CONTEXT
MAX_SKILLS = mission_spec.MAX_SKILLS

STATUSES = (
    "draft",
    "running",
    "paused",
    "blocked",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES = mission_observations.TERMINAL_STATUSES
ATTACHMENT_KINDS = {"workflow", "contract", "delegation", "evidence", "artifact"}
ATTACHMENT_STATES = {"unknown", "pending", "running", "blocked", "succeeded", "failed", "cancelled"}
MISSION_TRANSITIONS = {
    "draft": {"running", "paused", "blocked", "failed", "cancelled"},
    "running": {"paused", "blocked", "failed", "cancelled"},
    "paused": {"running", "blocked", "failed", "cancelled"},
    "blocked": {"running", "paused", "failed", "cancelled"},
    "awaiting_approval": {"running", "blocked", "failed", "cancelled"},
}

MAX_ATTACHMENTS = 512
MAX_LIST = 200

_ALLOWED_SPEC_KEYS = mission_spec._ALLOWED_SPEC_KEYS
_ALLOWED_PATCH_KEYS = mission_spec._ALLOWED_PATCH_KEYS
_ALLOWED_CONTEXT_KEYS = mission_spec._ALLOWED_CONTEXT_KEYS
_ALLOWED_SKILL_KEYS = mission_spec._ALLOWED_SKILL_KEYS

# Existing mission callers import these private helpers from this module.
_closed = mission_spec._closed
_bounded_text = mission_spec._bounded_text
_normalize_acceptance = mission_spec._normalize_acceptance
_normalize_context = mission_spec._normalize_context
_normalize_skills = mission_spec._normalize_skills
_normalize_spec = mission_spec._normalize_spec

# These helpers stay available here because mission consumers share this
# module as the stable entry point for its SQLite records.
_root = mission_store.root
_db_path = mission_store.db_path
_connect = mission_store.connect
_init_db = mission_store.init_db
_begin_write = mission_store.begin_write
_row_to_mission = mission_store.row_to_mission
_get_row = mission_store.get_row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    db: sqlite3.Connection,
    mission_id: str,
    event_type: str,
    *,
    from_status: str = "",
    to_status: str = "",
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reason_sha = hashlib.sha256(reason.encode()).hexdigest() if reason else ""
    detail_value = details or {}
    db.execute(
        "INSERT INTO mission_events(mission_id,event_type,from_status,to_status,reason_sha256,details_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (mission_id, event_type, from_status, to_status, reason_sha, json.dumps(detail_value, sort_keys=True), _now()),
    )
    return {
        "mission_id": mission_id,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "reason_sha256": reason_sha,
        "details": detail_value,
    }


def _publish_live_event(notice: dict[str, Any] | None, hermes_root: Path | None) -> None:
    """Publish a wake-up only after the authoritative Mission commit succeeds."""
    if notice is None:
        return
    try:
        from hermes_gpt.workspace import live_events

        live_events.publish_event(
            topic="mission",
            kind=str(notice["event_type"]),
            subject_type="mission",
            subject_id=str(notice["mission_id"]),
            mission_id=str(notice["mission_id"]),
            source="mission-runtime",
            payload={
                "from_status": notice.get("from_status", ""),
                "to_status": notice.get("to_status", ""),
                "reason_sha256": notice.get("reason_sha256", ""),
                "details": notice.get("details", {}),
            },
            hermes_root=_root(hermes_root),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return

def _audit(tool: str, policy: op.OperatorPolicy, *, dry_run: bool, success: bool, changed: bool, mission_id: str = "", summary: str = "") -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=summary,
            extra={"mission_id": mission_id},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(op.error_from_exception(exc, layer="operator", code=code, suggested_action=action))


def hermes_mission_create(
    mission_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        raw = json.loads(mission_json)
        if not isinstance(raw, dict):
            raise TypeError("mission_json must contain an object")
        spec = _normalize_spec(raw)
        if not spec["final_approval_required"]:
            policy.require_owner(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission creation requires confirm=true")
        plan = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": "hermes_mission_create",
            "mission_id": spec["mission_id"],
            "status": "draft",
            "changed": not effective_dry,
            "dry_run": effective_dry,
        }
        if effective_dry:
            _audit("hermes_mission_create", policy, dry_run=True, success=True, changed=False, mission_id=spec["mission_id"], summary="mission create planned")
            return json.dumps(plan)
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            if db.execute("SELECT 1 FROM missions WHERE mission_id=?", (spec["mission_id"],)).fetchone():
                raise ValueError("mission already exists")
            now = _now()
            db.execute(
                "INSERT INTO missions(mission_id,spec_json,status,version,approval_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (spec["mission_id"], json.dumps(spec, sort_keys=True), "draft", 1, "{}", now, now),
            )
            live_notice = _event(db, spec["mission_id"], "mission.created", to_status="draft")
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_create", policy, dry_run=False, success=True, changed=True, mission_id=spec["mission_id"], summary="mission created")
        return json.dumps(plan)
    except (ValueError, TypeError, json.JSONDecodeError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit("hermes_mission_create", policy, dry_run=dry_run, success=False, changed=False, summary="mission create rejected")
        return _error(exc, "MISSION_CREATE_REJECTED", "Check mission schema and Operator workspace/direct policy.")

def hermes_mission_get(mission_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        with _connect(_db_path(hermes_root), write=False) as db:
            value = _row_to_mission(db, _get_row(db, mission_id), include_events=True)
        _audit("hermes_mission_get", policy, dry_run=True, success=True, changed=False, mission_id=mission_id, summary="mission read")
        return json.dumps({"success": True, **value})
    except FileNotFoundError:
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_READ_FAILED", "Check mission id and Operator read access.")


def hermes_mission_list(status: str = "", limit: int = 50, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if status and status not in STATUSES:
            raise ValueError("status filter is invalid")
        limit = max(1, min(int(limit), MAX_LIST))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "missions": [], "count": 0})
        with _connect(path, write=False) as db:
            if status:
                rows = db.execute("SELECT * FROM missions WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            missions = [_row_to_mission(db, row) for row in rows]
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "missions": missions, "count": len(missions)})
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_LIST_FAILED", "Check status/limit and Operator read access.")


def hermes_mission_update(
    mission_id: str,
    patch_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        patch = json.loads(patch_json)
        if not isinstance(patch, dict):
            raise TypeError("patch_json must contain an object")
        _closed(patch, _ALLOWED_PATCH_KEYS, "mission patch")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission update requires confirm=true")
        path = _db_path(hermes_root)

        def validate(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
            if row["status"] in TERMINAL_STATUSES:
                raise ValueError("terminal missions cannot be edited")
            if any(key in patch for key in ("acceptance_criteria", "owner_profile")):
                attached = db.execute("SELECT 1 FROM attachments WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
                if row["status"] != "draft" or attached:
                    raise ValueError("acceptance criteria and owner_profile freeze once mission work is attached or started")
            old_spec = json.loads(row["spec_json"])
            candidate = dict(old_spec)
            candidate.update(patch)
            candidate["mission_id"] = mission_id
            return _normalize_spec(candidate, mission_id=mission_id)

        if effective_dry:
            with _connect(path, write=False) as db:
                spec = validate(_get_row(db, mission_id), db)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_update", "mission_id": mission_id, "changed": False, "dry_run": True, "spec": spec})
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = _get_row(db, mission_id)
            spec = validate(row, db)
            version = int(row["version"]) + 1
            now = _now()
            db.execute("UPDATE missions SET spec_json=?,version=?,updated_at=? WHERE mission_id=?", (json.dumps(spec, sort_keys=True), version, now, mission_id))
            live_notice = _event(db, mission_id, "mission.updated", details={"version": version, "fields": sorted(patch)})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_update", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission updated")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_update", "mission_id": mission_id, "version": version, "changed": True})
    except (ValueError, LookupError, TypeError, json.JSONDecodeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_UPDATE_REJECTED", "Check patch schema and mission lifecycle state.")

def hermes_mission_attach(
    mission_id: str,
    kind: str,
    ref: str,
    relationship: str = "contains",
    state: str = "unknown",
    evidence_ref: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if kind not in ATTACHMENT_KINDS:
            raise ValueError("attachment kind is invalid")
        ref = _bounded_text(ref, "attachment ref", 256, required=True)
        if not REF_RE.fullmatch(ref):
            raise ValueError("attachment ref contains unsupported characters")
        if kind == "workflow" and not WORKFLOW_REF_RE.fullmatch(ref):
            raise ValueError("workflow attachment ref must be a canonical workflow id")
        relationship = _bounded_text(relationship, "relationship", 64, required=True)
        if state not in ATTACHMENT_STATES:
            raise ValueError("attachment state is invalid")
        if state == "succeeded":
            raise PermissionError("public mission attachment cannot assert succeeded state")
        if state == "cancelled":
            raise PermissionError("public mission attachment cannot assert cancelled state")
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 256)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission attachment requires confirm=true")
        path = _db_path(hermes_root)

        def validate(db: sqlite3.Connection) -> None:
            mission_row = _get_row(db, mission_id)
            if mission_row["status"] in TERMINAL_STATUSES:
                raise ValueError("terminal missions cannot be modified")
            count = int(db.execute("SELECT COUNT(*) FROM attachments WHERE mission_id=?", (mission_id,)).fetchone()[0])
            exists = db.execute("SELECT 1 FROM attachments WHERE mission_id=? AND kind=? AND ref=?", (mission_id, kind, ref)).fetchone()
            if count >= MAX_ATTACHMENTS and not exists:
                raise ValueError("mission attachment cap reached")

        if effective_dry:
            with _connect(path, write=False) as db:
                validate(db)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_attach", "mission_id": mission_id, "kind": kind, "ref": ref, "changed": False, "dry_run": True})
        now = _now()
        with _connect(path, write=True) as db:
            _begin_write(db)
            validate(db)
            db.execute(
                "INSERT INTO attachments(mission_id,kind,ref,relationship,state,evidence_ref,verified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(mission_id,kind,ref) DO UPDATE SET relationship=excluded.relationship,state=excluded.state,evidence_ref=excluded.evidence_ref,verified=0,updated_at=excluded.updated_at",
                (mission_id, kind, ref, relationship, state, evidence_ref, 0, now, now),
            )
            db.execute("UPDATE missions SET version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
            live_notice = _event(db, mission_id, "mission.attachment", details={"kind": kind, "ref": ref, "relationship": relationship, "state": state})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_attach", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission attachment updated")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_attach", "mission_id": mission_id, "kind": kind, "ref": ref, "changed": True})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_ATTACH_REJECTED", "Check attachment kind/ref, cap, and mutation policy.")

def record_attachment_state(
    mission_id: str,
    kind: str,
    ref: str,
    state: str,
    *,
    evidence_ref: str = "",
    verified: bool = False,
    hermes_root: Path | None = None,
) -> bool:
    """Internal lifecycle bridge; successful state requires verified evidence."""
    if kind not in ATTACHMENT_KINDS or state not in ATTACHMENT_STATES:
        return False
    if kind == "workflow" and not WORKFLOW_REF_RE.fullmatch(ref):
        return False
    evidence_ref = evidence_ref[:256]
    if state == "succeeded" and (not verified or not evidence_ref):
        return False
    path = _db_path(hermes_root)
    if not path.is_file():
        return False
    try:
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = db.execute("SELECT 1 FROM attachments WHERE mission_id=? AND kind=? AND ref=?", (mission_id, kind, ref)).fetchone()
            if row is None:
                return False
            now = _now()
            verified_value = 1 if verified and state == "succeeded" else 0
            db.execute("UPDATE attachments SET state=?,evidence_ref=?,verified=?,updated_at=? WHERE mission_id=? AND kind=? AND ref=?", (state, evidence_ref, verified_value, now, mission_id, kind, ref))
            db.execute("UPDATE missions SET version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
            live_notice = _event(db, mission_id, "mission.attachment_state", details={"kind": kind, "ref": ref, "state": state, "verified": bool(verified_value)})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        return True
    except (OSError, sqlite3.Error):
        return False


def reserve_delegation_attachment(
    mission_id: str,
    delegation_id: str,
    *,
    evidence_ref: str,
    hermes_root: Path | None = None,
) -> bool:
    """Private reservation bridge committed before any delegation backend call.

    Repeating the same reservation is idempotent.  Existing lifecycle state is
    never downgraded, and a terminal Mission cannot acquire new work.
    """
    if not REF_RE.fullmatch(delegation_id) or not evidence_ref:
        return False
    path = _db_path(hermes_root)
    if not path.is_file():
        return False
    try:
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission = _get_row(db, mission_id)
            existing = db.execute(
                "SELECT state,evidence_ref FROM attachments WHERE mission_id=? AND kind='delegation' AND ref=?",
                (mission_id, delegation_id),
            ).fetchone()
            if mission["status"] in TERMINAL_STATUSES and existing is None:
                db.rollback()
                return False
            if existing is not None:
                if str(existing["evidence_ref"] or "") not in {"", evidence_ref}:
                    db.rollback()
                    return False
                db.commit()
                return True
            count = int(db.execute("SELECT COUNT(*) FROM attachments WHERE mission_id=?", (mission_id,)).fetchone()[0])
            if count >= MAX_ATTACHMENTS:
                db.rollback()
                return False
            now = _now()
            db.execute(
                "INSERT INTO attachments(mission_id,kind,ref,relationship,state,evidence_ref,verified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mission_id, "delegation", delegation_id, "contains", "pending", evidence_ref[:256], 0, now, now),
            )
            db.execute("UPDATE missions SET version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
            _event(db, mission_id, "mission.delegation_reserved", details={"kind": "delegation", "ref": delegation_id})
            db.commit()
        return True
    except (LookupError, OSError, sqlite3.Error):
        return False

# Existing callers and concurrency tests use these runtime-level helper names.
# Keep the stable names while storing child observation policy in its own module.
_workflow_state = mission_observations.workflow_state
_delegation_state = mission_observations.delegation_state
_observe_attachments = mission_observations.observe_attachments
_completion_guard = mission_observations.completion_guard
_cancellation_guard = mission_observations.cancellation_guard
_desired_mission_status = mission_observations.desired_status

def hermes_mission_reconcile(
    mission_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission reconciliation requires confirm=true")
        path = _db_path(hermes_root)
        root = _root(hermes_root)
        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            observed = _observe_attachments(root, mission)
            desired = _desired_mission_status(mission, observed)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_reconcile", "mission_id": mission_id, "status": mission["status"], "desired_status": desired, "observed": observed, "changed": False, "dry_run": True})

        # Completion is linearized against delegation cancellation authority.
        # Observe without holding either write lock, then hold the short-lived
        # delegation guard while CAS-checking and committing the Mission row.
        with _connect(path, write=False) as db:
            completion_mission = _row_to_mission(db, _get_row(db, mission_id))
        completion_observed = _observe_attachments(root, completion_mission)
        if (
            completion_mission["status"] not in TERMINAL_STATUSES
            and _desired_mission_status(completion_mission, completion_observed) == "completed"
        ):
            live_notice: dict[str, Any] | None = None
            with _completion_guard(root, mission_id, completion_observed), _connect(path, write=True) as db:
                _begin_write(db)
                current_row = _get_row(db, mission_id)
                if int(current_row["version"]) != int(completion_mission["version"]):
                    raise ValueError("Mission authority changed after child observation")
                current = str(current_row["status"])
                now = _now()
                for item in completion_observed:
                    db.execute("UPDATE attachments SET state=?,verified=?,updated_at=? WHERE mission_id=? AND kind=? AND ref=?", (item["state"], 1 if item["verified"] else 0, now, mission_id, item["kind"], item["ref"]))
                db.execute("UPDATE missions SET status='completed',version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
                live_notice = _event(db, mission_id, "mission.reconciled", from_status=current, to_status="completed", details={"observed": completion_observed})
                db.commit()
            _publish_live_event(live_notice, hermes_root)
            _audit("hermes_mission_reconcile", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary=f"mission reconciled {completion_mission['status']}->completed")
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_reconcile", "mission_id": mission_id, "status": "completed", "observed": completion_observed, "changed": True})

        # Observe potentially slow external attachment stores without holding
        # the Mission write lock, then commit through the Mission version CAS.
        mission = completion_mission
        observed = completion_observed
        current = str(mission["status"])
        desired = _desired_mission_status(mission, observed)
        live_notice: dict[str, Any] | None = None
        with _connect(path, write=True) as db:
            _begin_write(db)
            current_row = _get_row(db, mission_id)
            if int(current_row["version"]) != int(mission["version"]):
                raise ValueError("Mission authority changed after child observation")
            if desired == "completed":
                raise ValueError("Mission child observation changed during completion; retry reconciliation")
            original = {(a["kind"], a["ref"]): (a["state"], bool(a.get("verified"))) for a in mission["attachments"]}
            attachment_changed = any(original.get((item["kind"], item["ref"])) != (item["state"], bool(item["verified"])) for item in observed)
            status_changed = desired != current
            changed = attachment_changed or status_changed
            if changed:
                now = _now()
                for item in observed:
                    db.execute("UPDATE attachments SET state=?,verified=?,updated_at=? WHERE mission_id=? AND kind=? AND ref=?", (item["state"], 1 if item["verified"] else 0, now, mission_id, item["kind"], item["ref"]))
                db.execute("UPDATE missions SET status=?,version=version+1,updated_at=? WHERE mission_id=?", (desired, now, mission_id))
                live_notice = _event(db, mission_id, "mission.reconciled", from_status=current, to_status=desired, details={"observed": observed})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_reconcile", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission reconciled {current}->{desired}")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_reconcile", "mission_id": mission_id, "status": desired, "observed": observed, "changed": changed})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_RECONCILE_FAILED", "Check mission attachments and lifecycle state.")

def hermes_mission_transition(
    mission_id: str,
    status: str,
    reason: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        if status not in STATUSES:
            raise ValueError("target mission status is invalid")
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        reason = _bounded_text(reason, "reason", 1000)
        effective_dry = policy.effective_dry_run(dry_run)
        if status == "completed":
            policy.require_owner(dry_run)
        if status == "awaiting_approval":
            raise ValueError("awaiting_approval is entered only by reconciliation")
        if not effective_dry and not confirm:
            raise PermissionError("direct mission transition requires confirm=true")
        path = _db_path(hermes_root)

        def validate(mission: dict[str, Any], observed: list[dict[str, Any]] | None = None) -> tuple[str, bool]:
            current = str(mission["status"])
            if current in TERMINAL_STATUSES:
                raise ValueError("terminal mission status cannot transition")
            if status == current:
                return current, False
            if status == "cancelled":
                if observed is None:
                    observed = _observe_attachments(_root(hermes_root), mission)
                active = [a for a in observed if a["kind"] == "delegation" and a["state"] in {"pending", "running", "blocked"}]
                if active:
                    raise ValueError("mission cancellation requires all delegation children to be terminal")
            allowed = MISSION_TRANSITIONS.get(current, set())
            if status != "completed" and status not in allowed:
                raise ValueError(f"mission transition {current}->{status} is not allowed")
            if status == "completed":
                if mission["final_approval_required"]:
                    raise PermissionError("approval-required missions complete only through hermes_mission_approve")
                observed = _observe_attachments(_root(hermes_root), mission)
                if _desired_mission_status(mission, observed) != "completed":
                    raise ValueError("mission children do not justify completion")
            return current, True

        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            current, would_change = validate(mission)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": status, "changed": False, "would_change": would_change, "dry_run": True})
        if status == "completed":
            root = _root(hermes_root)
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            current, changed = validate(mission)
            observed = _observe_attachments(root, mission)
            if _desired_mission_status(mission, observed) != "completed":
                raise ValueError("mission children do not justify completion")
            live_notice: dict[str, Any] | None = None
            with _completion_guard(root, mission_id, observed), _connect(path, write=True) as db:
                _begin_write(db)
                current_row = _get_row(db, mission_id)
                if int(current_row["version"]) != int(mission["version"]):
                    raise ValueError("Mission authority changed after child observation")
                now = _now()
                db.execute("UPDATE missions SET status='completed',version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
                live_notice = _event(db, mission_id, "mission.transition", from_status=current, to_status="completed", reason=reason)
                db.commit()
            _publish_live_event(live_notice, hermes_root)
            _audit("hermes_mission_transition", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission {current}->completed")
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": "completed", "changed": changed})
        if status == "cancelled":
            root = _root(hermes_root)
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            observed = _observe_attachments(root, mission)
            current, changed = validate(mission, observed)
            live_notice: dict[str, Any] | None = None
            with _cancellation_guard(root, mission_id, observed), _connect(path, write=True) as db:
                _begin_write(db)
                current_row = _get_row(db, mission_id)
                if int(current_row["version"]) != int(mission["version"]):
                    raise ValueError("Mission authority changed after child cancellation observation")
                if changed:
                    now = _now()
                    db.execute("UPDATE missions SET status='cancelled',version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
                    live_notice = _event(db, mission_id, "mission.transition", from_status=current, to_status="cancelled", reason=reason)
                db.commit()
            _publish_live_event(live_notice, hermes_root)
            _audit("hermes_mission_transition", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission {current}->cancelled")
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": "cancelled", "changed": changed})
        live_notice: dict[str, Any] | None = None
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission = _row_to_mission(db, _get_row(db, mission_id))
            current, changed = validate(mission)
            if changed:
                now = _now()
                db.execute("UPDATE missions SET status=?,version=version+1,updated_at=? WHERE mission_id=?", (status, now, mission_id))
                live_notice = _event(db, mission_id, "mission.transition", from_status=current, to_status=status, reason=reason)
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_transition", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission {current}->{status}")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": status, "changed": changed})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_TRANSITION_REJECTED", "Check mission lifecycle and Owner approval requirements.")

def hermes_mission_approve(
    mission_id: str,
    approval_reference: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_owner(dry_run)
        approval_reference = _bounded_text(approval_reference, "approval_reference", 256, required=True)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission approval requires confirm=true")
        path = _db_path(hermes_root)

        root = _root(hermes_root)

        def validate(mission: dict[str, Any]) -> list[dict[str, Any]]:
            if mission["status"] != "awaiting_approval":
                raise ValueError("mission must be awaiting_approval before Owner approval")
            observed = _observe_attachments(root, mission)
            succeeded = [a for a in observed if a["state"] == "succeeded" and bool(a.get("verified"))]
            invalid = [a for a in observed if a["state"] not in {"succeeded", "cancelled"} or (a["state"] == "succeeded" and not bool(a.get("verified")))]
            if invalid or not succeeded:
                raise ValueError("mission children are not currently terminal with at least one verified success")
            return observed

        approval = {"approved": True, "approved_by": "owner", "approval_reference": approval_reference, "approved_at": _now()}
        target = "completed"
        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            validate(mission)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_approve", "mission_id": mission_id, "status": target, "approval": approval, "changed": False, "dry_run": True})
        with _connect(path, write=False) as db:
            mission = _row_to_mission(db, _get_row(db, mission_id))
        observed = validate(mission)
        with _completion_guard(root, mission_id, observed), _connect(path, write=True) as db:
            _begin_write(db)
            current_row = _get_row(db, mission_id)
            if int(current_row["version"]) != int(mission["version"]):
                raise ValueError("Mission authority changed after child observation")
            current = str(current_row["status"])
            now = _now()
            db.execute("UPDATE missions SET status=?,approval_json=?,version=version+1,updated_at=? WHERE mission_id=?", (target, json.dumps(approval, sort_keys=True), now, mission_id))
            live_notice = _event(db, mission_id, "mission.approved", from_status=current, to_status=target, details={"approval_reference": approval_reference})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_approve", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission Owner-approved")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_approve", "mission_id": mission_id, "status": target, "approval": approval, "changed": True})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_APPROVAL_REJECTED", "Reconcile verified child work and use Owner direct mode with confirm=true.")
