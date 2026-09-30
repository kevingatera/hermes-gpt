"""Read-only session store, filtering, and projection tests."""

import json

import pytest

from hermes_gpt.sessions import history as session_history
from hermes_gpt.server import app as server
from tests.support.fakes import FakeSessionConnection, FakeSessionDB, session_db_factory


def test_phase1_adapter_opens_read_only_and_disposes_raw_connection_once():
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    factory, captured = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()

    assert captured["kwargs"]["read_only"] is True
    assert captured["kwargs"]["db_path"].name == "state.db"
    adapter.dispose_safely()
    adapter.dispose_safely()
    assert connection.close_calls == 1
    assert fake_db.close_calls == 0
    assert fake_db._conn is connection
    assert fake_db._fts_enabled is False
    assert fake_db._trigram_available is False
    assert connection.executed == []


def test_phase1_adapter_disposes_on_exception_path():
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    factory, _ = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()

    try:
        raise ValueError("C:\\Users\\example\\secret-token=sk-test-value")
    except ValueError as exc:
        adapter.dispose_safely()
        assert "[REDACTED_PATH]" in session_history.redact_error(exc)
    assert connection.close_calls == 1


def test_phase1_adapter_context_manager_disposes_on_success_and_exception():
    success_connection = FakeSessionConnection()
    success_db = FakeSessionDB(success_connection)
    success_factory, _ = session_db_factory(success_db)
    with server.ReadOnlySessionAdapter(
        db_factory=success_factory,
        connection_type=FakeSessionConnection,
    ) as adapter:
        assert adapter is not None
    assert success_connection.close_calls == 1

    error_connection = FakeSessionConnection()
    error_db = FakeSessionDB(error_connection)
    error_factory, _ = session_db_factory(error_db)
    with (
        pytest.raises(RuntimeError, match="expected"),
        server.ReadOnlySessionAdapter(
            db_factory=error_factory,
            connection_type=FakeSessionConnection,
        ),
    ):
        raise RuntimeError("expected")
    assert error_connection.close_calls == 1


def test_phase1_adapter_uses_only_verified_public_data_methods():
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    factory, _ = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()

    assert adapter.list_sessions(limit=3, offset=4) == []
    assert adapter.resolve_session_id("session-1") == "session-1"
    assert adapter.get_messages("session-1", limit=5, offset=6) == []
    assert adapter.export_session("session-1") is None
    assert adapter.export_session_lineage("session-1") is None
    assert [name for name, _ in fake_db.calls] == [
        "list_sessions_rich",
        "resolve_session_id",
        "get_messages",
        "export_session",
        "export_session_lineage",
    ]
    assert fake_db.calls[0][1]["compact_rows"] is True


@pytest.mark.parametrize("value", [-1, -10])
def test_phase1_bounds_reject_negative_values(value):
    with pytest.raises(ValueError):
        session_history.validate_limit(value, "limit", session_history.MAX_PAGE_SIZE)
    with pytest.raises(ValueError):
        session_history.validate_offset(value)


def test_phase1_bounds_enforce_maxima_and_ids():
    assert (
        session_history.validate_limit(
            session_history.MAX_PAGE_SIZE,
            "limit",
            session_history.MAX_PAGE_SIZE,
        )
        == session_history.MAX_PAGE_SIZE
    )
    assert (
        session_history.validate_offset(session_history.MAX_OFFSET)
        == session_history.MAX_OFFSET
    )
    with pytest.raises(ValueError):
        session_history.validate_limit(
            session_history.MAX_PAGE_SIZE + 1, "limit", session_history.MAX_PAGE_SIZE
        )
    with pytest.raises(ValueError):
        session_history.validate_offset(session_history.MAX_OFFSET + 1)
    with pytest.raises(ValueError):
        session_history.validate_session_id("")
    with pytest.raises(ValueError):
        session_history.validate_session_id("x" * (session_history.MAX_ID_LENGTH + 1))
    assert session_history.validate_query("  query  ") == "query"
    with pytest.raises(ValueError):
        session_history.validate_query("")
    with pytest.raises(ValueError):
        session_history.validate_query("q" * (session_history.MAX_QUERY_LENGTH + 1))


