from __future__ import annotations

from typing import Any

import operator_session as op_session
import operator_session_tasks as op_session_tasks
from hermes_session_history import redact_error as _redact_error
from server_session_tools import SessionToolContext

DEFAULT_SESSION_TIMEOUT = 900


class SessionControlTools:
    """Implement profile continuation and scoped-task MCP tools."""

    def __init__(self, context: SessionToolContext):
        self.context = context

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

    def hermes_task_workspaces(
        self,
    ) -> dict[str, Any]:
        """List configured workspace aliases for scoped Hermes tasks."""
        return self.context.managed_tasks.hermes_task_workspaces(
            self.context.get_hermes_root()
        )

    def hermes_task_list(
        self, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """List managed Hermes sessions that can be resumed by task ID."""
        return self.context.managed_tasks.hermes_task_list(
            limit=limit,
            offset=offset,
            hermes_root=self.context.get_hermes_root(),
        )

    def hermes_task_start(
        self,
        prompt: str,
        workspace_id: str,
        credential_profile: str = "default",
        allow_workspace_write: bool = False,
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = DEFAULT_SESSION_TIMEOUT,
        model: str = op_session_tasks.MODEL_ID,
        reasoning_effort: str = "high",
        browser_enabled: bool = True,
        headed_browser: bool = False,
        browser_profile: str | None = None,
    ) -> dict[str, Any]:
        """Start a confined Hermes session with an isolated or configured Hermes browser.

        Set browser_profile to attach the session to that allowlisted profile's local
        browser.cdp_url. Omit it to start a task-owned isolated browser.
        """
        return self.context.managed_tasks.hermes_task_start(
            prompt=prompt,
            workspace_id=workspace_id,
            credential_profile=credential_profile,
            allow_workspace_write=allow_workspace_write,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            model=model,
            reasoning_effort=reasoning_effort,
            browser_enabled=browser_enabled,
            headed_browser=headed_browser,
            browser_profile=browser_profile,
            hermes_root=self.context.get_hermes_root(),
            agent_root=self.context.get_agent_root(),
        )

    def hermes_task_continue(
        self,
        task_id: str,
        prompt: str,
        confirm: bool = False,
        dry_run: bool = True,
        timeout: int = DEFAULT_SESSION_TIMEOUT,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Continue a managed Hermes session, optionally changing its model or effort."""
        return self.context.managed_tasks.hermes_task_continue(
            task_id=task_id,
            prompt=prompt,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            model=model,
            reasoning_effort=reasoning_effort,
            hermes_root=self.context.get_hermes_root(),
            agent_root=self.context.get_agent_root(),
        )

    def hermes_task_status(self, task_id: str) -> dict[str, Any]:
        """Read the durable state for a scoped Hermes task."""
        return self.context.managed_tasks.hermes_task_status(
            task_id, self.context.get_hermes_root()
        )

    def hermes_task_result(
        self, task_id: str, max_chars: int = op_session.MAX_RESULT_CHARS
    ) -> dict[str, Any]:
        """Read the latest bounded, redacted Hermes task answer."""
        return self.context.managed_tasks.hermes_task_result(
            task_id, max_chars, self.context.get_hermes_root()
        )

    def hermes_task_cancel(self, task_id: str, confirm: bool = False) -> dict[str, Any]:
        """Cancel the current turn after an explicit client confirmation."""
        if not confirm:
            return {
                "success": False,
                "code": "CONFIRMATION_REQUIRED",
                "safe_message": "Cancelling a Hermes task requires explicit confirmation.",
            }
        status = self.context.managed_tasks.hermes_task_status(
            task_id, self.context.get_hermes_root()
        )
        if not status.get("success"):
            return status
        job_id = str((status.get("task") or {}).get("latest_job_id") or "")
        if not job_id:
            return {
                "success": False,
                "code": "TASK_JOB_NOT_FOUND",
                "safe_message": "Hermes task has no active job.",
            }
        return self.context.session_control.hermes_session_job_cancel(
            job_id, self.context.get_hermes_root()
        )
