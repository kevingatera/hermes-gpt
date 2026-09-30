"""MCP adapters for durable, runner-neutral background jobs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from hermes_gpt.execution import job_supervisor as op_jobs


class DurableJobTools:
    """Expose durable job status and bounded waiting to MCP clients."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]) -> None:
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        server.add_tool(
            self.hermes_job_status,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="Read durable background-job status and log cursor",
                readOnlyHint=True,
            ),
        )
        server.add_tool(
            self.hermes_job_wait,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="Wait up to 120 seconds for a background job to finish",
                readOnlyHint=True,
            ),
        )

    def hermes_job_status(
        self, job_id: str, cursor: int = 0, max_lines: int = 50
    ) -> str:
        """Read durable job state and the log lines after a cursor."""
        return op_jobs.hermes_job_status(
            job_id,
            cursor=cursor,
            max_lines=max_lines,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_job_wait(
        self,
        job_id: str,
        cursor: int = 0,
        wait_seconds: int = op_jobs.MAX_WAIT_SECONDS,
        max_lines: int = 50,
    ) -> str:
        """Wait for a terminal job state, bounded by the supervisor timeout."""
        return op_jobs.hermes_job_wait(
            job_id,
            cursor=cursor,
            wait_seconds=wait_seconds,
            max_lines=max_lines,
            hermes_root=self.get_hermes_root(),
        )


__all__ = ["DurableJobTools"]
