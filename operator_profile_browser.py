"""Authorize and control configured local Hermes browser profiles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_browser as browser
import operator_browser_profiles as browser_profiles
import operator_policy as op
import operator_session as sessions
import operator_session_job_store as session_store
import operator_session_tasks as tasks


@dataclass(frozen=True)
class ProfileBrowserTarget:
    """The private browser state bound to one authorized Hermes profile."""

    profile: str
    browser_id: str
    browser_home: Path
    hermes_root: Path
    cdp_port: int


def _profile_browser_target(profile: str) -> ProfileBrowserTarget | dict[str, Any]:
    if not op.env_truthy(tasks.ENABLE_SCOPED_TASKS_ENV):
        return {
            "success": False,
            "code": "TASKS_DISABLED",
            "safe_message": f"Scoped Hermes tasks are disabled. Set {tasks.ENABLE_SCOPED_TASKS_ENV}=1.",
        }

    checked = sessions.validate_session_profile(profile, None)
    if isinstance(checked, dict):
        return checked

    try:
        hermes_root = session_store._data_root(None).resolve(strict=True)
        cdp_port = browser_profiles.profile_cdp_port(checked, hermes_root)
        browser_id = hashlib.sha256(
            f"profile-browser\0{hermes_root}\0{checked}\0{cdp_port}".encode()
        ).hexdigest()[:32]
        browser_home = hermes_root / ".hermes-gpt" / "browser-profiles" / browser_id
        resolved_home = browser_home.resolve()
        resolved_home.relative_to(hermes_root)
        if browser_home.is_symlink():
            raise PermissionError("Browser profile state cannot be a symbolic link.")
    except PermissionError as exc:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_ACCESS_DENIED",
            "safe_message": str(exc),
        }
    except FileNotFoundError:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_CONFIG_UNAVAILABLE",
            "safe_message": "The selected Hermes profile has no readable local browser configuration.",
        }
    except (OSError, TypeError, ValueError):
        return {
            "success": False,
            "code": "BROWSER_PROFILE_UNAVAILABLE",
            "safe_message": "The selected Hermes profile needs an authorized local Chromium endpoint.",
        }

    return ProfileBrowserTarget(checked, browser_id, browser_home, hermes_root, cdp_port)


def _ensure_profile_browser_home(target: ProfileBrowserTarget) -> None:
    """Create private, root-contained state directories before attaching a browser."""
    root = target.hermes_root
    for path in (
        root / ".hermes-gpt",
        root / ".hermes-gpt" / "browser-profiles",
        target.browser_home,
    ):
        resolved_path = path.resolve()
        resolved_path.relative_to(root)
        browser._ensure_private_directory(path)


def hermes_browser_profile_list() -> dict[str, Any]:
    """List authorized local Hermes browser profiles without revealing endpoints."""
    if not op.env_truthy(tasks.ENABLE_SCOPED_TASKS_ENV):
        return {
            "success": False,
            "code": "TASKS_DISABLED",
            "safe_message": f"Scoped Hermes tasks are disabled. Set {tasks.ENABLE_SCOPED_TASKS_ENV}=1.",
        }
    try:
        op.OperatorPolicy().require_level("read_only")
        names = browser_profiles.allowed_profile_names()
    except (PermissionError, ValueError) as exc:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_ACCESS_DENIED",
            "safe_message": str(exc),
        }

    result: list[dict[str, Any]] = []
    for name in names:
        target = _profile_browser_target(name)
        if isinstance(target, dict):
            continue
        status = browser.browser_session_state(target.browser_home)
        attached = (
            status.get("success") is True
            and (status.get("browser") or {}).get("status") == "running"
        )
        result.append({"profile": target.profile, "attached": attached})
    return {"success": True, "profiles": result}


def hermes_browser_profile_attach(
    profile: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Attach ChatGPT controls to an authorized profile's live local browser."""
    target = _profile_browser_target(profile)
    if isinstance(target, dict):
        return target
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
    except PermissionError as exc:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_POLICY_DENIED",
            "safe_message": str(exc),
        }
    if policy.effective_dry_run(dry_run):
        return {
            "success": True,
            "dry_run": True,
            "changed": False,
            "profile": target.profile,
            "action": "attach_browser_profile",
        }
    if not isinstance(confirm, bool) or not confirm:
        return {
            "success": False,
            "code": "CONFIRMATION_REQUIRED",
            "safe_message": "Attaching browser controls requires confirm=true.",
        }

    try:
        _ensure_profile_browser_home(target)
    except (OSError, PermissionError, ValueError):
        return {
            "success": False,
            "code": "BROWSER_PROFILE_STATE_UNAVAILABLE",
            "safe_message": "Private browser profile state could not be prepared under the Hermes data root.",
        }

    current = browser.browser_session_state(target.browser_home)
    if current.get("success") is True and (current.get("browser") or {}).get("status") == "running":
        return {
            "success": True,
            "attached": True,
            "changed": False,
            "profile": target.profile,
            "browser": current["browser"],
        }

    attached = browser.create_profile_browser_session(
        target.browser_id,
        target.browser_home,
        target.hermes_root,
        target.cdp_port,
    )
    if not attached.get("success"):
        return attached
    browser_info = dict(attached.get("browser") or {})
    browser_info.pop("task_id", None)
    return {
        "success": True,
        "attached": True,
        "changed": True,
        "profile": target.profile,
        "browser": browser_info,
    }


