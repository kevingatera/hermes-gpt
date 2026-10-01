"""Stable, explicit management tools for private ChatGPT connections."""

import json
from typing import Any

from mcp.types import ToolAnnotations

from hermes_gpt.policy import authorization as op
from hermes_gpt.policy import runtime_settings as settings


def connection_settings() -> dict[str, Any]:
    environment = settings.effective_environment()
    try:
        persisted = settings.load().public() if settings.admin_enabled() else None
        ceiling = settings.profile_ceiling() if settings.admin_enabled() else []
    except (OSError, ValueError, TypeError):
        return {'success': False, 'code': 'CONNECTION_SETTINGS_INVALID'}
    return {'success': True, 'can_reconfigure': settings.admin_enabled(),
            'persisted': persisted, 'profile_ceiling': ceiling,
            'features': {feature: all(environment.get(name) == '1' for name in names)
                         for feature, names in settings.FEATURES.items()},
            'profiles': environment.get(settings.PROFILE_ENVS[0], '').split(',')
                        if environment.get(settings.PROFILE_ENVS[0]) else [],
            'apply_mode': environment.get('HERMES_GPT_OPERATOR_APPLY_MODE', 'dry_run'),
            'refresh_required': False}


def _decoded(result: Any) -> dict[str, Any]:
    return json.loads(result) if isinstance(result, str) else result


def register_management(server: Any, *, callbacks: dict[str, Any], controls: Any = None) -> None:
    if not settings.admin_enabled():
        return

    def hermes_connection_settings() -> dict[str, Any]:
        """Inspect live Hermes connection features, profile authority, and revision."""
        return connection_settings()

    def hermes_connection_configure(features: dict[str, bool] | None = None,
                                    profiles: list[str] | None = None,
                                    apply_mode: str | None = None,
                                    expected_revision: int | None = None,
                                    confirm: bool = False,
                                    dry_run: bool = True) -> dict[str, Any]:
        """Change live connection settings on explicit user request. Preview first; direct changes require confirm=true and dry_run=false. Settings take effect without refreshing ChatGPT."""
        result = settings.configure(features=features, profiles=profiles, apply_mode=apply_mode,
                                    expected_revision=expected_revision, confirm=confirm, dry_run=dry_run)
        # Audit decisions, not request bodies or configuration values.
        op.audit_record(tool='hermes_connection_configure', level='connection_admin',
                        apply_mode='direct' if not dry_run else 'dry_run', dry_run=dry_run,
                        success=bool(result.get('success')), changed=bool(result.get('changed')),
                        summary='connection settings decision',
                        extra={'revision': result.get('after', {}).get('revision'),
                               'code': result.get('code')})
        return result

    def hermes_profile_config_get(profile: str, key_path: str | None = None) -> dict[str, Any]:
        """Read redacted Hermes profile configuration, including provider and model defaults."""
        return _decoded(callbacks['hermes_operator_config_get'](profile=profile, key_path=key_path))

    def hermes_profile_config_set(profile: str, key_path: str, value: Any,
                                  confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Change a nonsecret profile setting on user request. Defaults apply to the next Hermes turn without refreshing ChatGPT. Direct writes require confirm=true and dry_run=false."""
        if not isinstance(confirm, bool) or not isinstance(dry_run, bool):
            return {"success": False, "code": "INVALID_CONFIRMATION"}
        if not dry_run and not confirm:
            return {'success': False, 'code': 'CONFIRMATION_REQUIRED'}
        if not dry_run and settings.getenv('HERMES_GPT_ALLOW_WRITE') != '1':
            return {'success': False, 'code': 'CHANGES_DISABLED',
                    'suggested_action': 'On user request, enable changes using hermes_connection_configure.'}
        return _decoded(callbacks['hermes_operator_config_set'](
            profile=profile, key_path=key_path, value=value, dry_run=dry_run))

    from hermes_gpt.clients.chatgpt import schedules

    def hermes_schedule_create(profile: str, schedule: str, prompt: str, name: str,
                                paused: bool = True, model: str | None = None,
                                reasoning_effort: str | None = None,
                                confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Create a local-delivery schedule, paused by default. Uses Hermes scheduler validation and locking. Direct changes require confirm=true and dry_run=false."""
        return schedules.create(profile, schedule, prompt, name, paused, model,
                                reasoning_effort, confirm, dry_run,
                                root=controls.context.get_hermes_root() if controls else None,
                                agent_root=controls.context.get_agent_root() if controls else None)

    def hermes_schedule_action(profile: str, job_id: str, action: str,
                                confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Pause, resume, run, or remove a schedule. Run queues the job for the next scheduler tick; it does not mean completed. Use cron_status to check the outcome. Direct changes require confirm=true and dry_run=false."""
        return schedules.action(profile, job_id, action, confirm=confirm, dry_run=dry_run,
                                root=controls.context.get_hermes_root() if controls else None,
                                agent_root=controls.context.get_agent_root() if controls else None)

    for tool, readonly in ((hermes_connection_settings, True), (hermes_connection_configure, False),
                           (hermes_profile_config_get, True), (hermes_profile_config_set, False),
                           (hermes_schedule_create, False), (hermes_schedule_action, False)):
        server.add_tool(tool, meta={'securitySchemes': [{'type': 'noauth'}]},
                        annotations=ToolAnnotations(title=tool.__name__.replace('_', ' '),
                                                    readOnlyHint=readonly, destructiveHint=not readonly))
