"""Filesystem roots for a source checkout or an installed package.

Runtime modules live in ``src/hermes_gpt`` after the package migration, so code
that used to resolve release, UI, and diagnostics paths beside its own module
now needs the checkout root instead. An installed wheel has no checkout, so
those callers handle ``None`` or use the user state directory rather than
writing inside the installed package.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# A checkout keeps the package under ``<checkout>/src/hermes_gpt``, so the root
# is at most two directories above the package. Stopping there keeps an
# installed ``site-packages/hermes_gpt`` from matching an unrelated parent.
_CHECKOUT_DEPTH = 2

_PROJECT_NAME_RE = re.compile(r"^\s*name\s*=\s*\"hermes-gpt\"", re.MULTILINE)


def _declares_hermes_gpt(directory: Path) -> bool:
    try:
        text = (directory / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_PROJECT_NAME_RE.search(text))


@lru_cache(maxsize=1)
def source_root() -> Path | None:
    """Return the source checkout root, or None when running installed."""
    directory = PACKAGE_DIR
    for _ in range(_CHECKOUT_DEPTH):
        directory = directory.parent
        if _declares_hermes_gpt(directory):
            return directory
    return None


def import_root() -> Path:
    """Return the directory to put on sys.path so ``hermes_gpt`` imports.

    That is ``<checkout>/src`` for a source checkout and ``site-packages`` for
    an installed wheel. A confined child process needs this exact value rather
    than the package directory.
    """
    return PACKAGE_DIR.parent


def project_root() -> Path:
    """Return the checkout root, or the working directory when installed.

    Diagnostics and release checks describe a repository. An installed wheel
    has no checkout, so they report the current directory instead of the
    installed package.
    """
    return source_root() or Path.cwd()


def web_dist_dir() -> Path | None:
    """Return the built web bundle that lives beside a source checkout."""
    root = source_root()
    return root / "web" / "dist" if root is not None else None


def runtime_state_dir() -> Path:
    """Return a writable root for state that used to sit beside the modules.

    A source checkout keeps its own ``logs`` directory. An installed wheel must
    not write inside the package, so the fallback is a user-scoped state
    directory.
    """
    root = source_root()
    if root is not None:
        return root
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        home = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_STATE_HOME", "").strip()
        home = Path(base) if base else Path.home() / ".local" / "state"
    return home / "hermes-gpt"
