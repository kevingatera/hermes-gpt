from __future__ import annotations

import operator_browser as browser
import operator_browser_tabs as browser_tabs


def test_tab_arguments_only_allow_listing_or_selecting_a_safe_reference():
    assert browser_tabs.validate_tab_command_args(["list"]) == ("list", None)
    assert browser_tabs.validate_tab_command_args(["select", "t2"]) == (
        "select",
        "t2",
    )
    assert browser_tabs.validate_tab_command_args(["select", "research_1"]) == (
        "select",
        "research_1",
    )
    for args in (["close"], ["new"], ["select", "--json"], ["t2;close"]):
        assert browser_tabs.validate_tab_command_args(args) == ("invalid", None)


def test_tab_list_is_bounded_redacted_and_drops_unrelated_fields():
    tabs = [
        {
            "tabId": f"t{index + 1}",
            "active": index == 0,
            "label": "work",
            "title": "A title",
            "url": (
                "https://alice:password@example.test/path?code=oauth-code"
                "&q=public-search#access_token=fragment-token"
            ),
            "type": "page",
            "pageText": "private page content must not be returned",
        }
        for index in range(22)
    ]

    result = browser_tabs.normalize_tab_list(
        {"success": True, "data": {"tabs": tabs, "other": "discarded"}}
    )

    assert result["success"] is True
    assert len(result["data"]["tabs"]) == 20
    assert result["data"]["truncated"] is True
    first = result["data"]["tabs"][0]
    assert first["tab_id"] == "t1"
    assert first["active"] is True
    assert first["title"] == "A title"
    assert first["type"] == "page"
    assert "alice:password" not in first["url"]
    assert "oauth-code" not in first["url"]
    assert "fragment-token" not in first["url"]
    assert "q=public-search" in first["url"]
    assert "pageText" not in result["data"]
    assert "private page content" not in str(result)


def test_tab_list_rejects_unexpected_cli_response_shape():
    result = browser_tabs.normalize_tab_list({"success": True, "data": {}})

    assert result == {
        "success": False,
        "code": "BROWSER_INVALID_TABS",
        "safe_message": "The browser returned an invalid tab list.",
    }


def test_browser_command_translates_and_normalizes_tab_selection(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        browser,
        "_read_task_state",
        lambda _home: {"status": "running", "headed": False},
    )

    def run(_state, command, args, **_kwargs):
        calls.append((command, args))
        return {
            "success": True,
            "data": {
                "tabId": "t2",
                "title": "Private title",
                "revived": True,
                "unexpected": "discarded",
            },
        }

    monkeypatch.setattr(browser, "_run", run)

    result = browser.browser_command(tmp_path, "tab", ["select", "t2"])

    assert calls == [("tab", ["t2"])]
    assert result == {
        "success": True,
        "data": {"selected_tab": "t2", "tab_id": "t2", "revived": True},
    }


def test_browser_command_rejects_tab_close_without_calling_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(
        browser,
        "_read_task_state",
        lambda _home: {"status": "running", "headed": False},
    )
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("CLI called")

    monkeypatch.setattr(browser, "_run", fail_if_called)

    result = browser.browser_command(tmp_path, "tab", ["close"])

    assert result["success"] is False
    assert result["code"] == "INVALID_BROWSER_TAB"


def test_active_title_refresh_preserves_other_tabs_and_bounds_output():
    result = browser_tabs.normalize_tab_list({"success": True, "data": {"tabs": [
        {"tabId": "t1", "active": True, "title": "example.net"},
        {"tabId": "t2", "active": False, "title": "Other page"},
    ]}})
    browser_tabs.refresh_active_title(result, {"success": True, "data": {"title": "Example Domain"}})
    assert result["data"]["tabs"][0]["title"] == "Example Domain"
    assert result["data"]["tabs"][1]["title"] == "Other page"
    browser_tabs.refresh_active_title(result, {"success": True, "data": {"title": "x" * 500}})
    assert len(result["data"]["tabs"][0]["title"]) == 240
    browser_tabs.refresh_active_title(result, {"success": False})
    assert "title" not in result["data"]["tabs"][0]
