"""Register and dispatch scoped Hermes task controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

import operator_session as op_session
import operator_session_tasks as op_session_tasks
from server_session_tools import SessionToolContext

DEFAULT_SESSION_TIMEOUT = 900


class ManagedSessionTaskTools:
    """Implement the isolated workspace-backed Hermes task API."""

    def __init__(self, context: SessionToolContext):
        self.context = context

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        enabled: bool,
    ) -> None:
        """Register scoped tasks only when their separate feature gate is on."""
        if not enabled:
            return

        read_only_tools = (
            (self.hermes_task_list, "List managed Hermes sessions for resuming"),
            (
                self.hermes_task_workspaces,
                "List configured workspaces for scoped Hermes tasks",
            ),
            (self.hermes_task_status, "Read scoped Hermes session status"),
            (self.hermes_task_result, "Read the latest scoped Hermes session result"),
        )
        for tool, title in read_only_tools:
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=title, readOnlyHint=True),
            )

        mutating_tools = (
            (self.hermes_task_start, "Start a scoped Hermes session"),
            (self.hermes_task_continue, "Continue a scoped Hermes session"),
            (self.hermes_task_cancel, "Cancel a running Hermes session turn"),
        )
        for tool, title in mutating_tools:
            server.add_tool(
                tool,
                meta=tool_meta(),
                annotations=ToolAnnotations(title=title, destructiveHint=True),
            )

    def hermes_task_workspaces(self) -> dict[str, Any]:
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
        """Start a confined task with an isolated or configured Hermes browser."""
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
        """Resume a managed session with optional model or effort overrides."""
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


__all__ = ["DEFAULT_SESSION_TIMEOUT", "ManagedSessionTaskTools"]
