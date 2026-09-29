"""Hermes configuration and HTTP transport for official A2A peers."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

A2A_REGISTRY_MODE_ENV = "HERMES_GPT_FLEET_A2A_MODE"
_A2A_DEFAULT_TIMEOUT = 30
_A2A_DEFAULT_REGISTRY_TIMEOUT = 10
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_REMOTE_BYTES = 1_048_576

# A peer served by this process can publish its card here. Fetching its
# loopback URL synchronously would block the event loop that serves the card.
_LOCAL_AGENT_CARDS: dict[str, dict[str, Any]] = {}


class FleetDispatchTimeout(TimeoutError):
    """A peer may have accepted a task even though the reply timed out.

    ``task_id`` is the peer-assigned A2A task id recovered by context lookup,
    not the local JSON-RPC request id.
    """

    def __init__(self, task_id: str):
        super().__init__("timed out awaiting A2A peer reply")
        self.task_id = task_id


def register_local_agent_card(url: str, card: dict[str, Any]) -> None:
    """Publish this process's Agent Card for loopback fleet verification."""
    _LOCAL_AGENT_CARDS[url.rstrip("/")] = card


def _a2a_mode() -> str:
    """Return the configured A2A backend mode: 'official', 'bridge', or 'auto'."""
    value = os.environ.get(A2A_REGISTRY_MODE_ENV, "auto").strip().lower()
    if value in {"official", "bridge", "auto"}:
        return value
    return "auto"


def _load_hermes_config() -> dict[str, Any]:
    """Load config.yaml lazily so importing the MCP server stays lightweight."""
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _a2a_peers() -> dict[str, dict[str, Any]]:
    """Return peer entries configured in Hermes ``config.yaml``."""
    return _load_hermes_config().get("a2a_agents") or {}


def _resolve_env_token(token: str) -> str:
    if token.startswith("${env:") and token.endswith("}"):
        name = token[6:-1].strip()
        return os.environ.get(name, "")
    return token


def _auth_header(peer: dict[str, Any]) -> dict[str, str]:
    auth = peer.get("auth") or {}
    if auth.get("type") == "bearer" and auth.get("token"):
        token = _resolve_env_token(auth["token"])
        if token:
            return {"Authorization": f"Bearer {token}"}
    return {}


def _a2a_peers_with_resolved_tokens() -> dict[str, dict[str, Any]]:
    """Return peer entries with configured ``${env:NAME}`` tokens resolved."""
    peers = _a2a_peers()
    out: dict[str, dict[str, Any]] = {}
    for name, entry in peers.items():
        entry = dict(entry)
        auth = entry.get("auth") or {}
        if auth.get("type") == "bearer" and isinstance(auth.get("token"), str):
            auth = dict(auth)
            auth["token"] = _resolve_env_token(auth["token"])
            entry["auth"] = auth
        out[name] = entry
    return out


def _http_get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if len(data) > _MAX_REMOTE_BYTES:
            raise ValueError("A2A discovery response exceeded the bounded response limit")
        return json.loads(data.decode("utf-8"))


def _http_get_json_threaded(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run the blocking card fetch off the event loop serving this MCP request."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_http_get_json, url, headers, timeout).result()


def _http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "A2A-Version": "1.0", **headers}
    req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _card_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/.well-known/agent-card.json"


