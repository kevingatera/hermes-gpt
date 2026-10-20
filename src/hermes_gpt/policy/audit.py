"""Append and read bounded Operator audit records."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt import paths
from hermes_gpt.policy.paths import _normalize_path

# Prefer the user's Hermes log directory, then fall back beside this package.
AUDIT_LOG_HERMES_PATH = (
    Path.home()
    / "AppData"
    / "Local"
    / "hermes"
    / "logs"
    / "hermes_gpt_operator_audit.jsonl"
)
# A source checkout keeps its own logs directory; an installed wheel writes
# under the user state directory instead of inside the package.
AUDIT_LOG_FALLBACK_PATH = (
    paths.runtime_state_dir() / "logs" / "hermes_gpt_operator_audit.jsonl"
)
_audit_log_override: Path | None = None
_audit_lock = threading.Lock()


def set_audit_log_override(path: Path | None) -> None:
    """Set or clear the audit log path override (for tests)."""
    global _audit_log_override
    with _audit_lock:
        _audit_log_override = path


def audit_log_path() -> Path:
    """Return the active audit log path."""
    with _audit_lock:
        if _audit_log_override is not None:
            return _audit_log_override
    # Prefer the Hermes logs dir if it exists / is writable.
    try:
        if AUDIT_LOG_HERMES_PATH.parent.exists():
            return AUDIT_LOG_HERMES_PATH
    except OSError:
        pass
    return AUDIT_LOG_FALLBACK_PATH


def _hash_secret_text(text: str | None) -> tuple[int, str]:
    """Return (length, sha256_hex) for prompt/content fields. Never log raw."""
    if text is None:
        return (0, "")
    data = text.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def audit_record(
    *,
    tool: str,
    level: str,
    apply_mode: str,
    dry_run: bool,
    success: bool,
    changed: bool = False,
    summary: str = "",
    error: str = "",
    profile: str | None = None,
    source_profile: str | None = None,
    target_profile: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
    skill_name: str | None = None,
    prompt: str | None = None,
    content: str | None = None,
    key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a single audit record to the JSONL log. Returns the record.

    Sensitive inputs (prompt, content) are recorded as length + sha256 only.
    Path is summarized to its basename + length, not the full path, to avoid
    leaking directory structure that might itself contain secret hints.

    The record is also returned so callers can include it in tool output.
    """
    prompt_len, prompt_sha = _hash_secret_text(prompt)
    content_len, content_sha = _hash_secret_text(content)

    path_summary = ""
    if path:
        try:
            resolved = _normalize_path(path)
            path_summary = f"{resolved.name} (<{len(str(resolved))} chars>)"
        except Exception:  # noqa: BLE001 - A malformed path summary must not drop the audit record.
            path_summary = f"<path> (<{len(str(path))} chars>)"

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "level": level,
        "apply_mode": apply_mode,
        "dry_run": bool(dry_run),
        "success": bool(success),
        "changed": bool(changed),
        "summary": summary[:500] if summary else "",
        "error": error[:500] if error else "",
        "profile": profile,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "path_summary": path_summary,
        "job_id": job_id,
        "skill_name": skill_name,
        "key": key,
        "prompt_len": prompt_len,
        "prompt_sha256": prompt_sha,
        "content_len": content_len,
        "content_sha256": content_sha,
    }
    if extra:
        # Extra must already be sanitized by the caller; we only truncate
        # string values to avoid accidental giant dumps.
        for k, v in extra.items():
            if isinstance(v, str):
                record[k] = v[:500]
            else:
                record[k] = v

    try:
        log_path = audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with _audit_lock, open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Audit failure must never break a tool. The record is returned so
        # callers can still surface it inline.
        pass

    return record


def audit_tail(limit: int = 20) -> list[dict[str, Any]]:
    """Read the last ``limit`` audit records. Returns newest-last."""
    log_path = audit_log_path()
    if not log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit <= 0:
        return records
    return records[-limit:]
