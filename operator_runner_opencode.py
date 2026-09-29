"""OpenCode backend and its bounded loopback credential relay."""

from __future__ import annotations

import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import operator_policy as op
import runner_confinement as confinement
from operator_runner_common import (
    RUNNER_MODEL_ALLOWLIST_ENV,
    _allowed_by_env,
    _sandbox_for,
)
from operator_runner_local import _LocalProcessBackend

_OPENCODE_ENV_REF_RE = re.compile(
    r"^(?:\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*))$"
)


_OPENCODE_PROXY_MAX_BODY = 16 * 1024 * 1024


_OPENCODE_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _opencode_profile_env_value(
    contract: dict[str, Any],
    hermes_root: Path | None,
    name: str,
) -> str:
    value = os.environ.get(name)
    if value:
        return value
    profile = str(contract.get("assigned_profile") or "default")
    allowed_profiles = contract.get("allowed_scope", {}).get("profiles") or []
    if allowed_profiles and profile not in allowed_profiles:
        raise PermissionError(
            "OpenCode runner profile is outside the contract allowed_scope"
        )
    profile_home = op.resolve_profile_home(profile, hermes_root)
    import operator_config as op_config

    return op_config._read_env_value(profile_home / ".env", name) or ""


def _opencode_runtime_material(
    exe: str,
    contract: dict[str, Any],
    hermes_root: Path | None,
) -> dict[str, Any]:
    """Resolve only the provider material needed by the trusted Hermes worker.

    The real provider key is returned to the parent worker only. It is never put
    into the confined OpenCode environment, argv, request envelope, or logs.
    """
    try:
        completed = subprocess.run(
            [exe, "debug", "config", "--pure"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("OpenCode resolved configuration is unavailable") from exc
    if completed.returncode != 0:
        raise RuntimeError("OpenCode resolved configuration is unavailable")
    try:
        resolved = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OpenCode resolved configuration is not valid JSON") from exc
    if not isinstance(resolved, dict):
        raise TypeError("OpenCode resolved configuration is invalid")

    options = (contract.get("execution") or {}).get("options") or {}
    model = str(options.get("model") or resolved.get("model") or "").strip()
    if "/" not in model:
        raise ValueError("OpenCode requires a provider/model selection")
    if not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
        raise PermissionError(
            f"OpenCode model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
        )
    provider_id, model_id = model.split("/", 1)
    providers = (
        resolved.get("provider") if isinstance(resolved.get("provider"), dict) else {}
    )
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise TypeError(f"OpenCode provider {provider_id!r} is not configured")
    provider_options = (
        provider.get("options") if isinstance(provider.get("options"), dict) else {}
    )
    base_url = str(
        provider_options.get("baseURL") or provider_options.get("baseUrl") or ""
    ).strip()
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OpenCode provider baseURL must be an absolute HTTP(S) URL")

    raw_key = provider_options.get("apiKey")
    real_key = ""
    if isinstance(raw_key, str):
        match = _OPENCODE_ENV_REF_RE.fullmatch(raw_key.strip())
        if match:
            real_key = _opencode_profile_env_value(
                contract, hermes_root, match.group(1) or match.group(2)
            )
        else:
            real_key = raw_key
    if not real_key:
        raise PermissionError(
            "OpenCode provider authentication is unavailable to the trusted runner"
        )

    provider_name = str(provider.get("name") or provider_id)[:128]
    npm = str(provider.get("npm") or "@ai-sdk/openai-compatible")[:256]
    timeout_value = provider_options.get("timeout")
    timeout_ms = (
        int(timeout_value) if isinstance(timeout_value, (int, float)) else 600_000
    )
    timeout_ms = max(1_000, min(timeout_ms, 3_600_000))
    model_meta = {}
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    source_model = models.get(model_id) if isinstance(models, dict) else None
    if isinstance(source_model, dict) and isinstance(source_model.get("name"), str):
        model_meta["name"] = source_model["name"][:128]
    return {
        "model": model,
        "provider_id": provider_id,
        "model_id": model_id,
        "provider_name": provider_name,
        "npm": npm,
        "timeout_ms": timeout_ms,
        "model_meta": model_meta,
        "upstream": parsed,
        "real_key": real_key,
    }


class _OpenCodeCredentialProxy(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, material: dict[str, Any]):
        self.material = dict(material)
        self.material["relay_token"] = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _OpenCodeCredentialProxyHandler)


class _OpenCodeCredentialProxyHandler(BaseHTTPRequestHandler):
    server_version = "HermesOpenCodeRelay/1"

    @property
    def relay(self) -> _OpenCodeCredentialProxy:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_plain(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _forward(self) -> None:
        material = self.relay.material
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {material['relay_token']}"
        if not hmac.compare_digest(supplied, expected):
            self._send_plain(401, "unauthorized")
            return
        upstream = material["upstream"]
        incoming = urllib.parse.urlsplit(self.path)
        prefix = upstream.path.rstrip("/")
        if prefix and not (
            incoming.path == prefix or incoming.path.startswith(prefix + "/")
        ):
            self._send_plain(404, "not found")
            return
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length or "0")
        except ValueError:
            self._send_plain(400, "invalid content length")
            return
        if length < 0 or length > _OPENCODE_PROXY_MAX_BODY:
            self._send_plain(413, "request too large")
            return
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in _OPENCODE_HOP_HEADERS | {"host", "authorization", "content-length"}
        }
        headers["Authorization"] = f"Bearer {material['real_key']}"
        if body is not None:
            headers["Content-Length"] = str(len(body))
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        connection_cls = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_cls(upstream.hostname, port, timeout=90)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in _OPENCODE_HOP_HEADERS | {
                    "content-length",
                    "server",
                    "date",
                }:
                    continue
                self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
        except (OSError, http.client.HTTPException):
            if not self.wfile.closed:
                self._send_plain(502, "upstream unavailable")
        finally:
            connection.close()

    do_GET = _forward
    do_POST = _forward


