from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

import operator_session as op_session
import operator_session_metadata as session_metadata
import operator_session_profiles as session_profiles
from hermes_session_history import redact_error as _redact_error
from server_session_context import SessionToolContext

DEFAULT_SESSION_TIMEOUT = 900


class SessionControlTools:
    """Implement the profile-aware Hermes session-control MCP tools."""

    def __init__(self, context: SessionToolContext):
        self.context = context

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        session_control_enabled: bool,
    ) -> None:
        """Register profile-session controls only when session control is enabled."""
        if not session_control_enabled:
            return
        server.add_tool(
            self.hermes_session_profiles,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="List authorized Hermes session profiles",
                readOnlyHint=True,
            ),
        )
        for tool, title in (
            (self.hermes_session_job_status, "Read Hermes session job status"),
            (self.hermes_session_job_result, "Read Hermes session job result"),
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=title, readOnlyHint=True),
            )
        for tool, title in (
            (self.hermes_session_start, "Start a Hermes session"),
            (self.hermes_session_continue, "Continue a Hermes session"),
            (self.hermes_session_send, "Send a turn to a Hermes session"),
            (self.hermes_session_job_cancel, "Cancel a running Hermes session turn"),
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=title, destructiveHint=True),
            )
        for tool, title in (
            (self.hermes_session_rename, "Rename a Hermes session"),
            (self.hermes_session_pin, "Pin or unpin a Hermes session"),
        ):
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(
                    title=title,
                    destructiveHint=False,
                    idempotentHint=True,
                ),
            )

    def hermes_session_profiles(self) -> dict[str, Any]:
        """List authorized profiles and their non-secret model defaults."""
        return session_profiles.hermes_session_profiles(
            self.context.get_hermes_root()
        )

    def hermes_session_start(
        self,
        prompt: str,
        timeout: int = DEFAULT_SESSION_TIMEOUT,
        profile: str = "default",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Start a new Hermes profile session with optional model and effort overrides."""
        return self.context.session_control.hermes_session_start(
            prompt,
            timeout,
            hermes_root=self.context.get_hermes_root(),
            agent_root=self.context.get_agent_root(),
            profile=profile,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def hermes_session_continue(
        self,
        session_id: str,
        prompt: str,
        timeout: int = DEFAULT_SESSION_TIMEOUT,
        profile: str = "default",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Continue an existing Hermes session, optionally overriding model and effort for this turn."""
        hermes_root = self.context.get_hermes_root()
        if not self.context.session_control_enabled():
            return self.context.session_control.hermes_session_continue(
                session_id,
                prompt,
                timeout,
                hermes_root=hermes_root,
                agent_root=self.context.get_agent_root(),
                profile=profile,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        safe_profile = self.context.session_control.validate_session_profile(
            profile, hermes_root
        )
        if isinstance(safe_profile, dict):
            return safe_profile
        adapter = self.context.make_adapter(profile=safe_profile)
        try:
            self.context.require_imports()
            adapter.open()
            resolved_id = adapter.resolve_session_id(session_id)
            if not resolved_id:
                return self.context.policy.make_error_envelope(
                    layer="session_control",
                    code="SESSION_ID_NOT_FOUND_OR_AMBIGUOUS",
                    safe_message="The requested session ID was not found or is ambiguous in the requested profile.",
                    suggested_action="Use an exact or unique-prefix ID returned by hermes_session_list for that profile.",
                )
            return self.context.session_control.hermes_session_continue(
                resolved_id,
                prompt,
                timeout,
                hermes_root=hermes_root,
                agent_root=self.context.get_agent_root(),
                profile=safe_profile,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:  # noqa  # preserve the safe MCP error envelope
            return self.context.policy.make_error_envelope(
                layer="session_control",
                code="SESSION_CONTINUE_FAILED",
                safe_message=_redact_error(exc),
                suggested_action="Check the Hermes session database, profile, and local CLI installation.",
            )
        finally:
            adapter.dispose_safely()

    def hermes_session_send(
        self,
        session_id: str,
        prompt: str,
        timeout: int = DEFAULT_SESSION_TIMEOUT,
        profile: str = "default",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Alias for profile-aware hermes_session_continue for clients that use send terminology."""
        return self.context.continue_session(
            session_id, prompt, timeout, profile, model, reasoning_effort
        )

    def hermes_session_job_status(self, job_id: str) -> dict[str, Any]:
        """Return bounded metadata for a Hermes session-control job."""
        return self.context.session_control.hermes_session_job_status(
            job_id, self.context.get_hermes_root()
        )

    def hermes_session_job_cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a running Hermes session job owned by this server process."""
        return self.context.session_control.hermes_session_job_cancel(
            job_id, self.context.get_hermes_root()
        )

    def hermes_session_job_result(
        self, job_id: str, max_chars: int = op_session.MAX_RESULT_CHARS
    ) -> dict[str, Any]:
        """Return the bounded, redacted response from a Hermes session-control job."""
        return self.context.session_control.hermes_session_job_result(
            job_id, max_chars, self.context.get_hermes_root()
        )

    def hermes_session_rename(
        self,
        session_id: str,
        title: str,
        profile: str = "default",
    ) -> dict[str, Any]:
        """Rename an existing session through Hermes' supported session command."""
        return session_metadata.hermes_session_rename(
            session_id,
            title,
            profile=profile,
            hermes_root=self.context.get_hermes_root(),
            agent_root=self.context.get_agent_root(),
        )

    def hermes_session_pin(
        self,
        session_id: str,
        pinned: bool = True,
        profile: str = "default",
    ) -> dict[str, Any]:
        """Pin or unpin an existing session through Hermes' supported command."""
        return session_metadata.hermes_session_pin(
            session_id,
            pinned,
            profile=profile,
            hermes_root=self.context.get_hermes_root(),
            agent_root=self.context.get_agent_root(),
        )
