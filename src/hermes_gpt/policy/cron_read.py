"""Profile authority for read-only scheduler inspection."""

import os
from pathlib import Path

from hermes_gpt.policy import authorization as op

CRON_READ_PROFILES_ENV = "HERMES_GPT_CRON_READ_ALLOWED_PROFILES"


def allowed_profiles() -> list[str]:
    if CRON_READ_PROFILES_ENV not in os.environ:
        return op.OperatorPolicy().allowed_profiles
    raw = os.environ[CRON_READ_PROFILES_ENV]
    profiles = []
    for item in raw.split(','):
        value = item.strip()
        if value == '*':
            return ['*']
        if value:
            try:
                profiles.append(op.validate_profile_name(value))
            except ValueError:
                continue
    # A malformed or explicitly empty read allowlist grants no authority.
    return profiles


def require_profile(profile: str, root: Path | None) -> None:
    policy = op.OperatorPolicy()
    policy.allowed_profiles = allowed_profiles()
    policy.require_profile(profile, root)
