"""Choose safe read-only runtime paths for managed Hermes tasks."""

from __future__ import annotations

import os
from pathlib import Path

import operator_policy as policy
import operator_policy_paths as policy_paths

_MAX_PROFILE_RUNTIME_SCAN_ENTRIES = 100_000
_SOURCE_FILE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".mjs",
        ".php",
        ".py",
        ".pyc",
        ".rb",
        ".rs",
        ".sh",
        ".tcl",
        ".ts",
        ".tsx",
    }
)
_SAFE_RUNTIME_DATA_FILE_NAMES = frozenset({"secrets_introspect.xml"})


def _path_has_protected_component(path: Path) -> bool:
    path_parts = tuple(part.lower() for part in path.parts)
    return any(
        part in policy_paths.DEFAULT_DENIED_DIR_NAMES for part in path_parts
    ) or policy.is_denied_path(path)


def _runtime_entry_is_protected(entry: Path, runtime_path: Path) -> bool:
    """Check secrets without treating standard-library modules as credentials."""
    if entry.is_symlink():
        return _path_has_protected_component(entry.resolve(strict=False))

    relative_parts = entry.relative_to(runtime_path).parts
    if any(
        part.lower() in policy_paths.DEFAULT_DENIED_DIR_NAMES
        for part in relative_parts
    ):
        return True

    name = entry.name.lower()
    if name in policy_paths.DEFAULT_DENIED_BASENAMES or name.startswith(".env."):
        return True
    if name in _SAFE_RUNTIME_DATA_FILE_NAMES:
        return False

    # Runtime libraries use secret-related words in module names such as
    # token.py and secrets.py. Check data files by name, but allow source files.
    if entry.is_dir() or entry.suffix.lower() in _SOURCE_FILE_SUFFIXES:
        return False
    return policy.is_denied_path(entry)


def _reject_protected_runtime_descendants(runtime_path: Path) -> None:
    """Refuse a runtime directory that contains known secret stores or files."""
    if not runtime_path.is_dir():
        return

    scanned_entries = 0

    def fail_on_walk_error(error: OSError) -> None:
        raise PermissionError(
            "Selected Hermes profile runtime could not be checked safely"
        ) from error

    for current, directories, files in os.walk(
        runtime_path, followlinks=False, onerror=fail_on_walk_error
    ):
        for name in [*directories, *files]:
            scanned_entries += 1
            if scanned_entries > _MAX_PROFILE_RUNTIME_SCAN_ENTRIES:
                raise PermissionError(
                    "Selected Hermes profile runtime is too large to check safely"
                )
            entry = Path(current) / name
            if _runtime_entry_is_protected(entry, runtime_path):
                raise PermissionError(
                    "Selected Hermes profile references a protected runtime path"
                )


def _validated_profile_runtime_mount(
    candidate: Path, task_home: Path, hermes_data_root: Path
) -> Path | None:
    """Reject secrets and skip host/profile trees that are mounted separately."""
    resolved = Path(candidate).resolve(strict=True)
    if _path_has_protected_component(resolved):
        raise PermissionError(
            "Selected Hermes profile references a protected runtime path"
        )

    resolved_task_home = task_home.resolve()
    resolved_data_root = hermes_data_root.resolve()
    profiles_root = resolved_data_root / "profiles"
    home = Path.home().resolve()
    in_task_home = resolved == resolved_task_home or resolved_task_home in resolved.parents
    in_profile_tree = resolved == profiles_root or profiles_root in resolved.parents

    # Mounting an ancestor would expose HOME or other Hermes profiles.
    if (
        resolved in {Path("/"), home, resolved_data_root, profiles_root}
        or resolved in home.parents
        or resolved_data_root in resolved.parents
        or (in_profile_tree and not in_task_home)
    ):
        return None

    _reject_protected_runtime_descendants(resolved)
    return resolved


def readonly_profile_runtime_paths(
    candidates: tuple[Path, ...],
    workspace: Path,
    task_home: Path,
    hermes_data_root: Path,
) -> tuple[Path, ...]:
    """Validate profile runtimes and return disjoint read-only mount paths."""
    accepted: list[Path] = []
    for candidate in candidates:
        mount_path = _validated_profile_runtime_mount(
            candidate, task_home, hermes_data_root
        )
        if mount_path is not None:
            accepted.append(mount_path)

    protected = (workspace.resolve(), task_home.resolve())
    existing = {path.resolve(strict=True) for path in accepted if path.exists()}
    selected: list[Path] = []
    for candidate in sorted(existing, key=lambda path: len(path.parts)):
        if any(
            candidate == other
            or candidate in other.parents
            or other in candidate.parents
            for other in protected
        ):
            continue
        if any(
            candidate == other
            or candidate in other.parents
            or other in candidate.parents
            for other in selected
        ):
            continue
        selected.append(candidate)
    return tuple(selected)
