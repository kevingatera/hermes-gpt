"""Single source of truth for the Hermes GPT runtime version."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as distribution_version
from hermes_gpt.paths import source_root
import re

UNKNOWN_VERSION = "0+unknown"


def get_version() -> str:
    """Read checkout metadata, or the installed distribution outside a checkout."""
    root = source_root()
    if root is not None:
        pyproject = root / "pyproject.toml"
        try:
            match = re.search(
                r'^version\s*=\s*["\']([^"\']+)["\']',
                pyproject.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if match:
                return match.group(1)
        except OSError:
            pass
        # A project manifest identifies a checkout. Never substitute a stale
        # installed distribution when its source metadata is malformed.
        return UNKNOWN_VERSION
    try:
        return distribution_version("hermes-gpt")
    except (PackageNotFoundError, OSError, ValueError):
        return UNKNOWN_VERSION


VERSION = get_version()
