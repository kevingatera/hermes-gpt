"""MCP adapters for Codex-backed background jobs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.execution import codex as operator_codex


class CodexTools:
    """Keep Codex job argument mapping out of the server entry point."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]) -> None:
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        for tool in (
            self.hermes_codex_status,
            self.hermes_codex_plan,
            self.hermes_codex_start,
            self.hermes_codex_review_start,
            self.hermes_codex_jobs,
            self.hermes_codex_job_status,
            self.hermes_codex_job_result,
            self.hermes_codex_cancel,
        ):
            server.add_tool(tool, meta=tool_meta())

    def hermes_codex_status(self) -> dict[str, Any]:
        return operator_codex.hermes_codex_status(self.get_hermes_root())

    def hermes_codex_plan(
        self,
        prompt: str,
        workdir: str,
        sandbox: str = "read-only",
        model: str | None = None,
        ignore_user_config: bool = False,
        timeout: int = 900,
        execution_mode: str = "normal",
    ) -> dict[str, Any]:
        return operator_codex.hermes_codex_plan(
            prompt,
            workdir,
            sandbox,
            model,
            ignore_user_config,
            timeout,
            execution_mode=execution_mode,
        )

    def hermes_codex_start(
        self,
        prompt: str,
        workdir: str,
        sandbox: str = "read-only",
        model: str | None = None,
        ignore_user_config: bool = False,
        timeout: int = 900,
        confirm: bool = False,
        dry_run: bool = True,
        execution_mode: str = "normal",
    ) -> dict[str, Any]:
        return operator_codex.hermes_codex_start(
            prompt,
            workdir,
            sandbox,
            model,
            ignore_user_config,
            timeout,
            confirm,
            dry_run,
            self.get_hermes_root(),
            execution_mode=execution_mode,
        )

    def hermes_codex_review_start(
        self,
        workdir: str,
        target: str = "uncommitted",
        instructions: str = "",
        model: str | None = None,
        ignore_user_config: bool = False,
        timeout: int = 900,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return operator_codex.hermes_codex_review_start(
            workdir,
            target,
            instructions,
            model,
            ignore_user_config,
            timeout,
            confirm,
            dry_run,
            self.get_hermes_root(),
        )

    def hermes_codex_jobs(self, limit: int = 50) -> dict[str, Any]:
        return operator_codex.hermes_codex_jobs(limit, self.get_hermes_root())

    def hermes_codex_job_status(self, job_id: str) -> dict[str, Any]:
        return operator_codex.hermes_codex_job_status(job_id, self.get_hermes_root())

    def hermes_codex_job_result(
        self, job_id: str, max_chars: int = operator_codex.MAX_RESULT_CHARS
    ) -> dict[str, Any]:
        return operator_codex.hermes_codex_job_result(
            job_id, max_chars, self.get_hermes_root()
        )

    def hermes_codex_cancel(
        self, job_id: str, confirm: bool = False, dry_run: bool = True
    ) -> dict[str, Any]:
        return operator_codex.hermes_codex_cancel(
            job_id, confirm, dry_run, self.get_hermes_root()
        )


__all__ = ["CodexTools"]
