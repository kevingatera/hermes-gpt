"""One conversational entry point over the existing session job lifecycle."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.types import ToolAnnotations


def register_ask_tool(server: Any, controls: Any, tool_meta: Any) -> None:
    async def hermes_ask(
        prompt: str,
        profile: str,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: int = 900,
        wait_seconds: int = 20,
    ) -> dict[str, Any]:
        """Ask Hermes to do work using an authorized profile's configured tools and accounts.

        Continue with session_id or omit it for a new conversation. Returns a
        bounded answer when ready, otherwise a job_id to poll. Missing account
        integrations are reported by Hermes, not inferred by this bridge.
        """
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 30:
            return {"success": False, "code": "INVALID_WAIT_SECONDS"}
        options = {"profile": profile, "model": model,
                   "reasoning_effort": reasoning_effort, "timeout": timeout}
        if session_id is None:
            started = controls.hermes_session_start(prompt=prompt, **options)
        else:
            started = controls.hermes_session_continue(session_id=session_id, prompt=prompt, **options)
        if not started.get("success"):
            return started
        job_id = started.get("job_id")
        if not job_id:
            return {"success": False, "code": "JOB_ID_MISSING"}
        deadline = time.monotonic() + wait_seconds
        while True:
            status = controls.hermes_session_job_status(job_id)
            if not status.get("success"):
                return {**status, "job_id": job_id}
            job = status.get("job", {})
            state = job.get("status", "unknown")
            if state in {"completed", "failed", "cancelled", "timed_out", "orphaned"}:
                result = controls.hermes_session_job_result(job_id)
                return {"success": state == "completed" and result.get("success", False),
                        "job_id": job_id, "session_id": job.get("session_id"),
                        "status": state, "result": result}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"success": True, "job_id": job_id,
                        "session_id": job.get("session_id"), "status": state,
                        "next_action": "Poll hermes_session_job_status, then retrieve hermes_session_job_result. Do not submit the prompt again."}
            # The job persists independently of this bounded wait. A client
            # disconnect must not cancel work or create another turn.
            await asyncio.sleep(min(1, remaining))

    server.add_tool(hermes_ask, meta=tool_meta(),
                    annotations=ToolAnnotations(title="Ask Hermes", readOnlyHint=False,
                                                destructiveHint=True, idempotentHint=False))