def test_phase1_elevated_content_gate_and_default_roles(monkeypatch):
    monkeypatch.delenv(session_history.INTERNAL_CONTENT_ENV, raising=False)
    assert session_history.allowed_message_roles() == {"user", "assistant"}
    with pytest.raises(RuntimeError, match=session_history.INTERNAL_CONTENT_ENV):
        session_history.allowed_message_roles(include_system_messages=True)
    with pytest.raises(RuntimeError, match=session_history.INTERNAL_CONTENT_ENV):
        session_history.allowed_message_roles(include_tool_messages=True)
    monkeypatch.setenv(session_history.INTERNAL_CONTENT_ENV, "1")
    assert session_history.allowed_message_roles(
        include_system_messages=True,
        include_tool_messages=True,
    ) == {"user", "assistant", "system", "tool", "function"}


def test_phase1_projection_and_redaction_helpers():
    metadata = session_history.safe_session_metadata(
        {
            "id": "session-1",
            "source": "cli",
            "started_at": 1.0,
            "ended_at": 2.0,
            "message_count": 3,
            "title": "private title",
            "system_prompt": "do not expose",
            "cwd": r"C:\Users\example\private",
        }
    )
    assert metadata["id"] == "session-1"
    assert "system_prompt" not in metadata
    assert "cwd" not in metadata
    assert metadata["has_title"] is True

    assert (
        session_history.safe_message(
            {"id": 1, "session_id": "session-1", "role": "system", "content": "secret"},
            {"user", "assistant"},
        )
        is None
    )
    message = session_history.safe_message(
        {
            "id": 2,
            "session_id": "session-1",
            "role": "user",
            "content": "token=sk-test-value",
        },
        {"user", "assistant"},
    )
    assert message["session_id"] == "session-1"
    assert "***" not in message["content"]


def test_phase1_safe_message_cannot_bypass_internal_gate(monkeypatch):
    monkeypatch.delenv(session_history.INTERNAL_CONTENT_ENV, raising=False)
    with pytest.raises(RuntimeError, match=session_history.INTERNAL_CONTENT_ENV):
        session_history.safe_message(
            {"role": "system", "session_id": "session-1", "content": "hidden"},
            {"system"},
        )


def test_phase1_adapter_projects_raw_rows_before_returning(monkeypatch):
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "system",
                "content": "hidden",
                "system_prompt": "secret",
            },
            {
                "id": 2,
                "session_id": "session-1",
                "role": "user",
                "content": "token=sk-test-value",
                "tool_calls": "private",
            },
        ],
    )
    factory, _ = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()

    rows = adapter.get_messages("session-1", limit=10, offset=0)
    assert rows == [
        {
            "id": 2,
            "session_id": "session-1",
            "role": "user",
            "content": "token=[REDACTED]",
        }
    ]
    assert "system_prompt" not in rows[0]
    assert "tool_calls" not in rows[0]

    monkeypatch.setenv(session_history.INTERNAL_CONTENT_ENV, "1")
    rows = adapter.get_messages(
        "session-1",
        limit=10,
        offset=0,
        include_system_messages=True,
    )
    assert {row["role"] for row in rows} == {"system", "user"}


def test_phase1_adapter_lifecycle_edge_cases():
    class NoConnectionDB:
        pass

    class NonSqliteConnectionDB:
        _conn = object()

    for fake_db in (NoConnectionDB(), NonSqliteConnectionDB()):
        factory, _ = session_db_factory(fake_db)
        adapter = server.ReadOnlySessionAdapter(db_factory=factory).open()
        adapter.dispose_safely()
        adapter.dispose_safely()

    class RaisingConnection(FakeSessionConnection):
        def close(self):
            self.close_calls += 1
            raise OSError("C:\\Users\\example\\private-token=sk-test-value")

    raising_connection = RaisingConnection()
    raising_db = FakeSessionDB(raising_connection)
    factory, _ = session_db_factory(raising_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=RaisingConnection,
    ).open()
    adapter.dispose_safely()
    adapter.dispose_safely()
    assert raising_connection.close_calls == 1
    assert raising_db.close_calls == 0


