"""SQLite persistence, schema, and bounded record helpers for delegations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op

SCHEMA_VERSION = "0.9-delegation.2"

DELEGATION_SCHEMA = "hermes.delegation/v1"

DELEGATION_ID_RE = re.compile(r"^dlg-[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")

STATES = frozenset(
    {"reserved", "queued", "running", "reconciling", "succeeded", "failed", "cancelled"}
)

DISPATCH_PHASES = frozenset({"reserved", "invoking", "dispatched", "cancelled"})

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

MAX_LIST = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    if hermes_root is not None:
        normalized = op.normalize_hermes_data_root(Path(hermes_root).expanduser())
        return Path(normalized or hermes_root)
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        normalized = op.normalize_hermes_data_root(Path(raw).expanduser())
        if normalized is not None:
            return Path(normalized)
    return Path.home() / ".hermes"


def _db_path(hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / "delegations" / "delegations.db"


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(path)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _init(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS delegations (
            delegation_id TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            mission_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL UNIQUE,
            contract_sha256 TEXT NOT NULL,
            backend TEXT NOT NULL,
            state TEXT NOT NULL,
            backend_state TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            backend_ref_json TEXT NOT NULL DEFAULT '{}',
            validation_verdict TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            authority_version INTEGER NOT NULL DEFAULT 1,
            cancellation_in_progress INTEGER NOT NULL DEFAULT 0,
            cancellation_claimed_at TEXT NOT NULL DEFAULT '',
            cancellation_observation_sha256 TEXT NOT NULL DEFAULT '',
            cancellation_watermark_ready INTEGER NOT NULL DEFAULT 0,
            dispatch_phase TEXT NOT NULL DEFAULT 'dispatched',
            created_at TEXT NOT NULL,
            dispatched_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_delegations_mission ON delegations(mission_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_delegations_state ON delegations(state, updated_at);
        CREATE TABLE IF NOT EXISTS delegation_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            delegation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT NOT NULL DEFAULT '',
            to_state TEXT NOT NULL DEFAULT '',
            backend_state TEXT NOT NULL DEFAULT '',
            observed_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (delegation_id) REFERENCES delegations(delegation_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_delegation_events ON delegation_events(delegation_id, seq);
        CREATE TABLE IF NOT EXISTS delegation_validation_manifests (
            delegation_id TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            context_sha256 TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (delegation_id) REFERENCES delegations(delegation_id) ON DELETE CASCADE
        );
        """
    )
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(delegations)").fetchall()
    }
    if "dispatch_phase" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN dispatch_phase TEXT NOT NULL DEFAULT 'dispatched'"
        )
    if "authority_version" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN authority_version INTEGER NOT NULL DEFAULT 1"
        )
    if "cancellation_in_progress" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN cancellation_in_progress INTEGER NOT NULL DEFAULT 0"
        )
    if "cancellation_claimed_at" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN cancellation_claimed_at TEXT NOT NULL DEFAULT ''"
        )
    if "cancellation_observation_sha256" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN cancellation_observation_sha256 TEXT NOT NULL DEFAULT ''"
        )
    if "cancellation_watermark_ready" not in columns:
        db.execute(
            "ALTER TABLE delegations ADD COLUMN cancellation_watermark_ready INTEGER NOT NULL DEFAULT 0"
        )
    db.commit()


def _bounded(value: Any, maximum: int = 256) -> str:
    text = op.redact_output(str(value or "")).strip()
    return text if len(text) <= maximum else text[: maximum - 3] + "..."


def _new_id(contract_sha256: str, task_id: str) -> str:
    seed = f"{contract_sha256}\0{task_id}\0{_now()}".encode()
    return f"dlg-{hashlib.sha256(seed).hexdigest()[:20]}"


