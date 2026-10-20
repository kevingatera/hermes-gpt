"""Keep the Agent session database separate from the UI fallback."""

import sys
from types import ModuleType

from hermes_gpt.server import app
from hermes_gpt.ui import chat


def test_server_uses_agent_session_database(monkeypatch, tmp_path):
    runtime = ModuleType("hermes_state")
    runtime.SessionDB = object()
    runtime.get_hermes_home = lambda: tmp_path
    tools = ModuleType("tools")
    for name in (
        "file_tools", "memory_tool", "terminal_tool", "vision_tools",
        "web_tools", "skill_manager_tool",
    ):
        setattr(tools, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "hermes_state", runtime)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setattr(app, "find_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(app, "add_hermes_to_syspath", lambda _root: None)
    # Restore startup globals after invoking the importer.
    for name in (
        "HERMES_ROOT", "IMPORT_ERROR", "file_tools", "memory_tool",
        "terminal_tool", "vision_tool", "web_tool", "skill_manager_tool",
        "SessionDB", "get_hermes_home",
    ):
        monkeypatch.setattr(app, name, getattr(app, name))
    app.import_hermes()
    assert app.SessionDB is runtime.SessionDB
    assert app.get_hermes_home is runtime.get_hermes_home


def test_ui_prefers_agent_session_database(monkeypatch, tmp_path):
    runtime = ModuleType("hermes_state")
    database = object()
    runtime.SessionDB = lambda **_kwargs: database
    monkeypatch.setitem(sys.modules, "hermes_state", runtime)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(chat, "_session_db_instance", None)
    assert chat._session_db() is database
