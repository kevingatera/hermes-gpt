"""MCP session history, paging, export, and search tests."""

import json
import sqlite3

import pytest

import hermes_session_history as session_history
import server
from server_test_helpers import clear_gate_envs, tool_names
from session_test_fakes import FakeSessionConnection, FakeSessionDB, session_db_factory


def test_phase2_session_list_projects_metadata_and_paginates(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        session_rows=[
            {
                "id": "session-1",
                "source": "cli",
                "started_at": 1.0,
                "ended_at": 2.0,
                "last_active": 2.0,
                "message_count": 3,
                "tool_call_count": 1,
                "title": "private",
                "preview": "private content",
                "system_prompt": "hidden",
                "cwd": r"C:\\Users\\example\\private",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_session_list(limit=20, offset=0))
    assert result["success"] is True
    assert result["offset"] == 0
    assert result["returned_count"] == 1
    assert result["has_more"] is False
    assert result["sessions"][0]["id"] == "session-1"
    assert result["sessions"][0]["has_title"] is True
    assert "title" not in result["sessions"][0]
    assert "preview" not in result["sessions"][0]
    assert "system_prompt" not in result["sessions"][0]
    assert "cwd" not in result["sessions"][0]
    assert fake_db.calls[0][1]["include_archived"] is False
    assert fake_db.close_calls == 0
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase2_bot_chat_get_resolves_hidden_registry_to_current_tip(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        session_rows=[
            {
                "id": "bot-root",
                "title": "Bot Chat",
                "hidden": 1,
                "source": "desktop",
                "started_at": 1.0,
                "ended_at": 2.0,
                "last_active": 2.0,
                "message_count": 40,
                "tool_call_count": 5,
                "archived": 0,
            },
            {
                "id": "bot-tip",
                "title": None,
                "hidden": 1,
                "source": "desktop",
                "started_at": 3.0,
                "ended_at": None,
                "last_active": 9.0,
                "message_count": 18,
                "tool_call_count": 2,
                "archived": 0,
                "_compression_tip_for": "bot-root",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_bot_chat_get())
    assert result["success"] is True
    assert result["profile"] == "default"
    assert result["kind"] == "bot_chat"
    assert result["canonical_title"] == "Bot Chat"
    assert result["registry_session_id"] == "bot-root"
    assert result["current_session_id"] == "bot-tip"
    assert result["compression_continuation"] is True
    assert result["session_list_visibility"] == "canonical_bot_chat_may_be_hidden"
    assert result["preferred_send_tool"] == "hermes_bot_chat_send"
    assert result["registry"]["id"] == "bot-root"
    assert result["current"]["id"] == "bot-tip"
    assert result["current"]["last_active"] == 9.0
    assert "title" not in result["registry"]
    assert "hidden" not in result["registry"]


def test_phase2_bot_chat_get_uses_exact_title_even_when_current_row_is_visible(
    monkeypatch,
):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        session_rows=[
            {
                "id": "bot-current",
                "title": "Bot Chat",
                "hidden": 0,
                "source": "desktop",
                "started_at": 3.0,
                "last_active": 9.0,
                "message_count": 12,
                "archived": 0,
            }
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_bot_chat_get())
    assert result["success"] is True
    assert result["registry_session_id"] == "bot-current"
    assert result["current_session_id"] == "bot-current"
    assert result["compression_continuation"] is False


def test_phase2_bot_chat_get_rejects_internal_source(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        session_rows=[
            {"id": "internal", "title": "Bot Chat", "source": "tool", "archived": 0}
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_bot_chat_get())
    assert result["success"] is False
    assert result["error"]["code"] == "BOT_CHAT_NOT_FOUND"


def test_phase2_session_read_filters_and_resolves_ids(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "timestamp": 1,
                "content": "hello",
            },
            {
                "id": 2,
                "session_id": "session-1",
                "role": "assistant",
                "timestamp": 2,
                "content": "world",
            },
            {
                "id": 3,
                "session_id": "session-1",
                "role": "tool",
                "timestamp": 3,
                "content": "hidden",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_session_read("session-1", limit=2, offset=0))
    assert result["success"] is True
    assert result["session_id"] == "session-1"
    assert {row["role"] for row in result["messages"]} == {"user", "assistant"}
    assert result["returned_count"] == 2
    assert result["next_offset"] == 2
    assert result["has_more"] is True
    assert result["truncated"] is False
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")

    prefix_result = json.loads(server.hermes_session_read("prefix", limit=1))
    assert prefix_result["success"] is True
    assert prefix_result["session_id"] == "session-1"

    monkeypatch.setenv(session_history.INTERNAL_CONTENT_ENV, "1")
    elevated = json.loads(
        server.hermes_session_read("session-1", include_tool_messages=True)
    )
    assert {row["role"] for row in elevated["messages"]} == {
        "user",
        "assistant",
        "tool",
    }


def test_phase2_session_read_denies_internal_roles_without_gate(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.delenv(session_history.INTERNAL_CONTENT_ENV, raising=False)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = json.loads(
        server.hermes_session_read("session-1", include_tool_messages=True)
    )
    assert result["success"] is False
    assert result["error"]["code"] == "SESSION_READ_FAILED"
    assert session_history.INTERNAL_CONTENT_ENV in result["error"]["message"]


def test_phase2_session_read_rejects_missing_and_ambiguous_ids(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(connection)
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    for value in ("missing", "ambiguous"):
        result = json.loads(server.hermes_session_read(value))
        assert result["success"] is False
        assert result["error"]["code"] == "SESSION_ID_NOT_FOUND_OR_AMBIGUOUS"


def test_phase2_response_size_is_enforced():
    huge = [
        {
            "id": 1,
            "session_id": "s",
            "role": "user",
            "content": "x" * (server.MAX_RESPONSE_BYTES + 1),
        }
    ]
    result = json.loads(
        server._session_page_response("messages", huge, offset=0, requested_limit=1)
    )
    assert (
        len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        <= server.MAX_RESPONSE_BYTES
    )
    assert result["success"] is False or result["truncated"] is True


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.hermes_session_list(limit=101),
        lambda: server.hermes_session_list(offset=session_history.MAX_OFFSET + 1),
        lambda: server.hermes_session_read("session-1", limit=101),
        lambda: server.hermes_session_read(
            "session-1", offset=session_history.MAX_OFFSET + 1
        ),
    ],
)
def test_phase2_tool_bounds_fail_closed(monkeypatch, call):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = json.loads(call())
    assert result["success"] is False


def test_phase3_session_export_json_and_markdown_without_files(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "timestamp": 1,
                "content": "hello",
            },
            {
                "id": 2,
                "session_id": "session-1",
                "role": "assistant",
                "timestamp": 2,
                "content": "world",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    before = list(tmp_path.iterdir())

    exported_json = json.loads(
        server.hermes_session_export("session-1", format="json", limit=2)
    )
    assert exported_json["success"] is True
    assert exported_json["format"] == "json"
    assert [row["content"] for row in exported_json["messages"]] == ["hello", "world"]

    exported_markdown = server.hermes_session_export(
        "session-1", format="markdown", limit=2
    )
    assert exported_markdown.startswith("# Hermes session export")
    assert "hello" in exported_markdown
    assert "world" in exported_markdown
    assert list(tmp_path.iterdir()) == before


def test_phase3_export_limits_truncation_and_lineage_fail_closed(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    too_many = json.loads(
        server.hermes_session_export(
            "session-1",
            limit=session_history.MAX_EXPORT_MESSAGES + 1,
        )
    )
    assert too_many["success"] is False

    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "content": "x" * server.MAX_RESPONSE_BYTES,
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    truncated = json.loads(
        server.hermes_session_export("session-1", format="json", limit=1)
    )
    assert (
        len(json.dumps(truncated, ensure_ascii=False).encode("utf-8"))
        <= server.MAX_RESPONSE_BYTES
    )
    assert truncated["success"] is False or truncated["truncated"] is True

    calls_before_lineage = list(fake_db.calls)
    lineage = json.loads(
        server.hermes_session_export("session-1", include_lineage=True)
    )
    assert lineage["success"] is False
    assert lineage["error"]["code"] == "SESSION_LINEAGE_EXPORT_UNAVAILABLE"
    assert fake_db.calls == calls_before_lineage


def test_phase3_export_elevated_content_denial_and_approval(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.delenv(session_history.INTERNAL_CONTENT_ENV, raising=False)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    denied = json.loads(
        server.hermes_session_export("session-1", include_tool_messages=True)
    )
    assert denied["success"] is False

    monkeypatch.setenv(session_history.INTERNAL_CONTENT_ENV, "1")
    connection = FakeSessionConnection()
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "tool",
                "content": "tool result",
            }
        ],
    )
    factory, _ = session_db_factory(fake_db)
    monkeypatch.setattr(server, "SessionDB", factory)
    approved = json.loads(
        server.hermes_session_export("session-1", include_tool_messages=True)
    )
    assert approved["success"] is True
    assert approved["messages"][0]["role"] == "tool"


def test_phase3_search_plain_text_compatibility_and_cleanup(monkeypatch):
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "snippet": "hello\nworld",
            }
        ],
        fts_enabled=True,
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = server.hermes_session_search("hello")
    assert result == "- session-1 [user] hello world"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase3_search_legacy_content_fallback(monkeypatch):
    connection = sqlite3.connect(":memory:")
    fake_db = FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "content": "legacy content",
            }
        ],
        fts_enabled=True,
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    assert server.hermes_session_search("legacy") == "- session-1 [user] legacy content"


def test_phase3_search_fts_unavailable_guidance(monkeypatch):
    class NoSearchDB:
        def __init__(self, connection):
            self._conn = connection

    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: NoSearchDB(connection))
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = server.hermes_session_search("hello")
    assert "unavailable" in result.lower()
    assert "fts" in result.lower()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase1_existing_tool_surface_remains_unchanged(monkeypatch):
    clear_gate_envs(monkeypatch)
    names = tool_names(server.build_server())
    assert "hermes_session_search" not in names
    assert "hermes_read_file" in names
    assert "hermes_search_files" in names


def test_profile_aware_session_tools_expose_profile_parameter():
    import inspect

    for func in (
        server.hermes_session_list,
        server.hermes_session_search,
        server.hermes_session_read,
        server.hermes_session_export,
        server.hermes_bot_chat_get,
    ):
        parameter = inspect.signature(func).parameters["profile"]
        assert parameter.default == "default"