def _normalize_state(value: Any) -> str:
    state = str(value or "").strip().lower().replace("-", "_")
    if state in {"queued", "accepted", "pending", "created", "submitted"}:
        return "queued"
    if state in {"running", "active", "in_progress", "started"}:
        return "running"
    if state in {"completed", "complete", "succeeded", "success", "done", "satisfied"}:
        return "succeeded"
    if state in {"failed", "failure", "error", "errored", "not_satisfied"}:
        return "failed"
    if state in {"cancelled", "canceled", "cancel_requested"}:
        return "cancelled" if state != "cancel_requested" else "running"
    if state in {"reconciling", "ambiguous", "unknown", "unavailable"}:
        return "reconciling"
    return "reconciling"


def _backend_ref(payload: dict[str, Any]) -> dict[str, str]:
    allowed = (
        "job_id",
        "dispatch_id",
        "attempt_id",
        "a2a_task_id",
        "node",
        "selected_node",
        "remote_backend",
    )
    out: dict[str, str] = {}
    for key in allowed:
        value = payload.get(key)
        if value is not None and str(value).strip():
            out[key] = _bounded(value, 192)
    return out


def _surface(
    row: sqlite3.Row | dict[str, Any], *, events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    value = dict(row)
    try:
        backend_ref = json.loads(value.pop("backend_ref_json", "{}") or "{}")
    except ValueError:
        backend_ref = {}
    value["backend_ref"] = backend_ref if isinstance(backend_ref, dict) else {}
    value["cancel_requested"] = bool(value.get("cancel_requested"))
    value["cancellation_in_progress"] = bool(value.get("cancellation_in_progress"))
    value["cancellation_watermark_ready"] = bool(
        value.get("cancellation_watermark_ready")
    )
    if events is not None:
        value["events"] = events
    return value


def _event(
    db: sqlite3.Connection,
    delegation_id: str,
    event_type: str,
    *,
    from_state: str = "",
    to_state: str = "",
    backend_state: str = "",
    observed: dict[str, Any] | None = None,
) -> None:
    observed_sha = ""
    if observed:
        encoded = json.dumps(
            observed, sort_keys=True, default=str, separators=(",", ":")
        )
        observed_sha = hashlib.sha256(encoded.encode()).hexdigest()
    db.execute(
        "INSERT INTO delegation_events(delegation_id,event_type,from_state,to_state,backend_state,observed_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            delegation_id,
            event_type,
            from_state,
            to_state,
            _bounded(backend_state, 128),
            observed_sha,
            _now(),
        ),
    )


def _live_event(
    kind: str, row: dict[str, Any], *, hermes_root: Path | None = None
) -> None:
    try:
        import operator_live_events as live_events

        live_events.publish_event(
            topic="delegation",
            kind=kind,
            subject_type="delegation",
            subject_id=str(row["delegation_id"]),
            mission_id=str(row.get("mission_id") or ""),
            source="delegation-runtime",
            payload={
                "task_id": row.get("task_id"),
                "backend": row.get("backend"),
                "state": row.get("state"),
                "backend_state": row.get("backend_state"),
                "validation_verdict": row.get("validation_verdict"),
            },
            hermes_root=_root(hermes_root),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    delegation_id: str = "",
    task_id: str = "",
    backend: str = "",
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"delegation lifecycle {tool}",
            extra={
                "delegation_id": delegation_id,
                "task_id": task_id,
                "backend": backend,
            },
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


def _get_row(db: sqlite3.Connection, delegation_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"delegation {delegation_id!r} was not found")
    return row


def _manifest_row(db: sqlite3.Connection, delegation_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT schema,context_sha256,manifest_json,created_at,updated_at FROM delegation_validation_manifests WHERE delegation_id=?",
        (delegation_id,),
    ).fetchone()
    if row is None:
        raise LookupError("delegation validation manifest is missing")
    try:
        manifest = json.loads(row["manifest_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("delegation validation manifest is corrupt") from exc
    if (
        manifest.get("schema") != row["schema"]
        or manifest.get("context_sha256") != row["context_sha256"]
    ):
        raise ValueError("delegation validation manifest metadata mismatch")
    return manifest
