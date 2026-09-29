"""Validate OAuth clients and load connector authentication settings."""

from __future__ import annotations

import hmac
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

AUTH_TOKEN_ENV = "HERMES_GPT_BEARER_TOKEN"
OAUTH_ENABLE_ENV = "HERMES_GPT_OAUTH_ENABLE"
OAUTH_ISSUER_ENV = "HERMES_GPT_OAUTH_ISSUER"
OAUTH_CLIENT_ID_ENV = "HERMES_GPT_OAUTH_CLIENT_ID"
OAUTH_CLIENT_SECRET_ENV = "HERMES_GPT_OAUTH_CLIENT_SECRET"
OAUTH_REDIRECT_URI_ENV = "HERMES_GPT_OAUTH_REDIRECT_URI"
OAUTH_SCOPE_ENV = "HERMES_GPT_OAUTH_SCOPE"
GEMINI_ENABLE_ENV = "HERMES_GPT_OAUTH_GEMINI_ENABLE"
GEMINI_CLIENT_ID_ENV = "HERMES_GPT_OAUTH_GEMINI_CLIENT_ID"
GEMINI_CLIENT_SECRET_ENV = "HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET"
GEMINI_REDIRECT_URI_ENV = "HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI"
_CLIENT_SECRET = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


@dataclass(frozen=True)
class OAuthClient:
    """One registered confidential OAuth client.

    Hermes GPT has no dynamic client registration; every client is an
    operator-provisioned entry with its own secret and its own exact-match
    redirect-URI allowlist. Additional clients (for example the opt-in Gemini
    Spark client profile) stay isolated from the primary client: a client can
    only redirect to, or authenticate with, its own credentials.
    """

    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise ValueError("OAuth client_id is required.")
        if not _CLIENT_SECRET.fullmatch(self.client_secret):
            raise ValueError(
                "OAuth client_secret must contain 43 to 128 URL-safe characters."
            )
        if not self.redirect_uris:
            raise ValueError("At least one OAuth redirect URI is required.")
        for redirect_uri in self.redirect_uris:
            redirect = urllib.parse.urlparse(redirect_uri)
            if (
                redirect.scheme != "https"
                or not redirect.netloc
                or not redirect.hostname
                or redirect.fragment
                or redirect.username is not None
                or redirect.password is not None
            ):
                raise ValueError(
                    "OAuth redirect URIs must be absolute HTTPS URLs without userinfo or fragments."
                )
        object.__setattr__(self, "client_id", self.client_id.strip())
        object.__setattr__(
            self, "redirect_uris", tuple(dict.fromkeys(self.redirect_uris))
        )


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    scope: str = "hermes"
    additional_clients: tuple[OAuthClient, ...] = ()
    # Derived, primary-client-first registry. Additive clients never replace
    # or weaken the primary client's credentials or redirect allowlist.
    clients: tuple[OAuthClient, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        issuer = self.issuer.rstrip("/")
        parsed = urllib.parse.urlparse(issuer)
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("OAuth issuer must use HTTPS except on loopback.")
        if (
            not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "OAuth issuer must be an origin URL without path, userinfo, query, or fragment."
            )
        primary = OAuthClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uris=tuple(self.redirect_uris),
        )
        clients = (primary,) + tuple(self.additional_clients)
        seen: set[str] = set()
        for client in clients:
            if client.client_id in seen:
                raise ValueError(
                    "OAuth client_id values must be unique across registered clients."
                )
            seen.add(client.client_id)
        if not self.scope.strip() or len(self.scope.split()) != 1:
            raise ValueError("OAuth scope must be one non-empty scope token.")
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "client_id", primary.client_id)
        object.__setattr__(self, "client_secret", primary.client_secret)
        object.__setattr__(self, "redirect_uris", primary.redirect_uris)
        object.__setattr__(self, "clients", clients)
        object.__setattr__(self, "scope", self.scope.strip())

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    @property
    def supported_scopes(self) -> tuple[str, ...]:
        # ChatGPT currently adds `openid` even when its OIDC toggle is disabled.
        # It is accepted as a compatibility scope; this server does not advertise
        # OpenID Provider metadata or issue ID tokens.
        return tuple(dict.fromkeys((self.scope, "openid", "offline_access")))

    def client_for_id(self, client_id: str) -> OAuthClient | None:
        """Return the registered client with this exact id, or ``None``."""
        for client in self.clients:
            if hmac.compare_digest(client.client_id, client_id):
                return client
        return None

    def client_registered(self, client_id: Any) -> bool:
        """True when ``client_id`` identifies a registered client.

        Tokens that predate additional clients carry no ``client_id``; they are
        treated as the primary client so existing deployments keep validating.
        """
        if not isinstance(client_id, str) or not client_id:
            client_id = self.clients[0].client_id
        return self.client_for_id(client_id) is not None


