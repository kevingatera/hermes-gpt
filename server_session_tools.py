from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hermes_session_history import (
    MAX_EXPORT_MESSAGES,
    MAX_LIST_LIMIT,
    MAX_PAGE_SIZE,
)
from hermes_session_history import (
    SessionSearchUnavailable as _SessionSearchUnavailable,
)
from hermes_session_history import (
    allowed_message_roles as _allowed_message_roles,
)
from hermes_session_history import (
    redact_error as _redact_error,
)
from hermes_session_history import (
    redact_text as _redact_text,
)
from hermes_session_history import (
    redact_value as _redact_value,
)
from hermes_session_history import (
    safe_session_metadata as _safe_session_metadata,
)
from hermes_session_history import (
    utf8_response_bytes as _utf8_response_bytes,
)
from hermes_session_history import (
    validate_bool as _validate_bool,
)
from hermes_session_history import (
    validate_limit as _validate_limit,
)
from hermes_session_history import (
    validate_offset as _validate_offset,
)
from hermes_session_history import (
    validate_session_id as _validate_session_id,
)

MAX_RESPONSE_BYTES = 262_144
DEFAULT_SESSION_OFFSET = 0


@dataclass(frozen=True)
class SessionToolContext:
    """Server callbacks and operator modules needed by the session tool layer."""

    make_adapter: Callable[..., Any]
    require_imports: Callable[[], None]
    session_history_enabled: Callable[[], bool]
    session_history_env_name: str
    session_control_enabled: Callable[[], bool]
    session_control_env_name: str
    validate_profile: Callable[[str], str]
    get_hermes_root: Callable[[], Any]
    get_agent_root: Callable[[], Any]
    session_db_available: Callable[[], bool]
    continue_session: Callable[..., Any]
    eprint: Callable[[str], None]
    policy: Any
    session_control: Any
    managed_tasks: Any


