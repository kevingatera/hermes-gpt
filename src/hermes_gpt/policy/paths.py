"""Resolve allowed Hermes profiles and enforce path safety rules."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# Default denied path fragments. These are matched as path segments / suffixes
# conservatively. The check is intentionally broad: false positives (refusing
# to write a benign file that happens to look secret-like) are acceptable;
# false negatives (writing to a real secret store) are not.
DEFAULT_DENIED_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".env.staging",
        ".envrc",
        "auth.json",
        "auth.lock",
        ".anthropic_oauth.json",
        "google_oauth.json",
        "webhook_subscriptions.json",
        "bws_cache.json",
        "mcp-tokens",
        "credentials",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".git-credentials",
    }
)

DEFAULT_DENIED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".azure",
        "vault",
        "secrets",
        "mcp-tokens",
        "pairing",
        ".config",
    }
)

# Substrings that, when present in a path, mark it as secret-like.
SECRET_PATH_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "credential",
    "oauth",
    "cookie",
    "private",
    "password",
    "passwd",
    ".key",
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
)


def _normalize_path(path: str | os.PathLike[str]) -> Path:
    """Expand ~ and resolve. Never raises; falls back to expanded path."""
    try:
        return Path(os.path.expanduser(str(path))).resolve()
    except Exception:  # noqa: BLE001 - Keep path safety checks available on resolution errors.
        try:
            return Path(os.path.expanduser(str(path)))
        except Exception:  # noqa: BLE001 - Last resort lets callers inspect the supplied path.
            return Path(str(path))


def normalize_hermes_data_root(path: str | os.PathLike[str] | None) -> Path | None:
    """Normalize a Hermes install path to the data root.

    ``.../profiles/<profile>`` -> ``...``
    ``.../hermes-agent`` -> ``...``
    Already-normalized data roots remain unchanged.
    """
    if path is None:
        return None
    raw = Path(os.path.expanduser(str(path)))
    parts = [part.lower() for part in raw.parts]
    if not parts:
        return raw
    if parts[-1] == "hermes-agent":
        return raw.parent
    if len(parts) >= 2 and parts[-2] == "profiles":
        return raw.parent.parent
    return raw


def is_denied_path(path: str | os.PathLike[str]) -> bool:
    """Return True if ``path`` is a secret / credential / vault / token path.

    Conservative: returns True for any path whose basename matches a known
    secret file, whose parent directory is a known secret directory, whose
    name contains a secret-like substring, or that resolves into a known
    Hermes internal credential area (mcp-tokens, pairing, auth.json under a
    Hermes home).

    Defense-in-depth, not a security boundary (the terminal tool can still
    bypass). But operator tools rely on this as a hard refusal gate.
    """
    if path is None:
        return True

    resolved = _normalize_path(path)
    name = resolved.name.lower()

    # Exact-basename deny.
    if name in DEFAULT_DENIED_BASENAMES:
        return True

    # .env.* glob-style match.
    if name.startswith(".env."):
        return True

    # Any parent directory in the denied dir set.
    for parent in resolved.parents:
        if parent.name.lower() in DEFAULT_DENIED_DIR_NAMES:
            return True

    # Secret-like substring in the final path component.
    lower_name = name
    for needle in SECRET_PATH_SUBSTRINGS:
        if needle in lower_name:
            return True

    # Hermes-internal credential stores: detect by path shape (works even
    # when HERMES_HOME is overridden for tests, because we look at the
    # segment names, not the absolute prefix).
    parts = [p.lower() for p in resolved.parts]
    for segment in ("mcp-tokens", "pairing"):
        if segment in parts:
            return True
    # auth.json / .anthropic_oauth.json / google_oauth.json under any
    # hermes home or profile dir.
    if name in {
        "auth.json",
        "auth.lock",
        ".anthropic_oauth.json",
        "google_oauth.json",
        "webhook_subscriptions.json",
        "bws_cache.json",
    }:
        return True
    # cache/bws_cache.json shape.
    return name == "bws_cache.json" and "cache" in parts


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

# Profile names must match Hermes' profile id regex.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Reserved names that would create confusing on-disk collisions or conflict
# with Hermes itself. Mirrors Hermes' _RESERVED_NAMES, with the special alias
# ``default`` handled separately (it is the built-in profile).
_RESERVED_PROFILE_NAMES: frozenset[str] = frozenset(
    {"hermes", "test", "tmp", "root", "sudo"}
)


def validate_profile_name(name: str) -> str:
    """Return the canonical profile id, raising if the name is invalid.

    Mirrors Hermes' normalize_profile_name + validate_profile_name. The
    special alias ``default`` is allowed and normalized to itself.
    """
    if not isinstance(name, str):
        raise TypeError("profile name must be a string")
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    if stripped.casefold() == "default":
        return "default"
    canon = stripped.lower()
    if not _PROFILE_NAME_RE.match(canon):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if canon in _RESERVED_PROFILE_NAMES:
        raise ValueError(
            f"Profile name {name!r} is reserved — it collides with either "
            f"the Hermes installation itself or a common system binary. "
            f"Pick a different name."
        )
    return canon


def parse_allowed_profiles(raw: str | None) -> list[str]:
    """Parse the HERMES_GPT_OPERATOR_ALLOWED_PROFILES env value.

    Returns a list of canonical profile names. ``"*"`` is preserved as a
    sentinel meaning "all existing profiles".
    """
    if not raw:
        return ["default"]
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        return ["default"]
    if "*" in items:
        return ["*"]
    normalized: list[str] = []
    for item in items:
        try:
            normalized.append(validate_profile_name(item))
        except ValueError:
            continue
    return normalized or ["default"]


def profile_is_allowed(
    profile: str, allowed: list[str], existing_profiles: Iterable[str] | None = None
) -> bool:
    """Return True if ``profile`` is in the ``allowed`` set.

    If ``allowed`` is ``["*"]``, every profile is allowed (subject to the
    caller validating that ``profile`` actually exists).
    """
    if not allowed:
        return False
    if allowed == ["*"]:
        return True
    try:
        canon = validate_profile_name(profile)
    except ValueError:
        return False
    return canon in {validate_profile_name(p) for p in allowed}


def list_existing_profiles(hermes_root: Path | None) -> list[str]:
    """List existing profile names under ``hermes_root``. Best-effort.

    Returns ``["default"]`` at minimum. Named profiles are discovered by
    listing ``<root>/profiles/*``.
    """
    names = ["default"]
    if hermes_root is None:
        return names
    profiles_dir = hermes_root / "profiles"
    if not profiles_dir.is_dir():
        return names
    try:
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                canon = validate_profile_name(entry.name)
            except ValueError:
                continue
            if canon != "default" and canon not in names:
                names.append(canon)
    except OSError:
        pass
    return names


def resolve_profile_home(profile: str, hermes_root: Path | None) -> Path:
    """Resolve the HERMES_HOME path for a profile.

    ``default`` -> ``hermes_root``
    ``<name>``  -> ``hermes_root / profiles / <name>``
    """
    canon = validate_profile_name(profile)
    if hermes_root is None:
        raise RuntimeError("Hermes root is not available; cannot resolve profile home")
    hermes_root = normalize_hermes_data_root(hermes_root) or hermes_root
    if canon == "default":
        return hermes_root
    return hermes_root / "profiles" / canon


def profile_exists(profile: str, hermes_root: Path | None) -> bool:
    """Return True if ``profile`` exists on disk under ``hermes_root``."""
    if hermes_root is None:
        return profile == "default"
    hermes_root = normalize_hermes_data_root(hermes_root) or hermes_root
    try:
        home = resolve_profile_home(profile, hermes_root)
    except (ValueError, RuntimeError):
        return False
    return home.is_dir()


# ---------------------------------------------------------------------------
# Allowed / denied path policy
# ---------------------------------------------------------------------------


def parse_path_list(raw: str | None) -> list[Path]:
    """Parse a comma- or newline-separated path list. Returns resolved Paths."""
    if not raw:
        return []
    sep = ","
    if "\n" in raw and "," not in raw:
        sep = "\n"
    out: list[Path] = []
    for item in raw.split(sep):
        text = item.strip()
        if not text:
            continue
        out.append(_normalize_path(text))
    return out


def path_under_allowed(path: str | os.PathLike[str], allowed: list[Path]) -> bool:
    """Return True if ``path`` resolves under one of the ``allowed`` roots."""
    if not allowed:
        return False
    resolved = _normalize_path(path)
    for root in allowed:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False