def config_from_env() -> OAuthConfig | None:
    if os.environ.get(OAUTH_ENABLE_ENV) != "1":
        return None
    required = {
        OAUTH_ISSUER_ENV: os.environ.get(OAUTH_ISSUER_ENV, "").strip(),
        OAUTH_CLIENT_ID_ENV: os.environ.get(OAUTH_CLIENT_ID_ENV, "").strip(),
        OAUTH_CLIENT_SECRET_ENV: os.environ.get(OAUTH_CLIENT_SECRET_ENV, ""),
        OAUTH_REDIRECT_URI_ENV: os.environ.get(OAUTH_REDIRECT_URI_ENV, "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            f"OAuth is enabled but required configuration is missing: {', '.join(missing)}"
        )
    redirects = tuple(
        item.strip()
        for item in required[OAUTH_REDIRECT_URI_ENV].replace("\n", ",").split(",")
        if item.strip()
    )
    additional: list[OAuthClient] = []
    gemini = gemini_client_from_env()
    if gemini is not None:
        additional.append(gemini)
    return OAuthConfig(
        issuer=required[OAUTH_ISSUER_ENV],
        client_id=required[OAUTH_CLIENT_ID_ENV],
        client_secret=required[OAUTH_CLIENT_SECRET_ENV],
        redirect_uris=redirects,
        scope=os.environ.get(OAUTH_SCOPE_ENV, "hermes").strip() or "hermes",
        additional_clients=tuple(additional),
    )


def gemini_client_from_env() -> OAuthClient | None:
    """Opt-in Gemini Spark client profile (additional registered client).

    Google's consumer "Custom apps for Spark" flow completes as a manually
    configured confidential client against a server that advertises no dynamic
    registration endpoint. When enabled, this profile is a fully isolated
    registered client with its own secret and its own exact-match redirect-URI
    allowlist; the primary (for example ChatGPT) client is untouched.
    """
    if os.environ.get(GEMINI_ENABLE_ENV) != "1":
        return None
    required = {
        GEMINI_CLIENT_ID_ENV: os.environ.get(GEMINI_CLIENT_ID_ENV, "").strip(),
        GEMINI_CLIENT_SECRET_ENV: os.environ.get(GEMINI_CLIENT_SECRET_ENV, ""),
        GEMINI_REDIRECT_URI_ENV: os.environ.get(GEMINI_REDIRECT_URI_ENV, "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            "The Gemini Spark client profile is enabled but required configuration is missing: "
            + ", ".join(missing)
        )
    redirects = tuple(
        item.strip()
        for item in required[GEMINI_REDIRECT_URI_ENV].replace("\n", ",").split(",")
        if item.strip()
    )
    try:
        return OAuthClient(
            client_id=required[GEMINI_CLIENT_ID_ENV],
            client_secret=required[GEMINI_CLIENT_SECRET_ENV],
            redirect_uris=redirects,
        )
    except ValueError as exc:
        raise ValueError(f"Gemini Spark client profile: {exc}") from exc


def static_bearer_from_env() -> str | None:
    token_value = os.environ.get(AUTH_TOKEN_ENV, "")
    if not token_value:
        return None
    if not _CLIENT_SECRET.fullmatch(token_value):
        raise ValueError(
            f"{AUTH_TOKEN_ENV} must contain 43 to 128 URL-safe characters."
        )
    return token_value