class SessionHistoryTools:
    """Implement read-only history and Bot Chat lookup tools."""

    def __init__(self, context: SessionToolContext):
        # Root paths, gates, and public handlers are callbacks so the active
        # server configuration is read when a tool is called.
        self.context = context

    def _session_error(self, code: str, message: str) -> str:
        payload = {
            "success": False,
            "error": {"code": code, "message": _redact_text(message)},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

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
        has_more = requested_limit > 0 and len(items) >= requested_limit
        next_offset = offset + len(items) if has_more else None
        if has_more_override is not None:
            has_more = has_more_override
        if next_offset_override is not None:
            next_offset = next_offset_override
        payload: dict[str, Any] = {
            "success": True,
            item_key: list(items),
            "returned_count": len(items),
            "offset": offset,
            "next_offset": next_offset,
            "has_more": has_more,
            "truncated": False,
        }
        if extra:
            payload.update(extra)

        removed_count = 0
        while (
            _utf8_response_bytes(
                json.dumps(
                    _redact_value(payload), ensure_ascii=False, separators=(",", ":")
                )
            )
            > MAX_RESPONSE_BYTES
            and payload[item_key]
        ):
            payload[item_key].pop()
            removed_count += 1
            payload["returned_count"] = len(payload[item_key])
            payload["truncated"] = True
            payload["has_more"] = True
            payload["next_offset"] = max(
                payload["next_offset"] or 0,
                offset + len(payload[item_key]) + removed_count,
            )

        serialized = json.dumps(
            _redact_value(payload), ensure_ascii=False, separators=(",", ":")
        )
        if _utf8_response_bytes(serialized) > MAX_RESPONSE_BYTES:
            return self._session_error(
                "SESSION_RESPONSE_TOO_LARGE",
                "The requested session response exceeds the configured response-size limit.",
            )
        return serialized

    def hermes_bot_chat_get(self, profile: str = "default") -> str:
        """Return a profile's canonical Bot Chat; it may be hidden from the general session list."""
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_history_enabled():
                return self._session_error(
                    "SESSION_HISTORY_DISABLED",
                    f"Session history is disabled. Set {self.context.session_history_env_name}=1 to enable it.",
                )
            adapter.open()
            resolved = adapter.get_canonical_bot_chat()
            if resolved is None:
                return self._session_error(
                    "BOT_CHAT_NOT_FOUND",
                    "No canonical Bot Chat was found for the requested profile.",
                )
            registry = _safe_session_metadata(resolved["registry"])
            current = _safe_session_metadata(resolved["current"])
            payload = {
                "success": True,
                "profile": safe_profile,
                "kind": "bot_chat",
                "canonical_title": resolved["canonical_title"],
                "registry_session_id": resolved["registry_session_id"],
                "current_session_id": resolved["current_session_id"],
                "compression_continuation": resolved["registry_session_id"]
                != resolved["current_session_id"],
                "session_list_visibility": "canonical_bot_chat_may_be_hidden",
                "preferred_send_tool": "hermes_bot_chat_send",
                "registry": registry,
                "current": current,
            }
            return json.dumps(
                _redact_value(payload), ensure_ascii=False, separators=(",", ":")
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self._session_error("BOT_CHAT_LOOKUP_FAILED", _redact_error(exc))
        finally:
            adapter.dispose_safely()

    def hermes_bot_chat_send(
        self,
        prompt: str,
        profile: str = "default",
        timeout: int = 900,
    ) -> dict[str, Any]:
        """Send one bounded turn directly to a profile's canonical Bot Chat."""
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_history_enabled():
                return self.context.policy.make_error_envelope(
                    layer="session_control",
                    code="SESSION_HISTORY_DISABLED",
                    safe_message=f"Session history is disabled. Set {self.context.session_history_env_name}=1 to enable Bot Chat targeting.",
                    suggested_action="Enable session history on the trusted local MCP server.",
                )
            adapter.open()
            resolved = adapter.get_canonical_bot_chat()
            if resolved is None:
                return self.context.policy.make_error_envelope(
                    layer="session_control",
                    code="BOT_CHAT_NOT_FOUND",
                    safe_message="No canonical Bot Chat was found for the requested profile.",
                    suggested_action="Open Bot Chat for that profile first, then retry.",
                )
            return self.context.continue_session(
                resolved["current_session_id"],
                prompt,
                timeout,
                safe_profile,
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self.context.policy.make_error_envelope(
                layer="session_control",
                code="BOT_CHAT_SEND_FAILED",
                safe_message=_redact_error(exc),
                suggested_action="Check the Bot Chat registry, profile authorization, and Hermes CLI installation.",
            )
        finally:
            adapter.dispose_safely()

    def hermes_session_list(
        self,
        limit: int = 20,
        offset: int = DEFAULT_SESSION_OFFSET,
        include_archived: bool = False,
        profile: str = "default",
    ) -> str:
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_history_enabled():
                return self._session_error(
                    "SESSION_HISTORY_DISABLED",
                    f"Session history is disabled. Set {self.context.session_history_env_name}=1 to enable it.",
                )
            safe_limit = _validate_limit(limit, "limit", MAX_LIST_LIMIT)
            safe_offset = _validate_offset(offset)
            safe_include_archived = _validate_bool(include_archived, "include_archived")
            adapter.open()
            rows = adapter.list_sessions(
                limit=safe_limit,
                offset=safe_offset,
                include_archived=safe_include_archived,
            )
            sessions = [
                _safe_session_metadata(row) for row in rows if isinstance(row, dict)
            ]
            return self._session_page_response(
                "sessions",
                sessions,
                offset=safe_offset,
                requested_limit=safe_limit,
                extra={"profile": safe_profile},
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self._session_error("SESSION_LIST_FAILED", _redact_error(exc))
        finally:
            adapter.dispose_safely()

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
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_history_enabled():
                return self._session_error(
                    "SESSION_HISTORY_DISABLED",
                    f"Session history is disabled. Set {self.context.session_history_env_name}=1 to enable it.",
                )
            safe_id = _validate_session_id(session_id)
            safe_limit = _validate_limit(limit, "limit", MAX_PAGE_SIZE)
            safe_offset = _validate_offset(offset)
            safe_include_inactive = _validate_bool(include_inactive, "include_inactive")
            safe_include_system = _validate_bool(
                include_system_messages, "include_system_messages"
            )
            safe_include_tool = _validate_bool(
                include_tool_messages, "include_tool_messages"
            )
            _allowed_message_roles(
                include_system_messages=safe_include_system,
                include_tool_messages=safe_include_tool,
            )
            adapter.open()
            resolved_id = adapter.resolve_session_id(safe_id)
            if not resolved_id:
                return self._session_error(
                    "SESSION_ID_NOT_FOUND_OR_AMBIGUOUS",
                    "The requested session ID was not found or is ambiguous.",
                )
            page = adapter.get_messages_page(
                resolved_id,
                limit=safe_limit,
                offset=safe_offset,
                include_inactive=safe_include_inactive,
                include_system_messages=safe_include_system,
                include_tool_messages=safe_include_tool,
            )
            return self._session_page_response(
                "messages",
                page["messages"],
                offset=safe_offset,
                requested_limit=safe_limit,
                extra={"session_id": resolved_id, "profile": safe_profile},
                has_more_override=page["has_more"],
                next_offset_override=page["next_offset"],
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self._session_error("SESSION_READ_FAILED", _redact_error(exc))
        finally:
            adapter.dispose_safely()

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
        current = list(messages)

        def render() -> str:
            lines = [
                "# Hermes session export",
                "",
                f"Session ID: `{_redact_text(session_id)}`",
                f"Returned count: {len(current)}",
                f"Offset: {offset}",
                f"Next offset: {next_offset}",
                f"Has more: {str(has_more).lower()}",
                f"Truncated: {str(truncated or len(current) < len(messages)).lower()}",
                "",
            ]
            for message in current:
                lines.extend(
                    [
                        f"## {message.get('role', '')} — {message.get('timestamp', '')}",
                        f"Message ID: `{message.get('id', '')}`",
                        "",
                        str(message.get("content", "")),
                        "",
                    ]
                )
            return "\n".join(lines)

        rendered = render()
        while _utf8_response_bytes(rendered) > MAX_RESPONSE_BYTES and current:
            current.pop()
            rendered = render()
        if _utf8_response_bytes(rendered) > MAX_RESPONSE_BYTES:
            return "# Hermes session export\n\nThe requested session export exceeds the configured response-size limit."
        return rendered

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
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_history_enabled():
                return self._session_error(
                    "SESSION_HISTORY_DISABLED",
                    f"Session history is disabled. Set {self.context.session_history_env_name}=1 to enable it.",
                )
            if not isinstance(format, str) or format.lower() not in {
                "json",
                "markdown",
            }:
                raise ValueError("format must be json or markdown.")
            safe_format = format.lower()
            safe_id = _validate_session_id(session_id)
            safe_limit = _validate_limit(limit, "limit", MAX_EXPORT_MESSAGES)
            safe_offset = _validate_offset(offset)
            safe_include_inactive = _validate_bool(include_inactive, "include_inactive")
            safe_include_system = _validate_bool(
                include_system_messages, "include_system_messages"
            )
            safe_include_tool = _validate_bool(
                include_tool_messages, "include_tool_messages"
            )
            safe_include_lineage = _validate_bool(include_lineage, "include_lineage")
            if safe_include_lineage:
                return self._session_error(
                    "SESSION_LINEAGE_EXPORT_UNAVAILABLE",
                    "Lineage export is disabled until a bounded safe lineage projection is proven.",
                )
            _allowed_message_roles(
                include_system_messages=safe_include_system,
                include_tool_messages=safe_include_tool,
            )
            adapter.open()
            resolved_id = adapter.resolve_session_id(safe_id)
            if not resolved_id:
                return self._session_error(
                    "SESSION_ID_NOT_FOUND_OR_AMBIGUOUS",
                    "The requested session ID was not found or is ambiguous.",
                )
            page = adapter.get_messages_page(
                resolved_id,
                limit=safe_limit,
                offset=safe_offset,
                include_inactive=safe_include_inactive,
                include_system_messages=safe_include_system,
                include_tool_messages=safe_include_tool,
            )
            if safe_format == "markdown":
                return self._session_markdown_response(
                    resolved_id,
                    page["messages"],
                    offset=safe_offset,
                    next_offset=page["next_offset"],
                    has_more=page["has_more"],
                    truncated=page["scan_limited"],
                )
            return self._session_page_response(
                "messages",
                page["messages"],
                offset=safe_offset,
                requested_limit=safe_limit,
                extra={
                    "session_id": resolved_id,
                    "format": "json",
                    "profile": safe_profile,
                },
                has_more_override=page["has_more"],
                next_offset_override=page["next_offset"],
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self._session_error("SESSION_EXPORT_FAILED", _redact_error(exc))
        finally:
            adapter.dispose_safely()

    def hermes_session_search(
        self,
        query: str,
        limit: int = 20,
        offset: int = DEFAULT_SESSION_OFFSET,
        profile: str = "default",
    ) -> str:
        safe_profile = self.context.validate_profile(profile)
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            if not self.context.session_db_available():
                return "Hermes session search is unavailable in this install: SessionDB import failed."
            rows = adapter.open().search_messages(
                query=query, limit=limit, offset=offset
            )
            if not rows:
                return "No matching Hermes session messages found."
            rendered = []
            for row in rows:
                session_id = row.get("session_id", "")
                role = row.get("role", "")
                content = (
                    (row.get("content") or "")
                    .replace(chr(13), " ")
                    .replace(chr(10), " ")
                )
                rendered.append(f"- {session_id} [{role}] {content[:500]}")
            return "\n".join(rendered)
        except _SessionSearchUnavailable as exc:
            message = f"Hermes session search is unavailable in this install: {exc}. Read-only FTS support is unavailable; no FTS activation or rebuild was attempted."
            self.context.eprint(f"hermes-gpt: {message}")
            return message
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            message = (
                f"Hermes session search is unavailable in this install: {exc}. "
                "Read-only FTS search was unavailable; no FTS activation or rebuild was attempted."
            )
            self.context.eprint(f"hermes-gpt: {message}")
            return message
        finally:
            adapter.dispose_safely()
