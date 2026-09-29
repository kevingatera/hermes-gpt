"""Cross-profile skill provenance derived from Hermes Agent loaders."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_policy as op
from operator_skill_loader import SkillEntry, _root, profile_skill_entries

SCOPE_GLOBAL = "global"
SCOPE_PROFILE_LOCAL = "profile_local"


@dataclass(frozen=True)
class SkillResolution:
    """Cross-profile provenance for one requested skill name."""

    name: str
    defined_in: tuple[str, ...]
    available_to: tuple[str, ...]
    scope: str
    entries: tuple[SkillEntry, ...]

    @property
    def exists(self) -> bool:
        return bool(self.defined_in)


@dataclass(frozen=True)
class SkillCatalog:
    entries: tuple[SkillEntry, ...]
    known_profiles: tuple[str, ...]


def build_catalog(hermes_root: Path | None = None) -> SkillCatalog:
    """Build provenance from the effective Agent loader, without persistence."""
    root = _root(hermes_root)
    profiles = tuple(op.list_existing_profiles(root))
    entries: list[SkillEntry] = []
    for profile in profiles:
        entries.extend(profile_skill_entries(profile, root))
    return SkillCatalog(entries=tuple(entries), known_profiles=profiles)


def resolve_name(name: str, catalog: SkillCatalog | None = None) -> SkillResolution:
    """Resolve one skill name across the effective profile loaders."""
    requested = str(name).strip()
    catalog = catalog or build_catalog()
    matches = tuple(entry for entry in catalog.entries if entry.name == requested)
    defined = tuple(sorted({entry.profile for entry in matches}))
    return SkillResolution(
        name=requested,
        defined_in=defined,
        available_to=defined,
        scope=SCOPE_GLOBAL if "default" in defined else SCOPE_PROFILE_LOCAL,
        entries=matches,
    )


def resolution_for_profile(
    resolution: SkillResolution,
    profile: str,
    known_profiles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded, actionable profile-specific resolution view."""
    known = list(known_profiles or ())
    if profile not in known:
        return {
            "profile": profile,
            "unknown_profile": True,
            "resolvable": False,
            "available_profiles": list(resolution.available_to),
            "known_profiles": known,
            "reason": f"profile '{profile}' is not a known Hermes profile",
        }
    resolvable = profile in resolution.available_to
    return {
        "profile": profile,
        "unknown_profile": False,
        "resolvable": resolvable,
        "available_profiles": list(resolution.available_to),
        "reason": (
            f"skill is defined in profile '{profile}' and can be loaded at execution"
            if resolvable
            else f"skill exists, but is not defined in profile '{profile}'"
        ),
    }
