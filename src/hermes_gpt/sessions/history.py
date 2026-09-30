"""Read-only access to Hermes session records and message history."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from hermes_gpt.policy import authorization as op_policy

MAX_LIST_LIMIT = 100
MAX_PAGE_SIZE = 100
MAX_EXPORT_MESSAGES = 500
MAX_OFFSET = 10_000
MAX_ID_LENGTH = 256
MAX_QUERY_LENGTH = 512
MAX_MESSAGE_SCAN_ROWS = 1_000
INTERNAL_CONTENT_ENV = "HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT"

_DEFAULT_MESSAGE_ROLES = {"user", "assistant"}
_INTERNAL_MESSAGE_ROLES = {"system", "tool", "function"}
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|private[_-]?key)"
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|\\\\|/(?:Users|home|mnt|var|tmp)/)[^\s\"']+"
)


class SessionSearchUnavailable(RuntimeError):
    """Raised when Hermes does not provide its read-only full-text search API."""


def env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def _default_hermes_root() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op_policy.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    return Path.home() / ".hermes"


def validate_profile(profile: str, hermes_root: Path | None = None) -> str:
    """Validate a profile name and require Operator authorization for named profiles."""
    canon = op_policy.validate_profile_name(profile)
    if canon != "default":
        op_policy.OperatorPolicy().require_profile(canon, hermes_root)
    return canon


def profile_db_path(profile: str, hermes_root: Path | None = None) -> Path:
    """Resolve the authorized profile's Hermes state database."""
    canon = validate_profile(profile, hermes_root)
    profile_home = op_policy.resolve_profile_home(canon, hermes_root or _default_hermes_root())
    return profile_home / "state.db"


