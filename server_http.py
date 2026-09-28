"""HTTP transport, authentication, and discovery routes for Hermes GPT."""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

import oauth_auth
import operator_fleet
import operator_live_events

REMOTE_PROFILE = "remote"
TRUSTED_PROXY_IPS_ENV = "HERMES_GPT_TRUSTED_PROXY_IPS"


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def oauth_state_from_env(
    get_hermes_root: Callable[[], Path | None],
) -> oauth_auth.OAuthState | None:
    config = oauth_auth.config_from_env()
    if config is None:
        return None
    state = oauth_auth.OAuthState(config)
    try:
        state.restore_tokens(get_hermes_root())
    except Exception:  # noqa: BLE001
        # A missing or corrupt token envelope fails closed to empty stores.
        return state
    return state


def auth_enabled() -> bool:
    return (
        oauth_auth.static_bearer_from_env() is not None
        or oauth_auth.config_from_env() is not None
    )


def trusted_proxy_ips_from_env() -> str:
    raw_value = os.environ.get(TRUSTED_PROXY_IPS_ENV, "").strip()
    if not raw_value:
        return ""
    addresses: list[str] = []
    for value in raw_value.split(","):
        candidate = value.strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError(
                f"{TRUSTED_PROXY_IPS_ENV} must contain only comma-separated IP addresses."
            ) from exc
        if not address.is_loopback:
            raise ValueError(
                f"{TRUSTED_PROXY_IPS_ENV} accepts loopback proxy addresses only."
            )
        addresses.append(str(address))
    return ",".join(dict.fromkeys(addresses))


def authenticated_http_security_options(
    *,
    profile: str,
    host: str,
    cert: str | None,
    key: str | None,
    configured_auth: bool,
) -> tuple[bool, str]:
    if bool(cert) != bool(key):
        raise SystemExit("TLS requires both --cert and --key.")
    if profile != REMOTE_PROFILE or not configured_auth:
        return False, ""
    if cert and key:
        return False, ""
    try:
        trusted_proxies = trusted_proxy_ips_from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not trusted_proxies or not is_loopback_host(host):
        raise SystemExit(
            "Authenticated remote mode requires direct TLS (--cert and --key), or a loopback bind behind an "
            f"explicit trusted HTTPS proxy configured with {TRUSTED_PROXY_IPS_ENV}."
        )
    return True, trusted_proxies


async def health_root(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "hermes-gpt", "mcp_path": "/mcp"})


def _fleet_peer_name() -> str:
    return os.environ.get("HERMES_GPT_FLEET_PEER_NAME", "").strip() or "hermes-peer"


def _fleet_peer_url() -> str:
    host = os.environ.get("HERMES_GPT_HOST", "127.0.0.1")
    port = os.environ.get("HERMES_GPT_PORT", "4750")
    return os.environ.get("HERMES_GPT_FLEET_PEER_URL", "").strip() or f"http://{host}:{port}"


