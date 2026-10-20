"""Bounded JSON parsing and safe completion bundles from fleet peers."""

from __future__ import annotations

import json
import re
from typing import Any

from hermes_gpt.fleet import a2a as fleet_a2a
from hermes_gpt.fleet import authority
from hermes_gpt.fleet import work_orders
from hermes_gpt.policy import authorization as op

_MAX_REMOTE_BYTES = fleet_a2a._MAX_REMOTE_BYTES
_MAX_ITEMS = authority._MAX_ITEMS
_MAX_TEXT = authority._MAX_TEXT
_CONTROL_RE = authority._CONTROL_RE
_clean_text = authority._clean_text
_authorization = work_orders._authorization


def _parse_json(stdout: str, *, operation: str) -> dict[str, Any]:
    if not isinstance(stdout, str):
        # Preserve the fleet validation error contract for malformed replies.
        raise ValueError(f"{operation} returned invalid UTF-8 text")  # noqa: TRY004
    if len(stdout.encode("utf-8")) > _MAX_REMOTE_BYTES:
        raise ValueError(f"{operation} response exceeded the bounded response limit")
    cleaned = _CONTROL_RE.sub("", stdout).lstrip("\ufeff")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", cleaned):
        try:
            parsed, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if cleaned.rstrip().endswith(("{", "[", ",", ":")) or cleaned.count("{") > cleaned.count("}"):
        raise ValueError(f"{operation} returned truncated JSON")
    raise ValueError(f"{operation} returned invalid JSON")


def _text_parts(value: Any) -> list[str]:
    found: list[str] = []
    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= _MAX_ITEMS:
            return
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                found.append(_clean_text(node["text"], field="result text", maximum=_MAX_TEXT, required=False))
            for key in ("parts", "content", "data"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)
    walk(value)
    return [x for x in found if x]


def _completion_payload(task: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for source in (task.get("result"), task.get("artifacts"), task.get("status")):
        if isinstance(source, dict):
            candidates.append(source)
        elif isinstance(source, list):
            candidates.extend(item for item in source[:_MAX_ITEMS] if isinstance(item, dict))
    texts = _text_parts(candidates)
    for text in texts:
        try:
            candidates.insert(0, _parse_json(text, operation="completed task content"))
        except ValueError:
            pass
    allowed = {"status", "node", "profile", "summary", "changed_paths", "artifacts", "verification", "residual_risk", "recommended_next_action", "authorization"}
    raw: dict[str, Any] = {}
    for candidate in candidates:
        inner = candidate.get("completion_bundle", candidate)
        if isinstance(inner, dict) and any(key in inner for key in allowed):
            raw = inner
            break
    status_obj = task.get("status") if isinstance(task.get("status"), dict) else {}
    def bounded(value: Any, field: str) -> str:
        return op.redact_output(_clean_text(value if isinstance(value, str) else "", field=field, maximum=_MAX_TEXT, required=False))
    result = {
        "status": bounded(raw.get("status") or status_obj.get("state") or "unknown", "status"),
        "node": bounded(raw.get("node") or "", "node"),
        "profile": bounded(raw.get("profile") or "", "profile"),
        "summary": bounded(raw.get("summary") or (texts[0] if texts else ""), "summary"),
        "changed_paths": [],
        "artifacts": [],
        "verification": [],
        "residual_risk": bounded(raw.get("residual_risk") or "", "residual_risk"),
        "recommended_next_action": bounded(raw.get("recommended_next_action") or "", "recommended_next_action"),
        "authorization": {"class": "none", "approved": False},
    }
    for field in ("changed_paths", "artifacts", "verification"):
        value = raw.get(field, [])
        if isinstance(value, list):
            result[field] = [bounded(x, field) for x in value[:_MAX_ITEMS] if isinstance(x, str)]
    if isinstance(raw.get("authorization"), (dict, str)):
        try:
            result["authorization"] = _authorization(raw["authorization"])
        except (ValueError, PermissionError):
            pass
    return result


