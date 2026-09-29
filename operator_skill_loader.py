"""Adapter to Hermes Agent skill discovery and its explicit-load path."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_policy as op

# Tests can inject an Agent-loader-shaped provider without an Agent checkout.
_skill_loader_override: Callable[[str, Path], Iterable[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class SkillEntry:
    """One skill exposed by the Agent loader for a logical profile."""

    name: str
    profile: str
    category: str | None = None
    description: str | None = None


class _LoaderUnavailable(RuntimeError):
    """The Agent loader could not be reached; fail closed, never not_found."""


def _default_root() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for candidate in (
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / ".hermes"


def _root(hermes_root: Path | None) -> Path:
    return Path(hermes_root) if hermes_root is not None else _default_root()


def _agent_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    for variable in ("HERMES_AGENT_ROOT", "HERMES_ROOT"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser())
    executable = shutil.which("hermes")
    if executable:
        bin_dir = Path(executable).resolve().parent
        candidates.extend((bin_dir.parent / "hermes-agent", bin_dir.parent))
    for package in ("hermes-agent", "hermes_agent"):
        try:
            base = Path(importlib.metadata.distribution(package).locate_file(""))
        except (importlib.metadata.PackageNotFoundError, OSError):
            continue
        candidates.extend((base, base / "hermes-agent"))
    candidates.extend(
        (
            Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent",
            Path.home() / ".hermes" / "hermes-agent",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _require_agent_modules() -> tuple[Any, Any]:
    """Return ``(skills_tool, hermes_constants)`` or raise _LoaderUnavailable."""
    last_error: Exception | None = None
    for candidate in [None, *_agent_root_candidates()]:
        if candidate is not None and candidate.is_dir():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            # The hermes-gpt checkout has a namespace ``tools/`` directory for
            # package-hygiene scripts. Remove that empty namespace only when
            # the real Agent package is about to be loaded; never replace a
            # concrete, already-loaded package.
            loaded_tools = sys.modules.get("tools")
            if (
                loaded_tools is not None
                and getattr(loaded_tools, "__file__", None) is None
                and (candidate / "tools" / "__init__.py").is_file()
            ):
                sys.modules.pop("tools", None)
        try:
            skills_tool = importlib.import_module("tools.skills_tool")
            constants = importlib.import_module("hermes_constants")
            if callable(getattr(skills_tool, "_find_all_skills", None)):
                return skills_tool, constants
        except Exception as exc:  # noqa: BLE001 - optional Agent runtime
            last_error = exc
            continue
    raise _LoaderUnavailable(
        "Hermes Agent loader is unavailable; refusing to validate skills"
        + (f": {last_error}" if last_error is not None else "")
    )


def _agent_modules() -> tuple[Any, Any] | None:
    """Return ``(skills_tool, hermes_constants)`` when Agent is available."""
    try:
        return _require_agent_modules()
    except _LoaderUnavailable:
        return None


@contextmanager
def _profile_scope(profile_home: Path, constants: Any):
    setter = getattr(constants, "set_hermes_home_override", None)
    resetter = getattr(constants, "reset_hermes_home_override", None)
    if not callable(setter) or not callable(resetter):
        yield
        return
    token = setter(profile_home)
    try:
        yield
    finally:
        resetter(token)


def _plugin_entries(skills_tool: Any) -> list[dict[str, Any]]:
    """Plugin-provided skill metadata, mirroring ``skills_list`` filtering.

    Best-effort provenance for the catalog: a plugin-registry failure here
    must not mask flat-tree skills, because the explicit-load probe resolves
    ``plugin:skill`` directly through ``skill_view``.
    """
    try:
        from hermes_cli.plugins import discover_plugins, get_plugin_manager

        discover_plugins()
        raw = get_plugin_manager().list_plugin_skill_metadata()
    except Exception:  # noqa: BLE001 - best-effort plugin provenance
        return []
    entries: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        meta = dict(item)
        frontmatter = meta.pop("frontmatter", {})
        if not isinstance(frontmatter, dict):
            frontmatter = {}
        name = str(meta.get("name") or "").strip()
        if not name:
            continue
        try:
            if not skills_tool.skill_matches_platform(frontmatter):
                continue
            if skills_tool._is_skill_disabled(name):
                continue
        except Exception:  # noqa: BLE001, S112 - per-skill best effort
            continue
        entries.append(
            {
                "name": name,
                "category": meta.get("category"),
                "description": meta.get("description"),
            }
        )
    return entries


def _discovery_entries(profile: str, hermes_root: Path) -> list[dict[str, Any]]:
    """Catalog entries for one profile; raises _LoaderUnavailable on failure."""
    skills_tool, constants = _require_agent_modules()
    profile_home = op.resolve_profile_home(profile, hermes_root)
    try:
        with _profile_scope(profile_home, constants):
            raw = skills_tool._find_all_skills()
            plugin_raw = _plugin_entries(skills_tool)
    except Exception as exc:
        raise _LoaderUnavailable(
            f"Hermes Agent loader failed for profile '{profile}': {exc}"
        ) from exc
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(
            {
                "name": name,
                "category": item.get("category"),
                "description": item.get("description"),
            }
        )
    for item in plugin_raw:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(item)
    return entries


def _agent_entries(profile: str, hermes_root: Path) -> list[dict[str, Any]]:
    """Backward-compatible discovery wrapper; loader failure yields []."""
    try:
        return _discovery_entries(profile, hermes_root)
    except _LoaderUnavailable:
        return []


def _explicit_load_ok(profile: str, name: str, hermes_root: Path) -> tuple[bool, str]:
    """Probe the explicit-load path (``skill_view``) for one skill.

    Returns ``(True, detail)`` when the requested profile can load the skill
    right now, ``(False, detail)`` when it cannot. Raises _LoaderUnavailable
    when the loader itself cannot be reached. No discovery cache is consulted,
    so a skill removed between planning and dispatch fails immediately.

    The probe calls ``skill_view(..., preprocess=False)``. Hermes preload
    also disables preprocessing at load time and renders later; the default
    ``preprocess=True`` would execute ``!`cmd``` snippets when
    ``skills.inline_shell`` is enabled.
    """
    requested = str(name).strip()
    if not requested:
        return False, "empty skill name"
    if _skill_loader_override is not None:
        try:
            entries = list(_skill_loader_override(profile, hermes_root))
        except Exception as exc:
            raise _LoaderUnavailable(
                f"skill loader override failed for profile '{profile}': {exc}"
            ) from exc
        for item in entries:
            if (
                isinstance(item, dict)
                and str(item.get("name") or "").strip() == requested
            ):
                return True, "explicit-loadable via test override"
        return False, "not present in test override entries"
    skills_tool, constants = _require_agent_modules()
    try:
        profile_home = op.resolve_profile_home(profile, hermes_root)
    except Exception as exc:  # noqa: BLE001 - invalid profile is a state
        return False, f"invalid profile: {exc}"
    try:
        with _profile_scope(profile_home, constants):
            raw = skills_tool.skill_view(requested, preprocess=False)
    except Exception as exc:
        raise _LoaderUnavailable(
            f"Hermes Agent loader failed for profile '{profile}': {exc}"
        ) from exc
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:  # noqa: BLE001 - malformed loader response is a state
        return False, "unparseable skill_view response"
    if isinstance(payload, dict) and payload.get("success"):
        return True, "explicit-loadable"
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("error") or payload.get("message") or "")[:200]
    return False, detail or "not loadable via skill_view"


def profile_skill_entries(
    profile: str, hermes_root: Path | None = None
) -> list[SkillEntry]:
    """Return the Agent loader's effective skills for one profile."""
    root = _root(hermes_root)
    canon = op.validate_profile_name(profile)
    if _skill_loader_override is not None:
        raw = list(_skill_loader_override(canon, root))
    else:
        raw = _discovery_entries(canon, root)
    return [
        SkillEntry(
            name=str(item["name"]),
            profile=canon,
            category=(str(item["category"]) if item.get("category") else None),
            description=(str(item["description"]) if item.get("description") else None),
        )
        for item in raw
        if isinstance(item, dict) and item.get("name")
    ]


def skill_names_for_home(profile_home: Path, profile: str | None = None) -> list[str]:
    """Return effective loader names for a manifest profile entity.

    Diagnostics only (Capability Manifest): best-effort, never raises, so a
    loader failure yields an empty skill list rather than a manifest outage.
    The dispatch hard gate uses ``validate_required_skills`` instead.
    """
    home = Path(profile_home)
    profile = profile or "default"
    root = home if profile == "default" else home.parent.parent
    try:
        if _skill_loader_override is not None:
            raw = list(_skill_loader_override(profile, root))
        else:
            raw = _discovery_entries(profile, root)
    except Exception:  # noqa: BLE001 - diagnostics never raise
        return []
    return sorted(
        {
            str(item["name"])
            for item in raw
            if isinstance(item, dict) and item.get("name")
        }
    )
