"""SQLite persistence and row decoding for durable Hermes missions."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import operator_mission_spec as mission_spec
import operator_policy as op

SCHEMA_VERSION = "0.9-mission.2"
MISSION_SCHEMA = "hermes.mission/v1"
MISSION_EVENT_SCHEMA = "hermes.mission-event/v1"


def root(hermes_root: Path | None) -> Path:
    if hermes_root is not None:
        return Path(hermes_root)
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    return Path.home() / ".hermes"


def db_path(hermes_root: Path | None) -> Path:
    return root(hermes_root) / "missions" / "missions.db"


def connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        init_db(db)
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            approval_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL,
            relationship TEXT NOT NULL,
            state TEXT NOT NULL,
            evidence_ref TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, kind, ref),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS mission_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            reason_sha256 TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_attachments_mission ON attachments(mission_id, kind,state);
        CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events(mission_id, seq);
        """
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(attachments)").fetchall()}
    if "verified" not in columns:
        db.execute("ALTER TABLE attachments ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    db.commit()


def begin_write(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")


def row_to_mission(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_events: bool = False,
) -> dict[str, Any]:
    spec = json.loads(row["spec_json"])
    attachments = [
        dict(attachment)
        for attachment in db.execute(
            "SELECT kind,ref,relationship,state,evidence_ref,verified,created_at,updated_at "
            "FROM attachments WHERE mission_id=? ORDER BY kind,ref",
            (row["mission_id"],),
        ).fetchall()
    ]
    approval = json.loads(row["approval_json"] or "{}")
    value: dict[str, Any] = {
        "schema": MISSION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        **spec,
        "status": row["status"],
        "version": int(row["version"]),
        "approval": approval,
        "attachments": attachments,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_events:
        value["events"] = [
            {
                "schema": MISSION_EVENT_SCHEMA,
                **dict(event),
                "details": json.loads(event["details_json"] or "{}"),
            }
            for event in db.execute(
                "SELECT seq,event_type,from_status,to_status,reason_sha256,details_json,created_at "
                "FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT 200",
                (row["mission_id"],),
            ).fetchall()
        ]
    return value


def get_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not mission_spec.MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
    if row is None:
        raise LookupError(f"mission {mission_id!r} not found")
    return row
