"""Load Hermes's skill manager and scope mutations to the selected profile.

The Agent integration is optional. Discovery errors leave dry-run previews
available, while mutations fail when the requested profile cannot be scoped.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

_skill_manager_module: Any | None = None


def _add_sys_path_once(path: Path) -> None:
    resolved = str(path.resolve()).lower()
    existing = {str(Path(p).resolve()).lower() for p in sys.path if p}
    if resolved not in existing:
        sys.path.insert(0, str(path))


def _get_skill_manager(hermes_root: Path | None = None) -> Any | None:
    global _skill_manager_module
    if _skill_manager_module is not None:
        return _skill_manager_module

    if hermes_root is not None:
        try:
            _add_sys_path_once(Path(hermes_root))
        except Exception:  # noqa: BLE001, S110 - manager discovery also works from installed Hermes modules
            pass

    try:
        module = importlib.import_module("tools.skill_manager_tool")
    except Exception:  # noqa: BLE001 - an unavailable Agent loader disables direct mutations
        return None

    manage = getattr(module, "skill_manage", None)
    if not callable(manage):
        return None

    _skill_manager_module = module
    return module


def _call_skill_manager(
    action: str,
    name: str,
    hermes_root: Path | None = None,
    profile_home: Path | None = None,
    **payload: Any,
) -> dict[str, Any]:
    manager = _get_skill_manager(hermes_root)
    if manager is None:
        return {
            "success": False,
            "error": "Hermes skill manager is unavailable for direct mutation.",
        }
    kwargs = {k: v for k, v in payload.items() if v is not None}
    kwargs.update({"action": action, "name": name})

    token = None
    reset_home = None
    if profile_home is not None:
        try:
            from hermes_constants import (
                reset_hermes_home_override,
                set_hermes_home_override,
            )

            token = set_hermes_home_override(profile_home)
            reset_home = reset_hermes_home_override
        except Exception as exc:  # noqa: BLE001 - inability to scope a profile must fail closed
            # Optional-import degradation (CI and other hosts without the
            # Hermes Agent source tree on sys.path). Scoping is a no-op for
            # the default profile (default home == hermes_root), so we may
            # safely proceed without it there. For a non-default profile we
            # cannot guarantee the write targets the requested profile home,
            # so we fail closed instead of writing to the wrong profile.
            if hermes_root is not None and profile_home == Path(hermes_root).resolve():
                pass  # default profile: scoping unnecessary
            else:
                return {
                    "success": False,
                    "error": f"Could not scope skill mutation to {profile_home}: {exc}",
                }
    try:
        result = manager.skill_manage(**kwargs)  # type: ignore[union-attr]
    finally:
        if token is not None and reset_home is not None:
            reset_home(token)

    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return json.loads(result)
    try:
        return json.loads(str(result))
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": f"skill manager returned unsupported result: {result!r}",
        }
