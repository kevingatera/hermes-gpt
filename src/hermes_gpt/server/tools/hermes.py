"""Adapters from Hermes GPT MCP tools to the installed Hermes Agent tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HermesToolContext:
    """Runtime dependencies read through callbacks so server state stays current."""

    require_imports: Callable[[], None]
    env_enabled: Callable[[str], bool]
    call_with_supported_kwargs: Callable[..., Any]
    expand_path: Callable[[str | None], str | None]
    clean_error: Callable[[str, Exception], RuntimeError]
    get_file_tools: Callable[[], Any]
    get_memory_tool: Callable[[], Any]
    get_terminal_tool: Callable[[], Any]
    get_vision_tool: Callable[[], Any]
    get_web_tool: Callable[[], Any]
    terminal_enabled_env: str
    memory_write_enabled_env: str
    vision_enabled_env: str
    web_enabled_env: str


class HermesTools:
    """Keep file, memory, terminal, vision, and web adapters out of server.py."""

    def __init__(self, context: HermesToolContext) -> None:
        self.context = context

    def register_read_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        for tool in (self.hermes_read_file, self.hermes_search_files, self.hermes_memory):
            server.add_tool(tool, meta=tool_meta())

    def register_local_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        write_enabled: bool,
        terminal_enabled: bool,
    ) -> None:
        if write_enabled:
            server.add_tool(self.hermes_write_file, meta=tool_meta())
            server.add_tool(self.hermes_patch, meta=tool_meta())
        if terminal_enabled:
            server.add_tool(self.hermes_run_command, meta=tool_meta())

    def register_online_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
        vision_enabled: bool,
        web_enabled: bool,
    ) -> None:
        if vision_enabled:
            server.add_tool(self.hermes_vision_analyze, meta=tool_meta())
        if web_enabled:
            server.add_tool(self.hermes_web_search, meta=tool_meta())
            server.add_tool(self.hermes_web_extract, meta=tool_meta())

    def hermes_read_file(self, path: str, offset: int = 1, limit: int = 500) -> str:
        try:
            self.context.require_imports()
            return self.context.get_file_tools().read_file_tool(
                path=self.context.expand_path(path), offset=offset, limit=limit
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_read_file", exc) from exc

    def hermes_write_file(self, path: str, content: str) -> str:
        try:
            self.context.require_imports()
            return self.context.get_file_tools().write_file_tool(
                path=self.context.expand_path(path), content=content
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_write_file", exc) from exc

    def hermes_patch(
        self,
        path: str,
        old_string: str,
        new_string: str,
        mode: str = "replace",
        replace_all: bool = False,
    ) -> str:
        try:
            self.context.require_imports()
            return self.context.call_with_supported_kwargs(
                self.context.get_file_tools().patch_tool,
                mode=mode,
                path=self.context.expand_path(path),
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_patch", exc) from exc

    def hermes_search_files(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
    ) -> str:
        try:
            self.context.require_imports()
            return self.context.call_with_supported_kwargs(
                self.context.get_file_tools().search_tool,
                pattern=pattern,
                target=target,
                path=self.context.expand_path(path),
                file_glob=file_glob,
                limit=limit,
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_search_files", exc) from exc

    def hermes_run_command(
        self, command: str, timeout: int = 30, workdir: str | None = None
    ) -> str:
        try:
            self.context.require_imports()
            if not self.context.env_enabled(self.context.terminal_enabled_env):
                raise RuntimeError(
                    "Terminal execution is disabled. Set "
                    f"{self.context.terminal_enabled_env}=1 to enable it."
                )
            capped_timeout = max(1, min(int(timeout), 120))
            return self.context.call_with_supported_kwargs(
                self.context.get_terminal_tool().terminal_tool,
                command=command,
                timeout=capped_timeout,
                workdir=self.context.expand_path(workdir),
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_run_command", exc) from exc

    def hermes_memory(
        self,
        action: str,
        target: str = "memory",
        content: str | None = None,
        old_text: str | None = None,
    ) -> str:
        try:
            self.context.require_imports()
            if action not in {"add", "replace", "remove", "search"}:
                raise RuntimeError(
                    "Unsupported memory action. Use add, replace, remove, or search."
                )
            if action in {"add", "replace", "remove"} and not self.context.env_enabled(
                self.context.memory_write_enabled_env
            ):
                raise RuntimeError(
                    "Memory write actions are disabled. Set "
                    f"{self.context.memory_write_enabled_env}=1 to enable them."
                )
            return self.context.get_memory_tool().memory_tool(
                action=action, target=target, content=content, old_text=old_text
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_memory", exc) from exc

    def hermes_vision_analyze(self, image_url: str, question: str = "") -> str:
        """Analyze an image through the optional Hermes Agent vision tool."""
        try:
            self.context.require_imports()
            if not self.context.env_enabled(self.context.vision_enabled_env):
                raise RuntimeError(
                    "Vision analysis is disabled. Set "
                    f"{self.context.vision_enabled_env}=1 to enable it."
                )
            vision_tool = self.context.get_vision_tool()
            if vision_tool is None:
                raise RuntimeError(
                    "Vision tool is not available (import failed at startup)."
                )
            user_prompt = question if question else "Describe this image in detail."
            return asyncio.run(
                vision_tool.vision_analyze_tool(
                    image_url=image_url, user_prompt=user_prompt
                )
            )
        except Exception as exc:
            raise self.context.clean_error("hermes_vision_analyze", exc) from exc

    def hermes_web_search(self, query: str, limit: int = 5) -> str:
        """Search the web through the optional Hermes Agent web tool."""
        try:
            self.context.require_imports()
            if not self.context.env_enabled(self.context.web_enabled_env):
                raise RuntimeError(
                    "Web search is disabled. Set "
                    f"{self.context.web_enabled_env}=1 to enable it."
                )
            web_tool = self.context.get_web_tool()
            if web_tool is None:
                raise RuntimeError(
                    "Web tool is not available (import failed at startup)."
                )
            return web_tool.web_search_tool(query=query, limit=limit)
        except Exception as exc:
            raise self.context.clean_error("hermes_web_search", exc) from exc

    def hermes_web_extract(self, urls: list[str], char_limit: int | None = None) -> str:
        """Extract web page content through the optional Hermes Agent web tool."""
        try:
            self.context.require_imports()
            if not self.context.env_enabled(self.context.web_enabled_env):
                raise RuntimeError(
                    "Web extract is disabled. Set "
                    f"{self.context.web_enabled_env}=1 to enable it."
                )
            web_tool = self.context.get_web_tool()
            if web_tool is None:
                raise RuntimeError(
                    "Web tool is not available (import failed at startup)."
                )
            kwargs = {"char_limit": char_limit} if char_limit is not None else {}
            return asyncio.run(web_tool.web_extract_tool(urls=urls, **kwargs))
        except Exception as exc:
            raise self.context.clean_error("hermes_web_extract", exc) from exc


__all__ = ["HermesToolContext", "HermesTools"]
