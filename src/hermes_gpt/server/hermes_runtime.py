"""Find the installed Hermes Agent source tree and prepare its import path."""

from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path


def is_hermes_root(path: Path) -> bool:
    """Identify an agent source root, not a data directory with an unrelated ``tools/`` folder.

    Hermes checkouts have a regular ``tools`` package or a top-level
    ``hermes_state.py`` marker. A namespace ``tools/`` directory alone is not
    enough because adding a Hermes data root to ``sys.path`` can shadow imports.
    """
    if not path.exists():
        return False
    tools_dir = path / "tools"
    if tools_dir.is_dir() and (tools_dir / "__init__.py").is_file():
        return True
    return (path / "hermes_state.py").is_file()


def candidate_roots() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        env_path = Path(env_home).expanduser()
        candidates.extend([env_path, env_path / "hermes-agent"])

    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "hermes" / "hermes-agent",
            home / ".hermes" / "hermes-agent",
        ]
    )

    for package in ("hermes-agent", "hermes_agent"):
        try:
            dist = importlib.metadata.distribution(package)
        except importlib.metadata.PackageNotFoundError:
            # Package metadata is optional when Hermes is installed from a checkout.
            continue
        try:
            base = Path(dist.locate_file("")).resolve()
        except OSError:
            # Skip an installed package whose source location cannot be resolved.
            continue
        for parent in [base, *base.parents]:
            if parent.name == "hermes-agent":
                candidates.append(parent)
                break

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        key = str(resolved).lower()
        if key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def find_hermes_root() -> Path:
    for candidate in candidate_roots():
        if is_hermes_root(candidate):
            return candidate
    raise RuntimeError(
        "Could not find a Hermes Agent source root with a tools directory."
    )


def add_path_once(path: Path, *, prepend: bool = True) -> None:
    value = str(path)
    existing = {str(Path(item).resolve()).lower() for item in sys.path if item}
    if str(path.resolve()).lower() not in existing:
        if prepend:
            sys.path.insert(0, value)
        else:
            sys.path.append(value)


def add_hermes_to_syspath(root: Path) -> None:
    add_path_once(root)
    if os.name == "nt":
        site_packages = root / "venv" / "Lib" / "site-packages"
    else:
        lib_dir = root / "venv" / "lib"
        candidates = (
            sorted(lib_dir.glob("python*/site-packages")) if lib_dir.exists() else []
        )
        site_packages = candidates[0] if candidates else lib_dir / "site-packages"
    if site_packages.exists():
        # Keep Hermes dependencies available without shadowing this server's MCP SDK.
        add_path_once(site_packages, prepend=False)


__all__ = [
    "add_hermes_to_syspath",
    "add_path_once",
    "candidate_roots",
    "find_hermes_root",
    "is_hermes_root",
]
