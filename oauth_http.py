"""HTTP handlers and ASGI middleware for Hermes GPT OAuth."""

from __future__ import annotations

import base64
import hmac
import urllib.parse
from typing import ClassVar

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from oauth_auth import (
    _PKCE_VALUE,
    MAX_TOKEN_REQUEST_BYTES,
    OAuthError,
    OAuthState,
    static_bearer_from_env,
)


def _error_response(exc: OAuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.error == "invalid_client":
        headers["WWW-Authenticate"] = "Basic realm=oauth-token"
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=exc.status_code,
        headers=headers,
    )


def validate_bearer_token(
    token_value: str,
    state: OAuthState | None,
    *,
    static_token: str | None = None,
) -> bool:
    expected = (
        (static_bearer_from_env() or "") if static_token is None else static_token
    )
    if expected and token_value and hmac.compare_digest(token_value, expected):
        return True
    return bool(state and state.validate_access_token(token_value))


class DefaultMcpAcceptMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            headers = list(scope.get("headers") or [])
            accept_indexes = [
                index
                for index, (key, _value) in enumerate(headers)
                if key.lower() == b"accept"
            ]
            replacement = b"application/json, text/event-stream"
            if not accept_indexes:
                headers.append((b"accept", replacement))
                scope = {**scope, "headers": headers}
            else:
                index = accept_indexes[-1]
                value = headers[index][1].decode("latin-1").strip()
                if not value or value == "*/*":
                    headers[index] = (b"accept", replacement)
                    scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    PUBLIC_PATHS: ClassVar[set[str]] = {
        "/",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        # Clients may probe OIDC discovery even though this server does not implement OIDC.
        "/.well-known/openid-configuration",
        "/oauth/authorize",
        "/oauth/token",
    }

    def __init__(
        self,
        app: ASGIApp,
        state: OAuthState | None = None,
        *,
        static_token: str | None = None,
    ) -> None:
        self.app = app
        self.state = state
        self.static_token = static_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        expected_static = (
            (static_bearer_from_env() or "")
            if self.static_token is None
            else self.static_token
        )
        if not expected_static and self.state is None:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS" or scope.get("path") in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        supplied = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else ""
        )
        if not validate_bearer_token(
            supplied, self.state, static_token=expected_static
        ):
            challenge = "Bearer"
            if self.state is not None:
                metadata = (
                    f"{self.state.config.issuer}/.well-known/oauth-protected-resource"
                )
                challenge = f'Bearer realm="hermes-gpt", resource_metadata="{metadata}"'
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def authorization_metadata(_request: Request, state: OAuthState) -> JSONResponse:
    issuer = state.config.issuer
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": list(state.config.supported_scopes),
        }
    )


def protected_resource_metadata(_request: Request, state: OAuthState) -> JSONResponse:
    return JSONResponse(
        {
            "resource": state.config.resource,
            "authorization_servers": [state.config.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(state.config.supported_scopes),
        }
    )


def _redirect_response(
    redirect_uri: str, values: list[tuple[str, str]]
) -> RedirectResponse:
    parsed = urllib.parse.urlparse(redirect_uri)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    location = urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(existing + values))
    )
    return RedirectResponse(location, status_code=302)


def authorize(request: Request, state: OAuthState) -> JSONResponse | RedirectResponse:
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    client = state.config.client_for_id(client_id)
    if client is None:
        return _error_response(
            OAuthError("invalid_client", "Unknown OAuth client.", status_code=401)
        )
    if redirect_uri not in client.redirect_uris:
        return _error_response(
            OAuthError("invalid_request", "redirect_uri is not registered.")
        )
    try:
        if params.get("response_type", "") != "code":
            raise OAuthError(
                "unsupported_response_type", "Only response_type=code is supported."
            )
        scope = state.normalize_scope(params.get("scope", "") or state.config.scope)
        resource = params.get("resource", "") or state.config.resource
        if resource != state.config.resource:
            raise OAuthError("invalid_target", "Requested resource is not supported.")
        challenge = params.get("code_challenge", "")
        method = params.get("code_challenge_method", "")
        if challenge and (method != "S256" or not _PKCE_VALUE.fullmatch(challenge)):
            raise OAuthError(
                "invalid_request", "Only a valid S256 PKCE challenge is supported."
            )
        if method and not challenge:
            raise OAuthError(
                "invalid_request",
                "code_challenge is required when a method is supplied.",
            )
        code = state.issue_authorization_code(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            resource=resource,
            code_challenge=challenge,
        )
    except OAuthError as exc:
        error_query = [("error", exc.error), ("error_description", exc.description)]
        if params.get("state"):
            error_query.append(("state", params["state"]))
        return _redirect_response(redirect_uri, error_query)
    query = [("code", code)]
    if params.get("state"):
        query.append(("state", params["state"]))
    return _redirect_response(redirect_uri, query)


