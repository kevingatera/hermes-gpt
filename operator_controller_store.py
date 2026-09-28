"""SQLite persistence and trigger queue for the mission controller."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_failure_semantics as fs
import operator_mission_runtime as mission

# The trigger values are persisted in the queue and consumed by the controller.
MISSION_ID_RE = mission.MISSION_ID_RE

# Trigger names are part of the controller API and persisted queue format.
TRIGGER_PERIODIC = "T1_periodic"
TRIGGER_LIVE_EVENT = "T2_live_event"
TRIGGER_DEPENDENCY = "T3_dependency"
TRIGGER_HEALTH = "T4_health"
TRIGGER_MANUAL = "T5_manual"
TRIGGERS = (
    TRIGGER_PERIODIC,
    TRIGGER_LIVE_EVENT,
    TRIGGER_DEPENDENCY,
    TRIGGER_HEALTH,
    TRIGGER_MANUAL,
)

MAX_MISSIONS_PER_PASS = 64
MAX_TARGET_STRING = 256


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _root(hermes_root: Path | None) -> Path:
    return mission._root(hermes_root)


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _sanitize(text: Any, limit: int = MAX_TARGET_STRING) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    if len(value) > limit:
        return value[:limit] + "…"
    return value


# These tables store controller state only. Mission and plan records stay owned
# by their existing modules.
def _init_controller_tables(db: sqlite3.Connection) -> None:
    fs._init_tables(db)  # controller_plan (shared with failure semantics)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS controller_pass_lease (
            mission_id TEXT PRIMARY KEY,
            lease_lock TEXT NOT NULL,
            lease_expires TEXT NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT '',
            pass_seq INTEGER NOT NULL DEFAULT 0,
            recheck_needed INTEGER NOT NULL DEFAULT 0,
            node_id TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ctl_lease_expires ON controller_pass_lease(lease_expires);
        CREATE TABLE IF NOT EXISTS controller_telemetry (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT '',
            node_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            pass_result TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL DEFAULT '',
            row_key TEXT NOT NULL DEFAULT '',
            would_execute INTEGER NOT NULL DEFAULT 0,
            lease_acquired INTEGER NOT NULL DEFAULT 0,
            actions_taken_json TEXT NOT NULL DEFAULT '[]',
            need_attention INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ctl_telem_mission ON controller_telemetry(mission_id, created_at);
        CREATE TABLE IF NOT EXISTS controller_trigger_queue (
            mission_id TEXT PRIMARY KEY,
            trigger_kind TEXT NOT NULL,
            ref TEXT NOT NULL DEFAULT '',
            seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    # §12.2 telemetry additions (t_e8468723): lease_reclaimed (true reclaim
    # counting, distinct from conflation stale passes) + escalation_tier.
    # Additive ALTERs guarded by presence checks — safe on existing stores.
    telem_cols = {
        r["name"] for r in db.execute("PRAGMA table_info(controller_telemetry)")
    }
    if "lease_reclaimed" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN lease_reclaimed "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "escalation_tier" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN escalation_tier "
            "TEXT NOT NULL DEFAULT ''"
        )
    # v0.12 slice-2 (Pack B): execution-ledger columns for the L2 rung. A pass
    # that executes an action records the idempotency key + bounded outcome on
    # its telemetry row, and the plan row carries the execution linkage. Prior
    # execution is checked over BOTH surfaces (§2.2 idempotency).
    if "executed_idempotency_key" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN executed_idempotency_key "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "executed_result" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN executed_result "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "executed_target" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN executed_target "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "executed_refused_reason" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN executed_refused_reason "
            "TEXT NOT NULL DEFAULT ''"
        )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_controller_tables(db)
        return db
    return mission._connect(path, write=False)


def _now_iso_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expiry(ttl: float) -> str:
    return datetime.fromtimestamp(_now_ts() + ttl, tz=timezone.utc).isoformat()


def acquire_lease(
    db: sqlite3.Connection,
    mission_id: str,
    trigger_kind: str,
    *,
    ttl: float,
    lease_lock: str,
    node_id: str = "",
) -> dict[str, Any]:
    """Acquire (or reclaim) the per-mission pass lease.

    §7.3: a single row UPDATE guarded by ``(lease_expires IS NULL OR
    lease_expires < now)``; if the row is absent, a guarded INSERT. A live pass
    (un-expired lease) is never stolen — the acquire returns ``acquired=False``
    and the caller conflates. A crashed pass (no release, TTL expired) is
    reclaimed on the next acquire.
    """
    now = _now_iso_ts()
    expiry = _lease_expiry(ttl)
    now_iso = _now()
    # 1. Try to reclaim/renew an existing expired-or-empty lease via a single
    #    guarded UPDATE (rowcount is the authoritative signal).
    cur = db.execute(
        "UPDATE controller_pass_lease SET lease_lock=?, lease_expires=?, trigger_kind=?, "
        "pass_seq=pass_seq+1, heartbeat_at=?, node_id=?, updated_at=? "
        "WHERE mission_id=? AND (lease_expires IS NULL OR lease_expires < ?)",
        (lease_lock, expiry, trigger_kind, now_iso, node_id, now_iso, mission_id, now),
    )
    if cur.rowcount == 1:
        db.commit()
        seq = _lease_seq(db, mission_id)
        return {
            "acquired": True,
            "mission_id": mission_id,
            "lease_lock": lease_lock,
            "lease_expires": expiry,
            "pass_seq": seq,
            "recheck": False,
            "reclaimed": True,
            "ttl_seconds": ttl,
        }
    # 2. Row absent → guarded INSERT. IGNORE so a concurrently-held lease wins.
    cur = db.execute(
        "INSERT OR IGNORE INTO controller_pass_lease("
        "mission_id,lease_lock,lease_expires,trigger_kind,pass_seq,recheck_needed,node_id,heartbeat_at,created_at,updated_at) "
        "VALUES(?,?,?,?,1,0,?,?,?,?)",
        (
            mission_id,
            lease_lock,
            expiry,
            trigger_kind,
            node_id,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    if cur.rowcount == 1:
        db.commit()
        return {
            "acquired": True,
            "mission_id": mission_id,
            "lease_lock": lease_lock,
            "lease_expires": expiry,
            "pass_seq": 1,
            "recheck": False,
            "reclaimed": False,
            "ttl_seconds": ttl,
        }
    # 3. A live lease is held by another pass → do not steal; conflate.
    db.commit()
    holder = _lease_info(db, mission_id)
    return {
        "acquired": False,
        "mission_id": mission_id,
        "lease_lock": lease_lock,
        "lease_expires": "",
        "pass_seq": holder.get("pass_seq", 0),
        "recheck": True,
        "reclaimed": False,
        "holder_lock": holder.get("lease_lock", ""),
        "holder_expires": holder.get("lease_expires", ""),
    }


def renew_lease(
    db: sqlite3.Connection, mission_id: str, lease_lock: str, *, ttl: float
) -> dict[str, Any]:
    """Heartbeat renewal — only the current lock may renew (CAS)."""
    now = _now_iso_ts()
    expiry = _lease_expiry(ttl)
    cur = db.execute(
        "UPDATE controller_pass_lease SET lease_expires=?, heartbeat_at=?, updated_at=? "
        "WHERE mission_id=? AND lease_lock=? AND (lease_expires IS NULL OR lease_expires >= ?)",
        (expiry, now, now, mission_id, lease_lock, now),
    )
    db.commit()
    return {
        "renewed": cur.rowcount == 1,
        "lease_expires": expiry if cur.rowcount else "",
    }


def release_lease(
    db: sqlite3.Connection, mission_id: str, lease_lock: str
) -> dict[str, Any]:
    """Release the lease on pass completion (CAS on lock)."""
    cur = db.execute(
        "DELETE FROM controller_pass_lease WHERE mission_id=? AND lease_lock=?",
        (mission_id, lease_lock),
    )
    db.commit()
    return {"released": cur.rowcount == 1}


def _lease_seq(db: sqlite3.Connection, mission_id: str) -> int:
    row = db.execute(
        "SELECT pass_seq FROM controller_pass_lease WHERE mission_id=?", (mission_id,)
    ).fetchone()
    return int(row["pass_seq"]) if row else 0


def _lease_info(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT lease_lock,lease_expires,pass_seq,recheck_needed,node_id,heartbeat_at "
        "FROM controller_pass_lease WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "lease_lock": row["lease_lock"],
        "lease_expires": row["lease_expires"],
        "pass_seq": int(row["pass_seq"]),
        "recheck_needed": bool(row["recheck_needed"]),
        "node_id": row["node_id"],
        "heartbeat_at": row["heartbeat_at"],
    }


def mark_recheck(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    """Conflation (§7.1): a second trigger while a pass holds the lease."""
    cur = db.execute(
        "UPDATE controller_pass_lease SET recheck_needed=1, updated_at=? "
        "WHERE mission_id=? AND lease_expires >= ?",
        (_now(), mission_id, _now_iso_ts()),
    )
    db.commit()
    return {"recheck_needed": cur.rowcount == 1}


def trigger(
    mission_id: str,
    trigger_kind: str,
    ref: str = "",
    *,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Enqueue a work request (mission_id + trigger kind + monotonic seq).

    One row per mission: a second trigger overwrites (conflate) and, if a pass
    currently holds the lease, marks ``recheck_needed`` so the loop re-runs
    after the in-flight pass.
    """
    if not MISSION_ID_RE.fullmatch(mission_id or ""):
        raise ValueError("mission_id is invalid")
    if trigger_kind not in TRIGGERS:
        raise ValueError(f"trigger_kind must be one of {TRIGGERS}")
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        mission._get_row(db, mission_id)  # verify the mission exists
        row = db.execute(
            "SELECT seq FROM controller_trigger_queue WHERE mission_id=?", (mission_id,)
        ).fetchone()
        seq = (int(row["seq"]) if row else 0) + 1
        db.execute(
            "INSERT INTO controller_trigger_queue(mission_id,trigger_kind,ref,seq,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET "
            "trigger_kind=excluded.trigger_kind, ref=excluded.ref, seq=excluded.seq, "
            "created_at=excluded.created_at",
            (mission_id, trigger_kind, _sanitize(ref, 128), seq, _now()),
        )
        mark_recheck(db, mission_id)  # conflate a live pass if one is running
        db.commit()
    return {
        "enqueued": True,
        "mission_id": mission_id,
        "trigger_kind": trigger_kind,
        "seq": seq,
    }


def conflate(
    db: sqlite3.Connection, *, limit: int = MAX_MISSIONS_PER_PASS
) -> list[dict[str, Any]]:
    """§7.1: de-duplicate to one work request per mission (latest trigger wins)."""
    rows = db.execute(
        "SELECT mission_id,trigger_kind,ref,seq FROM controller_trigger_queue "
        "ORDER BY seq ASC LIMIT ?",
        (max(1, min(int(limit), MAX_MISSIONS_PER_PASS)),),
    ).fetchall()
    return [
        {
            "mission_id": r["mission_id"],
            "trigger_kind": r["trigger_kind"],
            "ref": r["ref"],
            "seq": int(r["seq"]),
        }
        for r in rows
    ]


def consume_trigger(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    cur = db.execute(
        "DELETE FROM controller_trigger_queue WHERE mission_id=?", (mission_id,)
    )
    db.commit()
    return {"consumed": cur.rowcount == 1}
