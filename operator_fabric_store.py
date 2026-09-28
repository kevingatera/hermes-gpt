"""SQLite paths and connection setup for Fabric journals."""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import operator_policy as op
from operator_fabric_config import _root
from operator_fabric_protocol import FabricError


def _db_path(env_name: str, default_name: str, hermes_root: Path | None = None) -> Path:
    """Resolve a Fabric journal path and reject unsafe destinations."""
    configured = os.environ.get(env_name, "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else _root(hermes_root) / "fabric" / default_name
    )
    if not path.is_absolute() or op.is_denied_path(path) or path.is_symlink():
        raise FabricError("FABRIC_JOURNAL_PATH_INVALID", "Fabric journal path is not allowed")
    return path


def _prepare_db_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        return


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open and close a write-capable Fabric journal connection.

    A SQLite connection context manager handles commit/rollback but does not
    close the connection. Closing here keeps WAL cleanup inside the operation
    boundary instead of deferring it to garbage collection.
    """
    db = sqlite3.connect(path, timeout=5.0)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def _connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open and close a query-only Fabric journal connection."""
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _init_coordinator_db(path: Path) -> None:
    _prepare_db_parent(path)
    with _connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS dispatches (
              dispatch_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              node_name TEXT NOT NULL,
              evidence_policy_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS dispatches_task_idx ON dispatches(task_id);
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
              envelope_sha256 TEXT NOT NULL,
              node_name TEXT NOT NULL,
              peer_name TEXT NOT NULL,
              remote_backend TEXT NOT NULL,
              coordinator_principal TEXT NOT NULL,
              capability_sha256 TEXT NOT NULL,
              peer_policy_sha256 TEXT,
              state TEXT NOT NULL,
              remote_task_id TEXT,
              evidence_json TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_dispatch_idx ON attempts(dispatch_id);
            """
        )


def _init_peer_db(path: Path) -> None:
    _prepare_db_parent(path)
    with _connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL,
              envelope_sha256 TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              task_id TEXT NOT NULL,
              coordinator_principal TEXT NOT NULL,
              node_name TEXT NOT NULL,
              remote_backend TEXT NOT NULL,
              logical_workspace TEXT NOT NULL,
              conflict_domain TEXT NOT NULL,
              authorization_class TEXT NOT NULL,
              policy_sha256 TEXT NOT NULL,
              local_task_id TEXT NOT NULL,
              state TEXT NOT NULL,
              dispatch_result_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS write_claims (
              conflict_domain TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              state TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              released_at TEXT
            );
            """
        )


__all__ = [
    "_connect",
    "_connect_readonly",
    "_db_path",
    "_init_coordinator_db",
    "_init_peer_db",
    "_prepare_db_parent",
]