def _form_value(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name) or []
    return values[0] if values else ""


def _client_credentials(
    request: Request, form: dict[str, list[str]]
) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(
                authorization.split(" ", 1)[1], validate=True
            ).decode("utf-8")
        except Exception as exc:
            raise OAuthError(
                "invalid_client", "Invalid OAuth client credentials.", status_code=401
            ) from exc
        client_id, separator, client_secret = decoded.partition(":")
        if not separator:
            raise OAuthError(
                "invalid_client", "Invalid OAuth client credentials.", status_code=401
            )
        return urllib.parse.unquote(client_id), urllib.parse.unquote(client_secret)
    return _form_value(form, "client_id"), _form_value(form, "client_secret")


def _authenticate_client(
    request: Request, form: dict[str, list[str]], state: OAuthState
) -> str:
    client_id, client_secret = _client_credentials(request, form)
    client = state.config.client_for_id(client_id)
    if client is None or not hmac.compare_digest(client_secret, client.client_secret):
        raise OAuthError(
            "invalid_client", "Invalid OAuth client credentials.", status_code=401
        )
    return client.client_id


async def token(request: Request, state: OAuthState) -> JSONResponse:
    try:
        content_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/x-www-form-urlencoded":
            raise OAuthError(
                "invalid_request", "Token requests must use form encoding."
            )
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > MAX_TOKEN_REQUEST_BYTES:
                    raise OAuthError("invalid_request", "Token request is too large.")
            except ValueError as exc:
                raise OAuthError(
                    "invalid_request", "Invalid Content-Length header."
                ) from exc
        buffered = bytearray()
        async for chunk in request.stream():
            if len(buffered) + len(chunk) > MAX_TOKEN_REQUEST_BYTES:
                raise OAuthError("invalid_request", "Token request is too large.")
            buffered.extend(chunk)
        body = bytes(buffered).decode("utf-8")
        try:
            form = urllib.parse.parse_qs(
                body, keep_blank_values=True, max_num_fields=32
            )
        except ValueError as exc:
            raise OAuthError(
                "invalid_request", "Token request form is invalid."
            ) from exc
        grant_type = _form_value(form, "grant_type")
        if not grant_type:
            raise OAuthError("invalid_request", "grant_type is required.")
        if grant_type not in {"authorization_code", "refresh_token"}:
            raise OAuthError(
                "unsupported_grant_type", "The requested grant type is not supported."
            )
        client_id = _authenticate_client(request, form, state)
        if grant_type == "authorization_code":
            response = state.exchange_authorization_code(
                code=_form_value(form, "code"),
                client_id=client_id,
                redirect_uri=_form_value(form, "redirect_uri"),
                code_verifier=_form_value(form, "code_verifier"),
            )
        else:
            refresh_token = _form_value(form, "refresh_token")
            if not refresh_token:
                raise OAuthError("invalid_request", "refresh_token is required.")
            response = state.exchange_refresh_token(
                refresh_token=refresh_token,
                client_id=client_id,
                requested_scope=_form_value(form, "scope"),
            )
        return JSONResponse(
            response, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
    except UnicodeDecodeError:
        return _error_response(
            OAuthError("invalid_request", "Token request is malformed.")
        )
    except OAuthError as exc:
        return _error_response(exc)
    except Exception:  # noqa: BLE001
        # Durable persistence failures must not escape after a code is consumed.
        return _error_response(
            OAuthError(
                "temporarily_unavailable",
                "The authorization server could not persist the token.",
                status_code=503,
            )
        )
