"""MCP adapters for workspace, git, gateway, export, and Owner tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from hermes_gpt.workspace import export as operator_export
from hermes_gpt.workspace import tools as operator_workspace


class WorkspaceTools:
    """Keep local workspace tool adapters and their MCP registration together."""

    def __init__(self, get_hermes_root: Callable[[], Path | None]):
        self.get_hermes_root = get_hermes_root

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        for tool in (
            self.hermes_gateway_status,
            self.hermes_gateway_restart,
            self.hermes_workspace_read,
        ):
            server.add_tool(tool, meta=tool_meta())
        server.add_tool(
            self.hermes_export_file,
            meta=tool_meta(),
            annotations=ToolAnnotations(
                title="Export an authorized local file as an MCP embedded resource",
                readOnlyHint=True,
            ),
        )
        for tool in (
            self.hermes_workspace_patch,
            self.hermes_workspace_write_file,
            self.hermes_workspace_run_test,
            self.hermes_git_status,
            self.hermes_git_diff,
            self.hermes_owner_run_command,
            self.hermes_owner_patch,
            self.hermes_owner_write_file,
        ):
            server.add_tool(tool, meta=tool_meta())

    def hermes_gateway_status(self, profile: str = "default") -> str:
        return operator_workspace.hermes_gateway_status(
            profile=profile,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_gateway_restart(
        self,
        profile: str = "default",
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_gateway_restart(
            profile=profile,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_workspace_read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 500,
    ) -> str:
        return operator_workspace.hermes_workspace_read(
            path=path,
            offset=offset,
            limit=limit,
        )

    def hermes_export_file(self, path: str) -> CallToolResult:
        return operator_export.hermes_export_file(path=path)

    def hermes_workspace_patch(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_workspace_patch(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            dry_run=dry_run,
        )

    def hermes_workspace_write_file(
        self,
        path: str,
        content: str,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_workspace_write_file(
            path=path,
            content=content,
            dry_run=dry_run,
        )

    def hermes_workspace_run_test(
        self,
        command: str,
        workdir: str | None = None,
        timeout: int = 120,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_workspace_run_test(
            command=command,
            workdir=workdir,
            timeout=timeout,
            dry_run=dry_run,
        )

    def hermes_git_status(self, workdir: str) -> str:
        return operator_workspace.hermes_git_status(workdir=workdir)

    def hermes_git_diff(
        self,
        workdir: str,
        pathspec: str | None = None,
        stat: bool = False,
    ) -> str:
        return operator_workspace.hermes_git_diff(
            workdir=workdir,
            pathspec=pathspec,
            stat=stat,
        )

    def hermes_owner_run_command(
        self,
        command: str,
        timeout: int = 120,
        workdir: str | None = None,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_owner_run_command(
            command=command,
            timeout=timeout,
            workdir=workdir,
            dry_run=dry_run,
        )

    def hermes_owner_patch(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_owner_patch(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            dry_run=dry_run,
        )

    def hermes_owner_write_file(
        self,
        path: str,
        content: str,
        dry_run: bool = True,
    ) -> str:
        return operator_workspace.hermes_owner_write_file(
            path=path,
            content=content,
            dry_run=dry_run,
        )
