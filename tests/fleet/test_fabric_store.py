from __future__ import annotations

import sqlite3

import pytest

from hermes_gpt.fleet import fabric_store as store


def test_store_initializes_journal_tables_and_readonly_connection_rejects_writes(
    tmp_path,
):
    path = tmp_path / "fabric" / "coordinator.db"

    store._init_coordinator_db(path)

    with store._connect_readonly(path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        with pytest.raises(sqlite3.OperationalError):
            db.execute("CREATE TABLE forbidden_write (id INTEGER)")

    assert {"dispatches", "attempts"}.issubset(tables)
    assert path.parent.stat().st_mode & 0o777 == 0o700
