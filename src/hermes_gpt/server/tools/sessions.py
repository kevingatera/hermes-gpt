"""Compatibility facade for the profile-aware Hermes session tool handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hermes_gpt.sessions.history import MAX_EXPORT_MESSAGES
from hermes_gpt.server.tools.session_bot_chat import BotChatTools
from hermes_gpt.server.tools.session_context import SessionToolContext
from hermes_gpt.server.tools.session_history import DEFAULT_SESSION_OFFSET, MAX_RESPONSE_BYTES, HermesSessionHistoryTools


class SessionHistoryTools:
    """Keep the established server import surface while handlers stay focused."""

    def __init__(self, context: SessionToolContext):
        self.history = HermesSessionHistoryTools(context)
        self.bot_chat = BotChatTools(context)

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        history_enabled: bool,
        send_enabled: bool,
    ) -> None:
        self.history.register_mcp_tools(
            server,
            tool_meta=tool_meta,
            history_enabled=history_enabled,
        )
        self.bot_chat.register_mcp_tools(
            server,
            tool_meta=tool_meta,
            history_enabled=history_enabled,
            send_enabled=send_enabled,
        )

    def _session_error(self, code: str, message: str) -> str:
        return self.history._session_error(code, message)

    def _session_page_response(
        self,
        item_key: str,
        items: list[dict[str, Any]],
        *,
        offset: int,
        requested_limit: int,
        extra: dict[str, Any] | None = None,
        has_more_override: bool | None = None,
        next_offset_override: int | None = None,
    ) -> str:
        return self.history._session_page_response(
            item_key,
            items,
            offset=offset,
            requested_limit=requested_limit,
            extra=extra,
            has_more_override=has_more_override,
            next_offset_override=next_offset_override,
        )

    def _session_markdown_response(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        offset: int,
        next_offset: int,
        has_more: bool,
        truncated: bool,
    ) -> str:
        return self.history._session_markdown_response(
            session_id,
            messages,
            offset=offset,
            next_offset=next_offset,
            has_more=has_more,
            truncated=truncated,
        )

    def hermes_bot_chat_get(self, profile: str = "default") -> str:
        """Return the profile's canonical Bot Chat and current continuation."""
        return self.bot_chat.hermes_bot_chat_get(profile)

    def hermes_bot_chat_send(
        self, prompt: str, profile: str = "default", timeout: int = 900
    ) -> dict[str, Any]:
        """Send one bounded turn to the profile's canonical Bot Chat."""
        return self.bot_chat.hermes_bot_chat_send(
            prompt=prompt, profile=profile, timeout=timeout
        )

    def hermes_session_list(
        self,
        limit: int = 20,
        offset: int = DEFAULT_SESSION_OFFSET,
        include_archived: bool = False,
        profile: str = "default",
    ) -> str:
        """List bounded session metadata for one Hermes profile."""
        return self.history.hermes_session_list(
            limit=limit,
            offset=offset,
            include_archived=include_archived,
            profile=profile,
        )

    def hermes_session_read(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        include_inactive: bool = False,
        include_system_messages: bool = False,
        include_tool_messages: bool = False,
        profile: str = "default",
    ) -> str:
        """Read a bounded page of messages from a profile session."""
        return self.history.hermes_session_read(
            session_id=session_id,
            limit=limit,
            offset=offset,
            include_inactive=include_inactive,
            include_system_messages=include_system_messages,
            include_tool_messages=include_tool_messages,
            profile=profile,
        )

    def hermes_session_export(
        self,
        session_id: str,
        format: str = "json",
        limit: int = MAX_EXPORT_MESSAGES,
        offset: int = 0,
        include_inactive: bool = False,
        include_system_messages: bool = False,
        include_tool_messages: bool = False,
        include_lineage: bool = False,
        profile: str = "default",
    ) -> str:
        """Export a bounded page of session messages as JSON or Markdown."""
        return self.history.hermes_session_export(
            session_id=session_id,
            format=format,
            limit=limit,
            offset=offset,
            include_inactive=include_inactive,
            include_system_messages=include_system_messages,
            include_tool_messages=include_tool_messages,
            include_lineage=include_lineage,
            profile=profile,
        )

    def hermes_session_search(
        self,
        query: str,
        limit: int = 20,
        offset: int = DEFAULT_SESSION_OFFSET,
        profile: str = "default",
    ) -> str:
        """Search bounded session message snippets within one profile."""
        return self.history.hermes_session_search(
            query=query, limit=limit, offset=offset, profile=profile
        )


__all__ = [
    "DEFAULT_SESSION_OFFSET",
    "MAX_RESPONSE_BYTES",
    "SessionHistoryTools",
    "SessionToolContext",
]
