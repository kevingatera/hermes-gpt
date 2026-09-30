"""Compatibility facade for the read-only Mission Control tools.

Surface collection, local source reads, and overview composition live in
separate modules so each part can be reviewed and changed on its own. Existing
``operator_mission.hermes_mission_*`` imports remain the public entrypoints.
"""

from __future__ import annotations

from pathlib import Path

from hermes_gpt.missions import common as mission_common
from hermes_gpt.missions.common import _audited
from hermes_gpt.missions.operational import hermes_mission_audit, hermes_mission_cron, hermes_mission_fleet, hermes_mission_health, hermes_mission_profiles
from hermes_gpt.missions.overview import hermes_mission_overview
from hermes_gpt.missions.resources import hermes_mission_codex, hermes_mission_usage, hermes_mission_vault
from hermes_gpt.missions.work import hermes_mission_approvals, hermes_mission_delegations, hermes_mission_failures

# Preserve constants and compatibility helpers previously exposed here.
OVERVIEW_CAP_BYTES = mission_common.OVERVIEW_CAP_BYTES
SCHEMA_VERSION = mission_common.SCHEMA_VERSION
MISSION_ALLOWED_SURFACES_ENV = mission_common.MISSION_ALLOWED_SURFACES_ENV
MISSION_SURFACES = mission_common.MISSION_SURFACES
OVERVIEW_HARD_CAP_BYTES = mission_common.OVERVIEW_HARD_CAP_BYTES
SURFACE_CAP_BYTES = mission_common.SURFACE_CAP_BYTES
SURFACE_HARD_CAP_BYTES = mission_common.SURFACE_HARD_CAP_BYTES
SURFACE_TTL = mission_common.SURFACE_TTL


def hermes_mission_overview_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return hermes_mission_overview(hermes_root=hermes_root, force_refresh=force_refresh)


def hermes_mission_health_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_health",
        surface="health",
        fn=hermes_mission_health,
        hermes_root=hermes_root,
        summary="health",
        force_refresh=force_refresh,
    )


def hermes_mission_cron_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_cron",
        surface="cron",
        fn=hermes_mission_cron,
        hermes_root=hermes_root,
        summary="cron",
        force_refresh=force_refresh,
    )


def hermes_mission_fleet_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_fleet",
        surface="fleet",
        fn=hermes_mission_fleet,
        hermes_root=hermes_root,
        summary="fleet",
        force_refresh=force_refresh,
    )


def hermes_mission_audit_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_audit",
        surface="audit",
        fn=hermes_mission_audit,
        hermes_root=hermes_root,
        summary="audit",
        force_refresh=force_refresh,
    )


def hermes_mission_profiles_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_profiles",
        surface="profiles",
        fn=hermes_mission_profiles,
        hermes_root=hermes_root,
        summary="profiles",
        force_refresh=force_refresh,
    )


def hermes_mission_delegations_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_delegations",
        surface="delegations",
        fn=hermes_mission_delegations,
        hermes_root=hermes_root,
        summary="delegations",
        force_refresh=force_refresh,
    )


def hermes_mission_failures_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_failures",
        surface="failures",
        fn=hermes_mission_failures,
        hermes_root=hermes_root,
        summary="failures",
        force_refresh=force_refresh,
    )


def hermes_mission_approvals_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_approvals",
        surface="approvals",
        fn=hermes_mission_approvals,
        hermes_root=hermes_root,
        summary="approvals",
        force_refresh=force_refresh,
    )


def hermes_mission_codex_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_codex",
        surface="codex",
        fn=hermes_mission_codex,
        hermes_root=hermes_root,
        summary="codex",
        force_refresh=force_refresh,
    )


def hermes_mission_vault_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_vault",
        surface="vault",
        fn=hermes_mission_vault,
        hermes_root=hermes_root,
        summary="vault",
        force_refresh=force_refresh,
    )


def hermes_mission_usage_tool(
    hermes_root: Path | None = None, force_refresh: bool = False
) -> str:
    return _audited(
        tool="hermes_mission_usage",
        surface="usage",
        fn=hermes_mission_usage,
        hermes_root=hermes_root,
        summary="usage",
        force_refresh=force_refresh,
    )
