"""HTTP and A2A response helpers for the Fabric coordinator."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

from hermes_gpt.fleet import fleet as op_fleet
from hermes_gpt.fleet.fabric_config import FabricNode
from hermes_gpt.fleet.fabric_protocol import _ID_RE, _MAX_BODY, _MAX_ITEMS, FabricError, _require_secure_transport, canonical_json, strict_json_loads


def http_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = canonical_json(body).encode("utf-8") if body is not None else None
    req_headers = {"Accept": "application/json", **headers}
    if data is not None:
        req_headers.update({"Content-Type": "application/json", "A2A-Version": "1.0"})
    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_BODY + 1)
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise FabricError(
                "FABRIC_TRANSPORT_TIMEOUT",
                "Fabric peer request timed out",
                ambiguous=body is not None,
            ) from exc
        raise FabricError(
            "FABRIC_PEER_UNAVAILABLE",
            "Fabric peer is unavailable",
            ambiguous=body is not None,
        ) from exc
    if len(raw) > _MAX_BODY:
        raise FabricError(
            "FABRIC_PAYLOAD_TOO_LARGE", "Fabric peer response exceeds the bounded limit"
        )
    parsed = strict_json_loads(raw)
    if not isinstance(parsed, dict):
        raise FabricError(
            "FABRIC_PROTOCOL_ERROR", "Fabric peer returned a non-object response"
        )
    return parsed


def peer_entry(node: FabricNode) -> dict[str, Any]:
    peers = op_fleet._a2a_peers_with_resolved_tokens()
    entry = peers.get(node.a2a_peer_name)
    if not isinstance(entry, dict):
        raise FabricError(
            "FABRIC_NODE_NOT_ENROLLED", "managed node has no configured A2A peer"
        )
    url = entry.get("url")
    if not isinstance(url, str):
        raise FabricError(
            "FABRIC_NODE_NOT_ENROLLED", "managed node peer has no configured URL"
        )
    _require_secure_transport(url)
    headers = op_fleet._auth_header(entry)
    if "Authorization" not in headers:
        raise FabricError(
            "FABRIC_PRINCIPAL_AUTH_REQUIRED",
            "verified Fabric requires a configured bearer credential",
        )
    return entry


def extract_data_response(task: Any) -> tuple[str | None, dict[str, Any]]:
    task_id: str | None = None
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal task_id
        if depth > 10 or len(found) > 8:
            return
        if isinstance(node, dict):
            if (
                task_id is None
                and isinstance(node.get("id"), str)
                and _ID_RE.fullmatch(node["id"])
            ):
                task_id = node["id"]
            if (
                isinstance(node.get("data"), dict)
                and node.get("mediaType") == "application/json"
            ):
                found.append(node["data"])
            for key in ("result", "task", "status", "message", "parts", "artifacts"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)

    walk(task)
    if len(found) != 1:
        raise FabricError(
            "FABRIC_PROTOCOL_ERROR",
            "Fabric A2A response must contain exactly one structured DataPart",
        )
    return task_id, found[0]
