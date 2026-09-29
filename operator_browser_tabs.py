"""Validate and redact tab summaries returned by agent-browser."""

from __future__ import annotations

import re
from typing import Any

import operator_policy as op

_MAX_TABS = 20
_MAX_TAB_TITLE_CHARS = 240
_MAX_TAB_URL_CHARS = 2_048
_TAB_ID_RE = re.compile(r"^t[1-9][0-9]{0,5}$")
_TAB_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_tab_reference(value: Any) -> str | None:
    """Return a stable tab id or label, or None when the reference is unsafe."""
    if not isinstance(value, str):
        return None
    reference = value.strip()
    if _TAB_ID_RE.fullmatch(reference) or _TAB_LABEL_RE.fullmatch(reference):
        return reference
    return None


def validate_tab_command_args(args: list[str]) -> tuple[str, str | None]:
    """Accept list or ``select, ref``; reserve list as an unambiguous MCP operation."""
    if args == ["list"]:
        return "list", None
    if len(args) == 2 and args[0] == "select":
        reference = validate_tab_reference(args[1])
        if reference:
            return "select", reference
    return "invalid", None


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return op.redact_output(value)[:limit]


def normalize_tab_list(result: dict[str, Any]) -> dict[str, Any]:
    """Keep tab-list output bounded and discard fields unrelated to tab choice."""
    if result.get("success") is not True:
        return result
    data = result.get("data")
    tabs = data.get("tabs") if isinstance(data, dict) else None
    if not isinstance(tabs, list):
        return {
            "success": False,
            "code": "BROWSER_INVALID_TABS",
            "safe_message": "The browser returned an invalid tab list.",
        }

    safe_tabs: list[dict[str, Any]] = []
    for item in tabs[:_MAX_TABS]:
        if not isinstance(item, dict):
            continue
        tab_id = item.get("tabId")
        if not isinstance(tab_id, str) or not _TAB_ID_RE.fullmatch(tab_id):
            continue
        tab: dict[str, Any] = {"tab_id": tab_id, "active": item.get("active") is True}
        for source, target, limit in (
            ("label", "label", 64),
            ("title", "title", _MAX_TAB_TITLE_CHARS),
            ("url", "url", _MAX_TAB_URL_CHARS),
            ("type", "type", 40),
        ):
            value = _safe_text(item.get(source), limit)
            if value is not None:
                tab[target] = value
        safe_tabs.append(tab)

    return {
        "success": True,
        "data": {
            "tabs": safe_tabs,
            "truncated": len(tabs) > _MAX_TABS,
        },
    }


def refresh_active_title(result: dict[str, Any], observation: dict[str, Any]) -> None:
    """Replace cached active-tab titles with a bounded current observation."""
    data = observation.get("data")
    title = None
    if observation.get("success") is True and isinstance(data, dict):
        title = _safe_text(data.get("title"), _MAX_TAB_TITLE_CHARS)
    for tab in result["data"]["tabs"]:
        if tab["active"]:
            # Do not present stale metadata as current if the page read failed.
            tab.pop("title", None)
            if title is not None:
                tab["title"] = title


def normalize_tab_selection(reference: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return a small confirmation without forwarding browser-controlled data."""
    if result.get("success") is not True:
        return result
    data = result.get("data")
    safe_data: dict[str, Any] = {"selected_tab": reference}
    if isinstance(data, dict):
        actual_id = data.get("tabId")
        if isinstance(actual_id, str) and _TAB_ID_RE.fullmatch(actual_id):
            safe_data["tab_id"] = actual_id
        for source, target in (("revived", "revived"), ("dialogBlocked", "dialog_blocked")):
            if data.get(source) is True:
                safe_data[target] = True
    return {"success": True, "data": safe_data}


__all__ = [
    "normalize_tab_list",
    "normalize_tab_selection",
    "refresh_active_title",
    "validate_tab_command_args",
    "validate_tab_reference",
]