def _opencode_child_config(material: dict[str, Any], proxy_port: int) -> str:
    upstream = material["upstream"]
    proxy_base = urllib.parse.urlunparse(
        ("http", f"127.0.0.1:{proxy_port}", upstream.path, "", "", "")
    )
    provider = {
        "name": material["provider_name"],
        "npm": material["npm"],
        "options": {
            "baseURL": proxy_base,
            "apiKey": material["relay_token"],
            "timeout": material["timeout_ms"],
        },
        "models": {material["model_id"]: material["model_meta"]},
    }
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": material["model"],
            "autoupdate": False,
            "provider": {material["provider_id"]: provider},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass
class OpenCodeBackend(_LocalProcessBackend):
    name: str = "opencode"

    def executable(self) -> str | None:
        configured = os.environ.get("HERMES_GPT_OPENCODE_EXE")
        candidates = [
            configured,
            shutil.which("opencode"),
            str(Path.home() / ".local" / "bin" / "opencode"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        options = (contract.get("execution") or {}).get("options") or {}
        sandbox = _sandbox_for(contract, backend="opencode")
        writable = sandbox == "workspace-write"
        if not confinement.confinement_available(writable=writable, expose_proc=True):
            posture = "write-capable" if writable else "read-only"
            raise PermissionError(
                f"opencode {posture} sessions require usable filesystem confinement; set "
                f"{confinement.CONFINEMENT_ENABLE_ENV}=1 and install a working bubblewrap "
                "(or sandbox-exec on macOS)"
            )
        model = str(options.get("model") or "").strip()
        if model and not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(
                f"OpenCode model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}"
            )
        agent = str(options.get("agent") or "").strip()
        variant = str(options.get("variant") or "").strip()
        if len(agent) > 128 or len(variant) > 128:
            raise ValueError("opencode agent/variant options must be <= 128 characters")
        return {
            "mode": "run",
            "format": "json",
            "pure": True,
            "sandbox": sandbox,
            "model": model or None,
            "agent": agent or None,
            "variant": variant or None,
        }