def validate_limit(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer.")  # noqa: TRY004 - keep the existing MCP validation error type.
    if value < 0:
        raise ValueError(f"{name} must not be negative.")
    if value > maximum:
        raise ValueError(f"{name} exceeds the maximum of {maximum}.")
    return value


def validate_offset(value: int) -> int:
    return validate_limit(value, "offset", MAX_OFFSET)


def validate_bool(value: bool, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean.")  # noqa: TRY004 - keep the existing MCP validation error type.
    return value


def validate_session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a string.")  # noqa: TRY004 - keep the existing MCP validation error type.
    normalized = value.strip()
    if not normalized:
        raise ValueError("session_id must not be empty.")
    if len(normalized) > MAX_ID_LENGTH:
        raise ValueError(f"session_id exceeds the maximum of {MAX_ID_LENGTH} characters.")
    return normalized


def validate_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be a string.")  # noqa: TRY004 - keep the existing MCP validation error type.
    normalized = value.strip()
    if not normalized:
        raise ValueError("query must not be empty.")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise ValueError(f"query exceeds the maximum of {MAX_QUERY_LENGTH} characters.")
    return normalized


def utf8_response_bytes(value: str) -> int:
    if not isinstance(value, str):
        raise TypeError("response value must be a string.")
    return len(value.encode("utf-8"))


def redact_text(value: Any) -> str:
    text = op_policy.redact_output(str(value))
    return _ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", text)


def redact_error(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")


def redact_value(value: Any, *, key: str | None = None) -> Any:
    if key and key != "session_id" and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(item_key): redact_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item) for item in value]
    return value


def safe_session_metadata(row: dict[str, Any]) -> dict[str, Any]:
    safe_keys = (
        "id",
        "source",
        "started_at",
        "ended_at",
        "last_active",
        "message_count",
        "tool_call_count",
        "archived",
    )
    result = {key: row[key] for key in safe_keys if key in row}
    result["has_title"] = bool(row.get("title"))
    return redact_value(result)


def safe_message(row: dict[str, Any], allowed_roles: set[str]) -> dict[str, Any] | None:
    requested_internal = set(allowed_roles) & _INTERNAL_MESSAGE_ROLES
    if requested_internal and not env_enabled(INTERNAL_CONTENT_ENV):
        raise RuntimeError(
            "Internal session content is disabled. Set "
            f"{INTERNAL_CONTENT_ENV}=1 to request system or tool messages."
        )
    role = row.get("role")
    safe_roles = _DEFAULT_MESSAGE_ROLES | _INTERNAL_MESSAGE_ROLES
    if role not in set(allowed_roles) & safe_roles:
        return None
    safe_keys = ("id", "session_id", "role", "timestamp", "content")
    result = {key: row[key] for key in safe_keys if key in row}
    return redact_value(result)


def safe_search_message(
    row: dict[str, Any], allowed_roles: set[str]
) -> dict[str, Any] | None:
    """Project a SessionDB search row through the normal message safeguards."""
    projected = dict(row)
    snippet = row.get("snippet")
    if isinstance(snippet, str):
        projected["content"] = snippet
    return safe_message(projected, allowed_roles)


def allowed_message_roles(
    *,
    include_system_messages: bool = False,
    include_tool_messages: bool = False,
) -> set[str]:
    if (include_system_messages or include_tool_messages) and not env_enabled(INTERNAL_CONTENT_ENV):
        raise RuntimeError(
            "Internal session content is disabled. Set "
            f"{INTERNAL_CONTENT_ENV}=1 to request system or tool messages."
        )
    allowed = set(_DEFAULT_MESSAGE_ROLES)
    if include_system_messages:
        allowed.add("system")
    if include_tool_messages:
        allowed.update({"tool", "function"})
    return allowed


class ReadOnlySessionStore:
    """Bounded adapter around Hermes' verified read-only SessionDB API."""

    def __init__(
        self,
        db_factory: Any,
        connection_type: type = sqlite3.Connection,
        profile: str = "default",
        *,
        hermes_root: Path | None = None,
        profile_validator: Callable[[str], str] | None = None,
        profile_db_resolver: Callable[[str], Path] | None = None,
    ):
        self._db_factory = db_factory
        self._connection_type = connection_type
        validate_profile_fn = profile_validator or (
            lambda selected: validate_profile(selected, hermes_root)
        )
        self._profile = validate_profile_fn(profile)
        self._hermes_root = hermes_root
        self._profile_db_resolver = profile_db_resolver or (
            lambda selected: profile_db_path(selected, hermes_root)
        )
        self._db = None
        self._disposed = False

    def open(self) -> ReadOnlySessionStore:
        if self._disposed:
            raise RuntimeError("Read-only session adapter has already been disposed.")
        if self._db is not None:
            return self
        if self._db_factory is None:
            raise RuntimeError("Hermes session database is unavailable: SessionDB import failed.")
        try:
            self._db = self._db_factory(
                db_path=self._profile_db_resolver(self._profile),
                read_only=True,
            )
        except Exception as exc:
            raise RuntimeError(f"Hermes session database is unavailable: {redact_error(exc)}") from exc
        return self

    def __enter__(self) -> ReadOnlySessionStore:  # noqa: PYI034 - typing.Self is unavailable on Python 3.10.
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.dispose_safely()

    def _require_db(self) -> Any:
        if self._db is None or self._disposed:
            raise RuntimeError("Read-only session adapter is not open.")
        return self._db

    def list_sessions(self, *, limit: int, offset: int, include_archived: bool = False) -> list[dict[str, Any]]:
        db = self._require_db()
        safe_limit = validate_limit(limit, "limit", MAX_LIST_LIMIT)
        safe_offset = validate_offset(offset)
        safe_include_archived = validate_bool(include_archived, "include_archived")
        return db.list_sessions_rich(
            limit=safe_limit,
            offset=safe_offset,
            include_archived=safe_include_archived,
            compact_rows=True,
        )

    def resolve_session_id(self, session_id_or_prefix: str) -> str | None:
        return self._require_db().resolve_session_id(validate_session_id(session_id_or_prefix))

    def get_canonical_bot_chat(self) -> dict[str, Any] | None:
        """Resolve this profile's canonical Bot Chat registry row and live tip."""
        db = self._require_db()
        if not hasattr(db, "get_session_by_title") or not hasattr(db, "get_compression_tip"):
            raise RuntimeError("Installed SessionDB runtime lacks canonical Bot Chat lookup support.")
        title = getattr(db, "CANONICAL_BOT_CHAT_TITLE", "Bot Chat")
        registry = db.get_session_by_title(title)
        if not isinstance(registry, dict):
            return None
        if str(registry.get("source") or "").strip().lower() in {"kanban", "tool"}:
            return None
        if bool(registry.get("archived")):
            return None
        registry_id = registry.get("id")
        if not isinstance(registry_id, str) or not registry_id:
            return None
        tip_id = db.get_compression_tip(registry_id) or registry_id
        current = registry
        if tip_id != registry_id:
            if not hasattr(db, "get_session"):
                raise RuntimeError("Installed SessionDB runtime cannot hydrate the Bot Chat compression tip.")
            resolved = db.get_session(tip_id)
            if isinstance(resolved, dict):
                current = resolved
        return {
            "registry": registry,
            "current": current,
            "registry_session_id": registry_id,
            "current_session_id": tip_id,
            "canonical_title": title,
        }

    def get_messages_page(
        self,
        session_id: str,
        *,
        limit: int,
        offset: int,
        include_inactive: bool = False,
        include_system_messages: bool = False,
        include_tool_messages: bool = False,
    ) -> dict[str, Any]:
        db = self._require_db()
        safe_id = validate_session_id(session_id)
        safe_limit = validate_limit(limit, "limit", MAX_EXPORT_MESSAGES)
        safe_offset = validate_offset(offset)
        safe_include_inactive = validate_bool(include_inactive, "include_inactive")
        safe_include_system = validate_bool(include_system_messages, "include_system_messages")
        safe_include_tool = validate_bool(include_tool_messages, "include_tool_messages")
        roles = allowed_message_roles(
            include_system_messages=safe_include_system,
            include_tool_messages=safe_include_tool,
        )
        projected: list[dict[str, Any]] = []
        cursor = safe_offset
        rows_examined = 0
        source_exhausted = safe_limit == 0
        while len(projected) < safe_limit and rows_examined < MAX_MESSAGE_SCAN_ROWS:
            fetch_limit = min(
                MAX_PAGE_SIZE,
                MAX_MESSAGE_SCAN_ROWS - rows_examined,
                max(1, safe_limit - len(projected)),
            )
            raw_rows = db.get_messages(
                safe_id,
                limit=fetch_limit,
                offset=cursor,
                include_inactive=safe_include_inactive,
            )
            examined_now = len(raw_rows)
            if examined_now == 0:
                source_exhausted = True
                break
            rows_examined += examined_now
            cursor += examined_now
            for row in raw_rows:
                message = safe_message(row, roles)
                if message is not None:
                    projected.append(message)
                    if len(projected) >= safe_limit:
                        break
            if examined_now < fetch_limit:
                source_exhausted = True
                break

        return {
            "messages": projected[:safe_limit],
            "next_offset": cursor,
            "rows_examined": rows_examined,
            "has_more": not source_exhausted,
            "scan_limited": rows_examined >= MAX_MESSAGE_SCAN_ROWS and not source_exhausted,
        }

    def get_messages(
        self,
        session_id: str,
        *,
        limit: int,
        offset: int,
        include_inactive: bool = False,
        include_system_messages: bool = False,
        include_tool_messages: bool = False,
    ) -> list[dict[str, Any]]:
        return self.get_messages_page(
            session_id,
            limit=limit,
            offset=offset,
            include_inactive=include_inactive,
            include_system_messages=include_system_messages,
            include_tool_messages=include_tool_messages,
        )["messages"]

    def search_messages(self, *, query: str, limit: int, offset: int) -> list[dict[str, Any]]:
        db = self._require_db()
        if not hasattr(db, "search_messages"):
            raise SessionSearchUnavailable("read-only FTS search_messages API is unavailable")
        if hasattr(db, "_fts_enabled") and not bool(db._fts_enabled):
            raise SessionSearchUnavailable("read-only FTS is disabled by the installed SessionDB runtime")
        safe_query = validate_query(query)
        safe_limit = validate_limit(limit, "limit", MAX_LIST_LIMIT)
        safe_offset = validate_offset(offset)
        raw_rows = db.search_messages(query=safe_query, limit=safe_limit, offset=safe_offset)
        roles = allowed_message_roles()
        projected = []
        for row in raw_rows:
            message = safe_search_message(row, roles)
            if message is not None:
                projected.append(message)
        return projected

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        """INTERNAL RAW export; callers must project before client exposure."""
        return self._require_db().export_session(validate_session_id(session_id))

    def export_session_lineage(self, session_id: str) -> dict[str, Any] | None:
        """INTERNAL RAW lineage export; never return directly from an MCP tool."""
        return self._require_db().export_session_lineage(validate_session_id(session_id))

    def dispose_safely(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        db = self._db
        if db is None:
            return
        connection = getattr(db, "_conn", None)
        if not isinstance(connection, self._connection_type):
            return
        try:
            connection.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the session response.
            print(f"hermes-gpt: read-only session disposal failed: {redact_error(exc)}", file=sys.stderr)
