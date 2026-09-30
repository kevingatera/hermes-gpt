"""Fail-closed checks for required skills on Hermes profiles."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hermes_gpt.policy import authorization as op
from hermes_gpt.skills import catalog as skill_catalog
from hermes_gpt.skills import loader as skill_loader

ERROR_NOT_FOUND = "skill_not_found"
ERROR_NOT_RESOLVABLE = "skill_not_resolvable_for_profile"
ERROR_UNAVAILABLE = "skill_resolution_unavailable"


class SkillRequirementsError(ValueError):
    """A structured profile/required-skills preflight rejection."""

    def __init__(self, rejection: dict[str, Any]):
        encoded = op.redact_output(json.dumps(rejection, ensure_ascii=False))
        try:
            safe_rejection = json.loads(encoded)
        except json.JSONDecodeError:
            safe_rejection = {
                "error": ERROR_UNAVAILABLE,
                "message": "Skill requirements could not be verified safely.",
            }
        self.rejection = safe_rejection
        super().__init__(
            str(safe_rejection.get("message", "skill requirements are invalid"))
        )


def _unavailable(profile: str, detail: str) -> dict[str, Any]:
    return {
        "error": ERROR_UNAVAILABLE,
        "profile": profile,
        "message": (
            "The Hermes Agent skill loader is unavailable; skill requirements "
            "cannot be verified and dispatch is refused."
        ),
        "detail": detail[:300],
    }


def validate_required_skills(
    profile: str,
    skills: Iterable[str] | None,
    hermes_root: Path | None = None,
    *,
    catalog: skill_catalog.SkillCatalog | None = None,
) -> dict[str, Any] | None:
    """Validate required skills against the Agent's explicit-load path.

    The hard gate is the explicit load (``skill_view``): a skill the worker
    could load via ``--skills`` passes, including environment-filtered and
    ``plugin:skill`` names. The catalog only classifies failures and reports
    ``available_profiles``. Loader failures are fail-closed as
    ``skill_resolution_unavailable``.
    """
    requested: list[str] = []
    for skill in skills or ():
        value = str(skill).strip()
        if value and value not in requested:
            requested.append(value)
    if not requested:
        return None

    root = skill_loader._root(hermes_root)
    if skill_loader._skill_loader_override is None:
        try:
            skill_loader._require_agent_modules()
        except skill_loader._LoaderUnavailable as exc:
            return _unavailable(profile, str(exc))
    try:
        cat = catalog or skill_catalog.build_catalog(root)
    except skill_loader._LoaderUnavailable as exc:
        return _unavailable(profile, str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-closed catalog
        return _unavailable(profile, f"skill catalog failed: {exc}")

    not_found: list[str] = []
    not_resolvable: list[dict[str, Any]] = []
    for name in requested:
        try:
            ok, detail = skill_loader._explicit_load_ok(profile, name, root)
        except skill_loader._LoaderUnavailable as exc:
            return _unavailable(profile, str(exc))
        if ok:
            continue
        resolution = skill_catalog.resolve_name(name, cat)
        if not resolution.exists:
            not_found.append(name)
            continue
        view = skill_catalog.resolution_for_profile(
            resolution, profile, cat.known_profiles
        )
        if not view["resolvable"]:
            not_resolvable.append(
                {
                    "skill": name,
                    "profile": profile,
                    "available_profiles": view["available_profiles"],
                    "reason": view["reason"],
                    "detail": detail[:200],
                }
            )
        else:
            # Catalog claims this profile, but the live explicit load just
            # failed (removed/disabled/platform-gated after the catalog
            # snapshot, or stale discovery cache): fail closed.
            not_resolvable.append(
                {
                    "skill": name,
                    "profile": profile,
                    "available_profiles": view["available_profiles"],
                    "reason": (
                        f"skill is catalogued for profile '{profile}' but "
                        f"failed the live explicit-load check: {detail[:160]}"
                    ),
                }
            )

    if not_found and not_resolvable:
        return {
            "error": "skill_requirements_invalid",
            "profile": profile,
            "skills_not_found": sorted(not_found),
            "skills_not_resolvable": not_resolvable,
            "message": "One or more required skills are invalid for the requested profile.",
        }
    if not_found:
        return {
            "error": ERROR_NOT_FOUND,
            "profile": profile,
            "skills_not_found": sorted(not_found),
            "message": (
                "The following required skill names do not exist in the Hermes "
                f"skill catalog: {', '.join(sorted(not_found))}."
            ),
        }
    if not_resolvable:
        return {
            "error": ERROR_NOT_RESOLVABLE,
            "profile": profile,
            "skills": not_resolvable,
            "incompatible_skills": [item["skill"] for item in not_resolvable],
            "message": (
                "One or more required skills exist but are not resolvable by "
                f"profile '{profile}'."
            ),
        }
    return None


def require_required_skills(
    profile: str,
    skills: Iterable[str] | None,
    hermes_root: Path | None = None,
) -> None:
    rejection = validate_required_skills(profile, skills, hermes_root)
    if rejection is not None:
        raise SkillRequirementsError(rejection)