def _fleet_card() -> dict[str, Any]:
    peer_name = _fleet_peer_name()
    peer_url = _fleet_peer_url()
    return {
        "protocolVersion": "1.0",
        "name": peer_name,
        "description": "Hermes Fleet peer",
        "version": os.environ.get("HERMES_GPT_FLEET_PEER_VERSION", "0.20.5"),
        "url": peer_url,
        "capabilities": {},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "hermes-agent-v1",
                "name": "Hermes Agent",
                "description": "Local-first Hermes Agent",
                "tags": ["hermes", "fleet"],
            }
        ],
        "supportedInterfaces": [
            {
                "url": peer_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
    }


async def _fleet_agent_card(_request: Request) -> JSONResponse:
    """Return the local peer identity without credentials or tokens."""
    return JSONResponse(_fleet_card())


def register_fleet_local_card() -> None:
    """Publish the local peer card so co-located status checks avoid HTTP."""
    try:
        card = _fleet_card()
        operator_fleet.register_local_agent_card(card["url"], card)
    except Exception:  # noqa: BLE001, S110
        # Local card publication is optional and must not prevent startup.
        pass


def build_asgi_app(
    server: Any,
    *,
    http: bool,
    get_hermes_root: Callable[[], Path | None],
    eprint: Callable[[str], None],
) -> Any:
    oauth_state = getattr(server, "_hermes_oauth_state", None)
    if oauth_state is not None and not http:
        raise ValueError("Built-in OAuth is supported only with streamable HTTP (--http).")
    raw_mcp_app = server.streamable_http_app() if http else server.sse_app()
    mcp_app = oauth_auth.DefaultMcpAcceptMiddleware(raw_mcp_app)
    static_bearer = oauth_auth.static_bearer_from_env() or ""

    async def live_websocket_authorized(websocket: Any) -> bool:
        """Apply MCP bearer/OAuth checks to live-event websocket handshakes."""
        if oauth_state is None and not static_bearer:
            return True
        admitted = False

        async def admitted_app(_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
            nonlocal admitted
            admitted = True

        auth_middleware = oauth_auth.BearerAuthMiddleware(
            admitted_app,
            oauth_state,
            static_token=static_bearer,
        )
        scope = dict(websocket.scope)
        scope.update(
            {
                "type": "http",
                "http_version": scope.get("http_version", "1.1"),
                "scheme": "http",
                "method": "POST",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
            }
        )
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: dict[str, Any]) -> None:
            return None

        await auth_middleware(scope, receive, send)
        return admitted

    routes: list[BaseRoute] = [Route("/", health_root, methods=["GET", "POST", "OPTIONS"])]
    routes.extend(
        [
            Route("/.well-known/agent-card.json", _fleet_agent_card, methods=["GET"]),
            Route("/.well-known/agent.json", _fleet_agent_card, methods=["GET"]),
        ]
    )
    if oauth_state is not None:
        async def resource_metadata(request: Request) -> JSONResponse:
            return oauth_auth.protected_resource_metadata(request, oauth_state)

        async def authorization_server_metadata(request: Request) -> JSONResponse:
            return oauth_auth.authorization_metadata(request, oauth_state)

        async def authorize(request: Request) -> Response:
            return oauth_auth.authorize(request, oauth_state)

        async def token(request: Request) -> JSONResponse:
            return await oauth_auth.token(request, oauth_state)

        routes.extend(
            [
                Route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"]),
                Route("/.well-known/oauth-protected-resource/mcp", resource_metadata, methods=["GET"]),
                Route("/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]),
                Route("/oauth/authorize", authorize, methods=["GET"]),
                Route("/oauth/token", token, methods=["POST"]),
            ]
        )

    routes.extend(_ui_routes(eprint))
    routes.extend(
        operator_live_events.websocket_routes(
            get_hermes_root,
            auth_check=live_websocket_authorized,
        )
    )
    routes.append(Mount("/", app=mcp_app))
    app = Starlette(routes=routes, lifespan=raw_mcp_app.router.lifespan_context)
    issuer = oauth_state.config.issuer if oauth_state is not None else ""
    parsed_issuer = urllib.parse.urlparse(issuer)
    issuer_origin = (
        f"{parsed_issuer.scheme}://{parsed_issuer.netloc}"
        if parsed_issuer.netloc
        else ""
    )
    origins = [origin for origin in ("https://chatgpt.com", issuer_origin) if origin]
    return CORSMiddleware(
        oauth_auth.BearerAuthMiddleware(app, oauth_state, static_token=static_bearer),
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=86400,
    )


def _ui_routes(eprint: Callable[[str], None]) -> list[BaseRoute]:
    """Load optional browser UI routes without making them a server dependency."""
    try:
        import ui_security

        enabled = ui_security.ui_enabled()
    except Exception:  # noqa: BLE001
        enabled = os.environ.get("HERMES_GPT_UI_ENABLED") == "1"
    if not enabled:
        return []
    try:
        import ui_api

        return list(ui_api.routes())
    except Exception as exc:  # noqa: BLE001
        eprint(f"UI mount skipped: {exc.__class__.__name__}: {exc}")
        return []


__all__ = [
    "REMOTE_PROFILE",
    "TRUSTED_PROXY_IPS_ENV",
    "_fleet_agent_card",
    "_fleet_peer_name",
    "_fleet_peer_url",
    "auth_enabled",
    "authenticated_http_security_options",
    "build_asgi_app",
    "health_root",
    "is_loopback_host",
    "oauth_state_from_env",
    "register_fleet_local_card",
    "trusted_proxy_ips_from_env",
]
