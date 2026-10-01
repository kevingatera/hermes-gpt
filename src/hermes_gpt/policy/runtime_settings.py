"""Validated live settings for an explicitly administered private connection."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_gpt.policy.paths import validate_profile_name

ADMIN_ENV = "HERMES_GPT_CONNECTION_ADMIN"
PROFILE_CEILING_ENV = "HERMES_GPT_CONNECTION_ALLOWED_PROFILES"
FEATURES = {
    "history": ("HERMES_GPT_ENABLE_SESSION_SEARCH",),
    "delegation": ("HERMES_GPT_ENABLE_SESSION_CONTROL",),
    "managed_tasks": ("HERMES_GPT_ENABLE_SCOPED_TASKS",),
    "web": ("HERMES_GPT_ENABLE_WEB",),
    "vision": ("HERMES_GPT_ENABLE_VISION",),
    "diagnostics": ("HERMES_GPT_ENABLE_DIAGNOSTICS",),
    "scheduling": ("HERMES_GPT_ENABLE_CRON",),
    "schedule_changes": ("HERMES_GPT_ALLOW_CRON_WRITE",),
    "skill_changes": ("HERMES_GPT_ALLOW_SKILL_WRITE",),
    "changes": ("HERMES_GPT_ALLOW_WRITE",),
}
PROFILE_ENVS = ("HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES",
                "HERMES_GPT_OPERATOR_ALLOWED_PROFILES",
                "HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES")
_lock = threading.RLock()


@dataclass(frozen=True)
class Settings:
    revision: int
    features: dict[str, bool]
    profiles: tuple[str, ...] | None = None
    apply_mode: str | None = None

    def public(self) -> dict[str, Any]:
        return {"revision": self.revision, "features": dict(self.features),
                "profiles": list(self.profiles) if self.profiles is not None else None,
                "apply_mode": self.apply_mode}


def admin_enabled() -> bool:
    # Runtime settings cannot grant administration to themselves.
    return os.environ.get(ADMIN_ENV) == "1"


def profile_ceiling() -> list[str]:
    raw = os.environ.get(PROFILE_CEILING_ENV, "")
    return [validate_profile_name(item.strip()) for item in raw.split(',') if item.strip()]


def settings_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / '.hermes'))) / 'hermes-gpt-connection.json'


def _validate(data: dict[str, Any]) -> Settings:
    if set(data) - {'revision', 'features', 'profiles', 'apply_mode'}:
        raise ValueError('Unknown connection setting.')
    revision = data.get('revision', 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('Invalid connection revision.')
    features = data.get('features', {})
    if not isinstance(features, dict) or any(key not in FEATURES or not isinstance(value, bool) for key, value in features.items()):
        raise ValueError('Features must be known names with boolean values.')
    profiles = data.get('profiles')
    if profiles is not None:
        if not isinstance(profiles, list) or len(profiles) > 20:
            raise ValueError('Profiles must be a bounded list.')
        profiles = tuple(dict.fromkeys(validate_profile_name(value) for value in profiles))
        if not set(profiles) <= set(profile_ceiling()):
            raise ValueError('Requested profiles exceed connection authority.')
    mode = data.get('apply_mode')
    if mode not in (None, 'dry_run', 'direct'):
        raise ValueError('Apply mode must be dry_run or direct.')
    return Settings(revision, dict(features), profiles, mode)


def load() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings(0, {})
    with path.open('rb') as file:
        raw = file.read(65537)
    if len(raw) > 65536:
        raise ValueError('Connection settings exceed the size limit.')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError('Connection settings must be an object.')
    return _validate(data)


def effective_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if not admin_enabled():
        return environment
    try:
        settings = load()
    except (OSError, ValueError, TypeError):
        # Corrupt persisted policy must not silently restore write authority.
        for names in FEATURES.values():
            for name in names:
                environment[name] = '0'
        environment['HERMES_GPT_OPERATOR_APPLY_MODE'] = 'dry_run'
        for name in PROFILE_ENVS:
            environment[name] = ''
        return environment
    for feature, enabled in settings.features.items():
        for name in FEATURES[feature]:
            environment[name] = '1' if enabled else '0'
    if settings.profiles is not None:
        for name in PROFILE_ENVS:
            environment[name] = ','.join(settings.profiles)
    if settings.apply_mode is not None:
        environment['HERMES_GPT_OPERATOR_APPLY_MODE'] = settings.apply_mode
    return environment


def getenv(name: str, default: Any = None) -> Any:
    relevant = name in PROFILE_ENVS or name == 'HERMES_GPT_OPERATOR_APPLY_MODE' or any(name in names for names in FEATURES.values())
    if not relevant:
        return os.environ.get(name, default)
    return effective_environment().get(name, default)


def configure(*, features: dict[str, bool] | None = None,
              profiles: list[str] | None = None, apply_mode: str | None = None,
              expected_revision: int | None = None, confirm: bool = False,
              dry_run: bool = True) -> dict[str, Any]:
    if not admin_enabled():
        return {'success': False, 'code': 'CONNECTION_ADMIN_DISABLED'}
    if not isinstance(confirm, bool) or not isinstance(dry_run, bool):
        return {'success': False, 'code': 'INVALID_CONFIRMATION'}
    if not dry_run and not confirm:
        return {'success': False, 'code': 'CONFIRMATION_REQUIRED'}
    if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0):
        return {"success": False, "code": "INVALID_REVISION"}
    try:
        with _lock:
            before = load()
            if expected_revision is not None and expected_revision != before.revision:
                return {'success': False, 'code': 'SETTINGS_CONFLICT'}
            merged = before.public()
            if features is not None:
                if not isinstance(features, dict):
                    raise ValueError('Features must be a mapping.')
                merged['features'] = {**before.features, **features}
            if profiles is not None:
                merged['profiles'] = profiles
            if apply_mode is not None:
                merged['apply_mode'] = apply_mode
            merged['revision'] += 1
            after = _validate(merged)
            if not dry_run:
                path = settings_path()
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
                temp = Path(temporary)
                with os.fdopen(fd, 'w', encoding='utf-8') as file:
                    json.dump(after.public(), file, sort_keys=True)
                    file.flush()
                    os.fsync(file.fileno())
                temp.replace(path)
            return {'success': True, 'dry_run': dry_run, 'changed': not dry_run,
                    'before': before.public(), 'after': after.public(),
                    'refresh_required': False}
    except (OSError, ValueError, TypeError):
        return {'success': False, 'code': 'CONNECTION_SETTINGS_INVALID',
                'safe_message': 'Connection settings are invalid or could not be updated.'}
