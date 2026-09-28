"""Discover and read Hermes skills for MCP clients."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def parse_skill_doc(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
            for line in parts[1].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip("'\"")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
    if not description:
        for line in body.splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean:
                description = clean[:180]
                break
    return {"name": name, "description": description, "path": str(path)}


class HermesSkillTools:
    """Keep skill discovery and file reads separate from server startup."""

    MAX_VIEW_BYTES = 80_000

    def register_mcp_tools(
        self,
        server: Any,
        *,
        tool_meta: Callable[[], dict[str, Any]],
    ) -> None:
        for tool in (self.hermes_skill_list, self.hermes_skill_view):
            server.add_tool(tool, meta=tool_meta())

    def __init__(
        self,
        *,
        require_imports: Callable[[], None],
        get_hermes_home: Callable[[], Any],
        get_source_root: Callable[[], Path | None],
        clean_error: Callable[[str, Exception], RuntimeError],
        eprint: Callable[[str], None],
    ) -> None:
        self.require_imports = require_imports
        self.get_hermes_home = get_hermes_home
        self.get_source_root = get_source_root
        self.clean_error = clean_error
        self.eprint = eprint

    def skill_roots(self) -> list[Path]:
        roots: list[Path] = []
        hermes_home = None
        if callable(self.get_hermes_home):
            try:
                hermes_home = Path(self.get_hermes_home())
            except (OSError, RuntimeError, TypeError, ValueError):
                hermes_home = None
        if hermes_home is None:
            env_home = os.environ.get("HERMES_HOME")
            hermes_home = (
                Path(env_home).expanduser() if env_home else Path.home() / ".hermes"
            )

        roots.append(hermes_home / "skills")
        profiles = hermes_home / "profiles"
        if profiles.exists():
            roots.extend(
                path / "skills" for path in profiles.iterdir() if path.is_dir()
            )

        source_root = self.get_source_root()
        if source_root:
            roots.append(source_root / "skills")

        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = root.expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            key = str(resolved).lower()
            if resolved.exists() and key not in seen:
                unique.append(resolved)
                seen.add(key)
        return unique

    def discover_skills(self) -> list[dict[str, str]]:
        skills: list[dict[str, str]] = []
        for root in self.skill_roots():
            for skill_md in root.rglob("SKILL.md"):
                try:
                    skills.append(parse_skill_doc(skill_md))
                except OSError as exc:
                    self.eprint(f"hermes-gpt: could not read skill {skill_md}: {exc}")
        return sorted(
            skills, key=lambda item: (item["name"].lower(), item["path"].lower())
        )

    def hermes_skill_list(self) -> str:
        try:
            self.require_imports()
            skills = self.discover_skills()
            if not skills:
                return "No Hermes skills found."
            # User-level skills take priority when names collide.
            seen_names: set[str] = set()
            unique_skills: list[dict[str, str]] = []
            for skill in skills:
                if skill["name"].lower() not in seen_names:
                    seen_names.add(skill["name"].lower())
                    unique_skills.append(skill)
            lines = []
            for skill in unique_skills:
                description = (
                    f" - {skill['description']}" if skill["description"] else ""
                )
                lines.append(f"- {skill['name']}{description}\n  {skill['path']}")
            return "\n".join(lines)
        except Exception as exc:
            raise self.clean_error("hermes_skill_list", exc) from exc

    def hermes_skill_view(self, name: str) -> str:
        try:
            self.require_imports()
            query = name.strip().lower()
            matches = [
                skill
                for skill in self.discover_skills()
                if skill["name"].lower() == query
                or Path(skill["path"]).parent.name.lower() == query
            ]
            if not matches:
                return f"No skill matched {name!r}."
            if len(matches) > 1:
                choices = "\n".join(
                    f"- {skill['name']}: {skill['path']}" for skill in matches
                )
                return f"Multiple skills matched:\n{choices}"

            skill_path = Path(matches[0]["path"])
            file_size = skill_path.stat().st_size
            if file_size > self.MAX_VIEW_BYTES:
                text = skill_path.read_text(encoding="utf-8", errors="replace")
                return text[: self.MAX_VIEW_BYTES] + (
                    f"\n\n--- TRUNCATED (showing {self.MAX_VIEW_BYTES} of {file_size} bytes). "
                    "Use hermes_read_file for specific sections. ---"
                )
            return skill_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise self.clean_error("hermes_skill_view", exc) from exc


__all__ = ["HermesSkillTools", "parse_skill_doc"]
