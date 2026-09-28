"""Small fakes for Hermes session history tests."""

from __future__ import annotations


class FakeSessionConnection:
    def __init__(self):
        self.close_calls = 0
        self.executed = []

    def close(self):
        self.close_calls += 1

    def execute(self, *args, **kwargs):
        self.executed.append((args, kwargs))
        raise AssertionError("dispose_safely must not execute SQL or PRAGMA")


class FakeSessionDB:
    def __init__(
        self, connection, *, message_rows=None, session_rows=None, fts_enabled=False
    ):
        self._conn = connection
        self._fts_enabled = fts_enabled
        self._trigram_available = False
        self.close_calls = 0
        self.calls = []
        self.message_rows = list(message_rows or [])
        self.session_rows = list(session_rows or [])

    def close(self):
        self.close_calls += 1
        raise AssertionError("SessionDB.close() must not be called")

    def list_sessions_rich(self, **kwargs):
        self.calls.append(("list_sessions_rich", kwargs))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", len(self.session_rows))
        return list(self.session_rows[offset : offset + limit])

    def resolve_session_id(self, value):
        self.calls.append(("resolve_session_id", value))
        if value == "prefix":
            return "session-1"
        return None if value in {"missing", "ambiguous"} else value

    def get_session_by_title(self, title):
        self.calls.append(("get_session_by_title", title))
        for row in self.session_rows:
            if row.get("title") == title:
                return dict(row)
        return None

    def get_compression_tip(self, session_id):
        self.calls.append(("get_compression_tip", session_id))
        for row in self.session_rows:
            if row.get("_compression_tip_for") == session_id:
                return row.get("id")
        return session_id

    def get_session(self, session_id):
        self.calls.append(("get_session", session_id))
        for row in self.session_rows:
            if row.get("id") == session_id:
                return dict(row)
        return None

    def get_messages(self, session_id, **kwargs):
        self.calls.append(("get_messages", {"session_id": session_id, **kwargs}))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", len(self.message_rows))
        return list(self.message_rows[offset : offset + limit])

    def search_messages(self, **kwargs):
        self.calls.append(("search_messages", kwargs))
        return list(self.message_rows)

    def export_session(self, value):
        self.calls.append(("export_session", value))

    def export_session_lineage(self, value):
        self.calls.append(("export_session_lineage", value))


def session_db_factory(fake_db):
    captured = {}

    def factory(**kwargs):
        captured["kwargs"] = kwargs
        return fake_db

    return factory, captured
