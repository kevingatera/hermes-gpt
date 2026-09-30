"""MCP handlers for the profile's canonical Hermes Bot Chat."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from hermes_gpt.sessions.history import redact_error as _redact_error
from hermes_gpt.sessions.history import redact_text as _redact_text
from hermes_gpt.sessions.history import redact_value as _redact_value
from hermes_gpt.sessions.history import safe_session_metadata as _safe_session_metadata
from hermes_gpt.server.tools.session_context import SessionToolContext


class BotChatTools:
    """Look up and send a turn to one profile's canonical Bot Chat."""

    def __init__(self, context: SessionToolContext):
        self.context = context

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        history_enabled: bool,
        send_enabled: bool,
    ) -> None:
        if history_enabled:
            server.add_tool(self.hermes_bot_chat_get, meta=tool_meta())
        if send_enabled:
            server.add_tool(self.hermes_bot_chat_send, meta=tool_meta())

    def _session_error(self, code: str, message: str) -> str:
        payload = {
            "success": False,
            "error": {"code": code, "message": _redact_text(message)},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def hermes_bot_chat_get(self, profile: str = "default") -> str:
        """Return the profile's canonical Bot Chat, including its current tip."""
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
        """Send one bounded turn to the current Bot Chat session."""
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
