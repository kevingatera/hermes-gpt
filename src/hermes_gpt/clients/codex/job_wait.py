"""Bounded observation of durable jobs; observation never submits or cancels work."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.types import ToolAnnotations

TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out", "orphaned"}


def valid_wait(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 30


async def wait_for_job(controls: Any, job_id: str, wait_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        observed = controls.hermes_session_job_status(job_id)
        if not observed.get("success"):
            return {**observed, "job_id": job_id}
        job = observed.get("job", {})
        state = job.get("status", "unknown")
        if state in TERMINAL_STATES:
            result = controls.hermes_session_job_result(job_id)
            return {"success": state == "completed" and result.get("success", False),
                    "job_id": job_id, "session_id": job.get("session_id"),
                    "status": state, "result": result}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"success": True, "job_id": job_id,
                    "session_id": job.get("session_id"), "status": state,
                    "next_action": "Use hermes_session_job_status with this job_id for a bounded wait and the actual result (or hermes_wait if available). Do not submit again. Report a brief working update once, not every check. If the chat turn ends, the job continues; the user can follow it in the Hermes panel or ask for its result later."}
        # Ending a tool observation leaves the underlying job untouched.
        await asyncio.sleep(min(1, remaining))


def register_wait_tool(server: Any, controls: Any, tool_meta: Any) -> None:
    async def hermes_wait(job_id: str, wait_seconds: int = 25) -> dict[str, Any]:
        """Wait briefly for an existing Hermes job and return its answer when finished.

        Repeat with the same job_id while running. This does not launch work or
        cancel it if observation ends. Prefer this over rapid status polling.
        If the chat turn ends, follow the durable job in the Hermes panel or
        ask for its result later; do not promise an automatic later chat reply.
        """
        if not valid_wait(wait_seconds):
            return {"success": False, "code": "INVALID_WAIT_SECONDS"}
        return await wait_for_job(controls, job_id, wait_seconds)

    server.add_tool(hermes_wait, meta=tool_meta(), annotations=ToolAnnotations(
        title="Wait for Hermes result", readOnlyHint=True,
        destructiveHint=False, idempotentHint=True))


def register_status_wait_tool(server: Any, controls: Any, tool_meta: Any) -> None:
    async def hermes_session_job_status(job_id: str, wait_seconds: int = 25) -> dict[str, Any]:
        """Wait briefly for existing work and return status plus its answer when finished.

        Repeat with the same job_id while running; give one brief working update,
        not a narration of each check. Set wait_seconds=0 for an immediate check.
        A stopped observation never cancels or resubmits the underlying job.
        """
        if not valid_wait(wait_seconds):
            return {"success": False, "code": "INVALID_WAIT_SECONDS"}
        waited = await wait_for_job(controls, job_id, wait_seconds)
        observed = controls.hermes_session_job_status(job_id)
        if not observed.get("success"):
            return observed
        if "result" in waited:
            return {**observed, "result": waited["result"]}
        return {**observed, "next_action": "Repeat hermes_session_job_status with the same job_id for a bounded wait, or open hermes_console(job_id=...) to follow in the panel. Do not submit again or narrate every check."}

    server.add_tool(hermes_session_job_status, meta=tool_meta(), annotations=ToolAnnotations(
        title="Follow Hermes work", readOnlyHint=True,
        destructiveHint=False, idempotentHint=True))