def _fetch_card(base_url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    key = base_url.rstrip("/")
    if key in _LOCAL_AGENT_CARDS:
        return _LOCAL_AGENT_CARDS[key]
    try:
        return _http_get_json_threaded(_card_url(base_url), headers, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    legacy_url = base_url.rstrip("/") + "/.well-known/agent.json"
    return _http_get_json_threaded(legacy_url, headers, timeout)


def _jsonrpc_interface(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(card, dict):
        for interface in card.get("supportedInterfaces", []) or []:
            if (
                isinstance(interface, dict)
                and interface.get("protocolBinding") == "JSONRPC"
                and isinstance(interface.get("url"), str)
            ):
                return interface
    return None


def _rpc_url(base_url: str, card: dict[str, Any] | None) -> str:
    interface = _jsonrpc_interface(card)
    if interface is not None:
        return str(interface["url"])
    if isinstance(card, dict) and isinstance(card.get("url"), str) and card["url"]:
        return card["url"]
    return base_url.rstrip("/")


def _interface_tenant(card: dict[str, Any] | None, peer: dict[str, Any]) -> str:
    interface = _jsonrpc_interface(card)
    if interface is not None and interface.get("tenant"):
        return str(interface["tenant"])
    return str(peer.get("tenant") or "")


def _send_message(agent: str, peer: dict[str, Any], text: str, timeout: int) -> dict[str, Any]:
    base_url = peer.get("url", "")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError(f"peer '{agent}' has no valid URL")

    headers = _auth_header(peer)
    cap = max(5, min(int(timeout), 120))
    card: dict[str, Any] | None = None
    try:
        card = _fetch_card(base_url, headers, min(cap, 30))
    except Exception:
        # Card discovery is best-effort; older peers may still accept the base URL.
        pass

    rpc_url = _rpc_url(base_url, card)
    request_id = f"req-{hashlib.sha256((agent + text + str(os.urandom(8))).encode()).hexdigest()[:16]}"
    context_id = f"ctx-{hashlib.sha256((request_id + str(os.urandom(8))).encode()).hexdigest()[:16]}"
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": text, "mediaType": "text/plain"}],
                "messageId": request_id.replace("req-", "msg-"),
                "contextId": context_id,
            },
        },
    }
    tenant = _interface_tenant(card, peer)
    if tenant:
        body["params"]["tenant"] = tenant

    def recover_task_id() -> str:
        lookup = {
            "jsonrpc": "2.0",
            "id": f"lookup-{request_id}",
            "method": "ListTasks",
            "params": {
                "contextId": context_id,
                "pageSize": 5,
                "includeArtifacts": False,
                "historyLength": 0,
            },
        }
        if tenant:
            lookup["params"]["tenant"] = tenant
        recovered = _http_post_json(rpc_url, lookup, headers, min(max(cap, 5), 15))
        result = recovered.get("result", {}) if isinstance(recovered, dict) else {}
        tasks = result.get("tasks", []) if isinstance(result, dict) else []
        matches = [
            item.get("id")
            for item in tasks
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and _TASK_ID_RE.fullmatch(item["id"])
        ]
        if len(matches) != 1:
            raise RuntimeError("could not uniquely recover peer task after timeout")
        return matches[0]

    try:
        response = _http_post_json(rpc_url, body, headers, cap)
    except TimeoutError as exc:
        raise FleetDispatchTimeout(recover_task_id()) from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise FleetDispatchTimeout(recover_task_id()) from exc
        raise
    if not isinstance(response, dict):
        raise ValueError("A2A peer returned a non-JSON-RPC response")
    if "error" in response:
        error = response["error"]
        raise RuntimeError(f"A2A peer returned error: {error.get('message', error)}")
    return response.get("result", {})


def _get_task(agent: str, peer: dict[str, Any], task_id: str, timeout: int) -> dict[str, Any]:
    base_url = peer.get("url", "")
    headers = _auth_header(peer)
    cap = max(1, min(int(timeout), 60))
    card: dict[str, Any] | None = None
    try:
        card = _fetch_card(base_url, headers, min(cap, 30))
    except Exception:
        pass
    rpc_url = _rpc_url(base_url, card)
    body = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "GetTask",
        "params": {"id": task_id},
    }
    tenant = _interface_tenant(card, peer)
    if tenant:
        body["params"]["tenant"] = tenant
    response = _http_post_json(rpc_url, body, headers, cap)
    if not isinstance(response, dict):
        raise ValueError("A2A peer returned a non-JSON-RPC response")
    if "error" in response:
        error = response["error"]
        raise RuntimeError(f"A2A peer returned error: {error.get('message', error)}")
    return response.get("result", {})


def _registry_official(*, timeout: int = _A2A_DEFAULT_REGISTRY_TIMEOUT) -> list[dict[str, Any]]:
    """List Hermes-configured A2A peers and probe their Agent Cards."""
    peers = _a2a_peers_with_resolved_tokens()
    clean: list[dict[str, Any]] = []
    cap = max(1, min(int(timeout), 30))
    for name, entry in peers.items():
        if not _AGENT_RE.fullmatch(name):
            continue
        url = entry.get("url", "")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        headers = _auth_header(entry)
        has_token = bool(headers.get("Authorization"))
        try:
            _fetch_card(url, headers, cap)
        except Exception:
            pass
        clean.append({"name": name, "has_token": has_token})
    return clean