def _profile_browser_read(
    profile: str, command: str, args: list[str] | None = None
) -> dict[str, Any]:
    target = _profile_browser_target(profile)
    if isinstance(target, dict):
        return target
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_POLICY_DENIED",
            "safe_message": str(exc),
        }

    current = browser.browser_session_state(target.browser_home)
    if not current.get("success"):
        if current.get("code") == "BROWSER_SESSION_NOT_FOUND" and command == "status":
            return {
                "success": True,
                "profile": target.profile,
                "browser": {"status": "not_attached", "source": "hermes_profile"},
            }
        return {
            "success": False,
            "code": "BROWSER_PROFILE_NOT_ATTACHED",
            "safe_message": "Attach this authorized browser profile before reading its page.",
        }
    if command == "status" or (current.get("browser") or {}).get("status") != "running":
        return {"success": True, "profile": target.profile, "browser": current["browser"]}

    result = browser.browser_command(target.browser_home, command, args)
    return {**result, "profile": target.profile}


def hermes_browser_profile_status(profile: str) -> dict[str, Any]:
    """Read whether an authorized browser profile is attached and its current URL."""
    return _profile_browser_read(profile, "status")


def hermes_browser_profile_snapshot(profile: str) -> dict[str, Any]:
    """Read the current page of an attached Hermes browser profile."""
    return _profile_browser_read(profile, "snapshot")


def hermes_browser_profile_tabs(profile: str) -> dict[str, Any]:
    """List a bounded summary of tabs in an attached Hermes browser profile."""
    return _profile_browser_read(profile, "tab", ["list"])


def _profile_browser_action(
    profile: str,
    command: str,
    args: list[str],
    confirm: bool,
    dry_run: bool,
) -> dict[str, Any]:
    target = _profile_browser_target(profile)
    if isinstance(target, dict):
        return target
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
    except PermissionError as exc:
        return {
            "success": False,
            "code": "BROWSER_PROFILE_POLICY_DENIED",
            "safe_message": str(exc),
        }
    current = browser.browser_session_state(target.browser_home)
    if not current.get("success") or (current.get("browser") or {}).get("status") != "running":
        return {
            "success": False,
            "code": "BROWSER_PROFILE_NOT_ATTACHED",
            "safe_message": "Attach this authorized browser profile before changing its page.",
        }
    if policy.effective_dry_run(dry_run):
        return {
            "success": True,
            "dry_run": True,
            "changed": False,
            "profile": target.profile,
            "action": command,
        }
    if not isinstance(confirm, bool) or not confirm:
        return {
            "success": False,
            "code": "CONFIRMATION_REQUIRED",
            "safe_message": "Browser actions require confirm=true.",
        }
    result = browser.browser_command(target.browser_home, command, args)
    return {**result, "profile": target.profile}


def hermes_browser_profile_navigate(
    profile: str, url: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Navigate the attached browser profile after its mutation gates pass."""
    return _profile_browser_action(profile, "navigate", [url], confirm, dry_run)


def hermes_browser_profile_click(
    profile: str, ref: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Click a reference from the latest browser profile snapshot."""
    return _profile_browser_action(profile, "click", [ref], confirm, dry_run)


def hermes_browser_profile_type(
    profile: str, ref: str, text: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Type text into a reference from the latest browser profile snapshot."""
    return _profile_browser_action(profile, "type", [ref, text], confirm, dry_run)


def hermes_browser_profile_scroll(
    profile: str,
    direction: str,
    pixels: int = 500,
    confirm: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Scroll the attached browser profile by a bounded number of pixels."""
    return _profile_browser_action(
        profile, "scroll", [direction, str(pixels)], confirm, dry_run
    )


def hermes_browser_profile_back(
    profile: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Go back one page in the attached browser profile."""
    return _profile_browser_action(profile, "back", [], confirm, dry_run)


def hermes_browser_profile_press(
    profile: str, key: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Press a key in the attached browser profile."""
    return _profile_browser_action(profile, "press", [key], confirm, dry_run)


def hermes_browser_profile_select_tab(
    profile: str, tab: str, confirm: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Select a tab by its stable id or label after mutation gates pass."""
    return _profile_browser_action(profile, "tab", ["select", tab], confirm, dry_run)


__all__ = [
    "hermes_browser_profile_attach",
    "hermes_browser_profile_back",
    "hermes_browser_profile_click",
    "hermes_browser_profile_list",
    "hermes_browser_profile_navigate",
    "hermes_browser_profile_press",
    "hermes_browser_profile_scroll",
    "hermes_browser_profile_select_tab",
    "hermes_browser_profile_snapshot",
    "hermes_browser_profile_status",
    "hermes_browser_profile_tabs",
    "hermes_browser_profile_type",
]
