"""One conversational entry point over the existing session job lifecycle."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from hermes_gpt.clients.codex.job_wait import (
    register_wait_tool,
    valid_wait,
    wait_for_job,
)


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
        if not valid_wait(wait_seconds):
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
        return await wait_for_job(controls, job_id, wait_seconds)

    server.add_tool(hermes_ask, meta=tool_meta(),
                    annotations=ToolAnnotations(title="Ask Hermes", readOnlyHint=False,
                                                destructiveHint=True, idempotentHint=False))

    register_wait_tool(server, controls, tool_meta)
