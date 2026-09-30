"""Bounded MCP request diagnostics. Record decisions and IDs, never payloads."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from hermes_gpt.mcp_compat import SDK_V2, HermesMCP

request_id = contextvars.ContextVar("hermes_request_id", default=None)
_lock = threading.Lock()
_handlers: dict[Path, RotatingFileHandler] = {}
_ID = re.compile(r"(?:[a-f0-9]{32}|[0-9]{8}_[0-9]{6}_[a-f0-9]{6})\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


def log_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "logs" / "hermes_gpt_requests.jsonl"


class _PrivateHandler(RotatingFileHandler):
    def _open(self) -> Any:
        # O_CREAT's mode protects the file from its first byte, including after
        # rotation. chmod also repairs an older file's permissive mode.
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def _write(record: dict[str, Any]) -> None:
    try:
        path = log_path()
        with _lock:
            handler = _handlers.get(path)
            if handler is None:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                handler = _PrivateHandler(path, maxBytes=5_000_000, backupCount=2)
                _handlers[path] = handler
            handler.emit(logging.LogRecord("hermes.requests", logging.INFO, "", 0,
                json.dumps(record, sort_keys=True), (), None))
    except OSError:
        # Diagnostics must not turn a successful user request into a failure.
        logging.getLogger(__name__).warning("MCP request diagnostics could not be written")


def _normalize(result: Any) -> Any:
    if SDK_V2 or isinstance(result, CallToolResult):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return CallToolResult(content=result[0], structuredContent=result[1])
    if isinstance(result, dict):
        return CallToolResult(content=[], structuredContent=result)
    if isinstance(result, list):
        return CallToolResult(content=result)
    return result


def _summary(result: Any) -> dict[str, Any]:
    wire = result.model_dump(by_alias=True) if hasattr(result, "model_dump") else {}
    body = wire.get("structuredContent")
    if not isinstance(body, dict):
        for block in wire.get("content", []):
            if block.get("type") == "text":
                try:
                    candidate = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict):
                    body = candidate
                    break
    body = body if isinstance(body, dict) else {}
    summary: dict[str, Any] = {"outcome": "error" if wire.get("isError") or body.get("success") is False or body.get("ok") is False else "ok"}
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else body.get("code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        summary["error_code"] = code
    for view in (body, body.get("job"), body.get("task")):
        if not isinstance(view, dict):
            continue
        for key in ("job_id", "task_id", "session_id"):
            value = view.get(key)
            if isinstance(value, str) and _ID.fullmatch(value):
                summary[key] = value
        if isinstance(view.get("status"), str) and view.get("status") in {"running", "completed", "failed", "cancelled", "timed_out", "orphaned"}:
            summary["job_status"] = view["status"]
        if isinstance(view.get("return_code"), int) and not isinstance(view["return_code"], bool):
            summary["return_code"] = view["return_code"]
    return summary


class TracedMCP(HermesMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        trace = uuid.uuid4().hex
        token = request_id.set(trace)
        started = time.monotonic()
        base = {"request_id": trace, "tool": name if re.fullmatch(r"hermes_[a-z0-9_]{1,90}", name) else "unknown"}
        for key in ("job_id", "task_id", "session_id"):
            value = arguments.get(key)
            if isinstance(value, str) and _ID.fullmatch(value):
                base[key] = value
        for key in ("confirm", "dry_run"):
            if isinstance(arguments.get(key), bool):
                base[key] = arguments[key]
        _write({**base, "timestamp": datetime.now(timezone.utc).isoformat(), "phase": "started"})
        summary: dict[str, Any] = {"outcome": "error"}
        try:
            result = await super().call_tool(name, arguments, *args, **kwargs)
            summary = _summary(_normalize(result))
            if isinstance(result, CallToolResult):
                # MCP metadata is outside the tool's output schema.
                result.meta = {**(result.meta or {}), "hermes_request_id": trace}
            return result
        except BaseException as exc:
            # Exceptions can contain prompts, URLs, and keys. Record type only.
            summary["exception_type"] = type(exc).__name__
            raise
        finally:
            _write({**base, **summary, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "phase": "finished", "duration_ms": round((time.monotonic() - started) * 1000)})
            request_id.reset(token)


def request_diagnostics(limit: int = 20, trace_id: str | None = None) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return {"success": False, "code": "INVALID_LIMIT"}
    if trace_id is not None and not re.fullmatch(r"[a-f0-9]{32}", trace_id):
        return {"success": False, "code": "INVALID_REQUEST_ID"}
    try:
        with log_path().open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 1_000_000))
            if size > 1_000_000:
                handle.readline()  # Drop a partial first record.
            lines = handle.read(1_000_000).decode("utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError:
        return {"success": False, "code": "REQUEST_LOG_UNAVAILABLE"}
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and (trace_id is None or record.get("request_id") == trace_id):
            records.append(record)
    return {"success": True, "records": records[-limit:], "scope": "Recent MCP calls; private inputs and answers are excluded."}