def test_phase1_adapter_open_failure_is_redacted():
    def failing_factory(**kwargs):
        raise OSError("C:\\Users\\example\\private-token=sk-test-value")

    adapter = server.ReadOnlySessionAdapter(db_factory=failing_factory)
    with pytest.raises(RuntimeError) as exc_info:
        adapter.open()
    assert "[REDACTED_PATH]" in str(exc_info.value)
    assert "sk-test-value" not in str(exc_info.value)
    adapter.dispose_safely()
    adapter.dispose_safely()


def test_phase1_recursive_redaction_covers_nested_values_and_safe_ids():
    value = {
        "session_id": "session-1",
        "nested": {
            "api_key": "provider-secret-value",
            "items": [
                "C:\\Users\\example\\private.txt",
                "/home/example/private.txt",
                (
                    "https://example.test/?api_key=provider-secret-value",
                    "sk-test-provider-key-value-123456",
                ),
            ],
        },
    }
    redacted = session_history.redact_value(value)
    assert redacted["session_id"] == "session-1"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["items"][0] == "[REDACTED_PATH]"
    assert redacted["nested"]["items"][1] == "[REDACTED_PATH]"
    assert "provider-secret-value" not in json.dumps(redacted)
    assert "sk-test-provider-key" not in json.dumps(redacted)


def test_phase1_bool_validation_and_archived_forwarding():
    with pytest.raises(ValueError):
        session_history.validate_bool(1, "include_archived")
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    factory, _ = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()
    adapter.list_sessions(limit=20, offset=0, include_archived=True)
    assert fake_db.calls[0][1]["include_archived"] is True


def test_phase3_filtered_role_pagination_advances_by_examined_rows():
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {"id": 1, "session_id": "s", "role": "system", "content": "hidden"},
            {"id": 2, "session_id": "s", "role": "user", "content": "one"},
            {"id": 3, "session_id": "s", "role": "assistant", "content": "two"},
            {"id": 4, "session_id": "s", "role": "user", "content": "three"},
        ],
    )
    factory, _ = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
    ).open()

    first = adapter.get_messages_page("s", limit=2, offset=0)
    second = adapter.get_messages_page("s", limit=2, offset=first["next_offset"])
    assert [row["id"] for row in first["messages"]] == [2, 3]
    assert first["rows_examined"] == 3
    assert first["next_offset"] == 3
    assert [row["id"] for row in second["messages"]] == [4]
    assert second["next_offset"] == 4
    assert second["has_more"] is False


def test_phase1_utf8_response_bytes():
    assert session_history.utf8_response_bytes("abc") == 3
    assert session_history.utf8_response_bytes("é") == 2
    assert session_history.utf8_response_bytes("🙂") == 4


def test_profile_aware_session_adapter_uses_named_profile_state_db(
    monkeypatch, tmp_path
):
    root = tmp_path / "profile-aware-hermes"
    profile_home = root / "profiles" / "project-manager"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: root)
    monkeypatch.setenv(
        server.op_policy.OPERATOR_ALLOWED_PROFILES_ENV, "default,project-manager"
    )

    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    factory, captured = session_db_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=FakeSessionConnection,
        profile="project-manager",
    ).open()

    assert captured["kwargs"]["read_only"] is True
    assert captured["kwargs"]["db_path"] == profile_home / "state.db"
    adapter.dispose_safely()


def test_profile_aware_session_adapter_rejects_unallowed_profile(monkeypatch, tmp_path):
    root = tmp_path / "profile-aware-hermes-denied"
    (root / "profiles" / "project-manager").mkdir(parents=True)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: root)
    monkeypatch.setenv(server.op_policy.OPERATOR_ALLOWED_PROFILES_ENV, "default")

    with pytest.raises(PermissionError):
        server.ReadOnlySessionAdapter(profile="project-manager")
