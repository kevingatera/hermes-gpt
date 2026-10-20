"""Shared path, name, and content validation for Hermes skill tools."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_NAME_LENGTH = 64
_MAX_CONTENT_CHARS = 100_000
_MAX_FILE_BYTES = 1_048_576
_ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}

# Refuse secret-looking files before they are copied or written into a skill.
_SECRET_FILE_SUBSTRING_RE = re.compile(
    r"(?i)(token|secret|credential|oauth|cookie|private|password|passwd|"
    r"id_rsa|id_ed25519|authorized_keys)"
)


_SECRET_FILE_SUFFIX_RE = re.compile(r"(?i)(\.env|\.pem|\.key)$")


def _is_secret_filename(name: str) -> bool:
    return bool(
        _SECRET_FILE_SUBSTRING_RE.search(name)
        or _SECRET_FILE_SUFFIX_RE.search(name)
        or name.lower() == ".env"
    )


def _validate_skill_name(name: str) -> str:
    if not name:
        raise ValueError("Skill name is required.")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Skill name exceeds {_MAX_NAME_LENGTH} characters.")
    if not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid skill name {name!r}. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return name


def _skills_dir(profile_home: Path) -> Path:
    return profile_home / "skills"


def _skill_dir(profile_home: Path, name: str) -> Path:
    return _skills_dir(profile_home) / _validate_skill_name(name)


def _find_skill_dir(profile_home: Path, name: str) -> Path | None:
    """Find a skill by name under the profile's skills dir.

    Mirrors Hermes' walk: rglob SKILL.md, match parent dir name.
    """
    canon = _validate_skill_name(name)
    root = _skills_dir(profile_home)
    if not root.is_dir():
        return None
    try:
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == canon:
                return skill_md.parent
    except OSError:
        return None
    return None


def _resolve_supporting_file(skill_dir: Path, file_path: str) -> Path:
    """Resolve a supporting-file path inside ``skill_dir`` and refuse escapes.

    Accepts ``SKILL.md`` and ``<name>/SKILL.md`` as spellings for the main
    file. Anything else must be under references/ templates/ scripts/ assets/.
    Refuses path traversal and secret-looking filenames.
    """
    from_parts = Path(file_path).parts
    if not from_parts:
        raise ValueError("file_path is required.")

    # Path traversal check.
    if ".." in from_parts:
        raise ValueError("Path traversal ('..') is not allowed.")
    if Path(file_path).is_absolute():
        raise ValueError("Absolute paths are not allowed.")

    # SKILL.md main file.
    if from_parts[-1] == "SKILL.md" and (len(from_parts) == 1 or len(from_parts) == 2):
        target = skill_dir / "SKILL.md"
        # Containment check.
        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            raise ValueError("File path escapes the skill directory.")
        return target

    # Supporting file must be under an allowed subdir.
    if from_parts[0] not in _ALLOWED_SUBDIRS:
        raise ValueError(
            f"File must be under one of: {', '.join(sorted(_ALLOWED_SUBDIRS))} "
            f"(or be SKILL.md). Got: {file_path!r}"
        )
    if len(from_parts) < 2:
        raise ValueError(
            f"Provide a file path, not just a directory. "
            f"Example: '{from_parts[0]}/example.md'"
        )

    target = skill_dir / Path(*from_parts)
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        raise ValueError("File path escapes the skill directory.")

    # Secret-looking filename refusal.
    if _is_secret_filename(from_parts[-1]):
        raise ValueError(
            f"Refusing to write secret-looking filename {from_parts[-1]!r}."
        )

    return target


def _hash_content(content: str | None) -> tuple[int, str]:
    if not content:
        return (0, "")
    data = content.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def _validate_frontmatter(content: str) -> None:
    """Validate SKILL.md frontmatter. Raises ValueError on bad shape."""
    if not content.strip():
        raise ValueError("Content cannot be empty.")
    if not content.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter (---).")
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        raise ValueError(
            "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
        )
    yaml_text = content[3 : end.start() + 3]
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for SKILL.md frontmatter validation."
        ) from exc
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML frontmatter parse error: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(  # noqa: TRY004 - preserve the existing invalid-frontmatter error contract
            "Frontmatter must be a YAML mapping (key: value pairs)."
        )
    if "name" not in parsed:
        raise ValueError("Frontmatter must include 'name' field.")
    if "description" not in parsed:
        raise ValueError("Frontmatter must include 'description' field.")
