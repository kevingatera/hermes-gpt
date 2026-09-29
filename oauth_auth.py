from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

import oauth_config as _oauth_config

# Keep the established oauth_auth imports available to server and client code.
AUTH_TOKEN_ENV = _oauth_config.AUTH_TOKEN_ENV
OAUTH_ENABLE_ENV = _oauth_config.OAUTH_ENABLE_ENV
OAUTH_ISSUER_ENV = _oauth_config.OAUTH_ISSUER_ENV
OAUTH_CLIENT_ID_ENV = _oauth_config.OAUTH_CLIENT_ID_ENV
OAUTH_CLIENT_SECRET_ENV = _oauth_config.OAUTH_CLIENT_SECRET_ENV
OAUTH_REDIRECT_URI_ENV = _oauth_config.OAUTH_REDIRECT_URI_ENV
OAUTH_SCOPE_ENV = _oauth_config.OAUTH_SCOPE_ENV
GEMINI_ENABLE_ENV = _oauth_config.GEMINI_ENABLE_ENV
GEMINI_CLIENT_ID_ENV = _oauth_config.GEMINI_CLIENT_ID_ENV
GEMINI_CLIENT_SECRET_ENV = _oauth_config.GEMINI_CLIENT_SECRET_ENV
GEMINI_REDIRECT_URI_ENV = _oauth_config.GEMINI_REDIRECT_URI_ENV
OAuthClient = _oauth_config.OAuthClient
OAuthConfig = _oauth_config.OAuthConfig
config_from_env = _oauth_config.config_from_env
gemini_client_from_env = _oauth_config.gemini_client_from_env
static_bearer_from_env = _oauth_config.static_bearer_from_env

AUTH_CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_AUTH_CODES = 1024
MAX_ACCESS_TOKENS = 4096
MAX_REFRESH_TOKENS = 4096
MAX_TOKEN_REQUEST_BYTES = 16384
_PKCE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _valid_pkce_verifier(verifier: str) -> bool:
    return bool(_PKCE_VALUE.fullmatch(verifier))


# Optional persistence hook (v0.7 S5). server.py installs it so every token
# issuance/refresh persists through token_store without oauth_auth depending
# on a concrete hermes_root. Never raises; token material never logged.
_persist_hook: Any | None = None
# Optional revocation hook. server.py installs it so a durable revocation
# (hermes_oauth_revoke) also drops the live process's in-memory token caches;
# otherwise the next issuance would re-persist pre-revocation tokens through
# the persist hook and resurrect them in the durable store.
_revocation_hook: Any | None = None


def set_revocation_hook(hook: Any | None) -> None:
    """Install (or clear) the durable-revocation notification hook."""
    global _revocation_hook
    _revocation_hook = hook


def run_revocation_hook() -> None:
    """Notify installed hooks that the durable token store was revoked."""
    if _revocation_hook is None:
        return
    try:
        _revocation_hook()
    except Exception:  # noqa: BLE001, S110 - revocation must succeed even if notification hooks fail.
        # Revocation notification must never break the revoke path.
        pass


def set_persist_hook(hook: Any | None) -> None:
    """Install (or clear) the durable-token persistence hook.

    The hook receives ``(state, kind)`` where kind is one of
    ``authorization_code`` | ``refresh`` and returns a bounded summary.
    """
    global _persist_hook
    _persist_hook = hook


def _run_persist_hook(state: OAuthState, kind: str) -> None:
    if _persist_hook is None:
        return
    try:
        _persist_hook(state, kind)
    except Exception:  # noqa: BLE001, S110 - best-effort persistence cannot break token exchange.
        # Persistence must never break the token exchange path.
        pass


def _run_persist_hook_strict(state: OAuthState, kind: str) -> None:
    """Persist or fail the exchange.

    Durable persistence is part of the exchange contract in server mode:
    returning credentials that were never persisted would hand the client
    tokens that die on the next validation. When no store is bound the call
    is a no-op (pure in-memory mode).
    """
    if _persist_hook is None:
        return
    _persist_hook(state, kind)


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


ACCESS_TOKEN_PREFIX = "hg.at.v1."
_ACCESS_TOKEN_MAC_CONTEXT = b"hermes-gpt.oauth.access.v1\0"


def _durable_record(kind: str, value: str, item: dict[str, Any]) -> dict[str, Any]:
    """Token item carrying the internal markers the store needs to file it."""
    record = dict(item)
    record["_kind"] = kind
    record["_token_value"] = value
    return record


class OAuthError(RuntimeError):
    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


class OAuthState:
    def __init__(
        self,
        config: OAuthConfig,
        *,
        max_auth_codes: int = MAX_AUTH_CODES,
        max_access_tokens: int = MAX_ACCESS_TOKENS,
        max_refresh_tokens: int = MAX_REFRESH_TOKENS,
    ) -> None:
        self.config = config
        self.max_auth_codes = max_auth_codes
        self.max_access_tokens = max_access_tokens
        self.max_refresh_tokens = max_refresh_tokens
        self._authorization_code_key = secrets.token_bytes(32)
        self.used_auth_codes: dict[str, dict[str, Any]] = {}
        self.access_tokens: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        # Bound by restore_tokens()/persist_tokens() in server mode. When set,
        # the durable store is authoritative for bearer validity so revocation
        # cannot be bypassed by the clustered signed-token fallback.
        self._hermes_root: Path | None = None
        # Durable revocation epoch this process's in-memory caches were built
        # under. A peer that misses a revocation event detects the mismatch
        # from this value and refuses to re-persist stale tokens.
        self._epoch: int = 0
        # Refresh tokens consumed/rotated since the last durable persist.
        # Because persistence merges rather than replaces, removals must be
        # carried explicitly or a rotated token would remain durable and
        # become replayable after a restart.
        self._retired_refresh_tokens: set[str] = set()

    def cleanup(self) -> None:
        now = time.time()
        for store in (self.used_auth_codes, self.access_tokens, self.refresh_tokens):
            for credential, item in list(store.items()):
                if item.get("expires_at", 0) <= now:
                    store.pop(credential, None)

    def normalize_scope(self, scope: str) -> str:
        requested = list(dict.fromkeys(scope.split()))
        if (
            not requested
            or self.config.scope not in requested
            or not set(requested).issubset(self.config.supported_scopes)
        ):
            raise OAuthError("invalid_scope", "Requested scope is not supported.")
        return " ".join(requested)

    def _require_capacity(
        self, store: dict[str, Any], maximum: int, credential_type: str
    ) -> None:
        self.cleanup()
        if len(store) >= maximum:
            raise OAuthError(
                "temporarily_unavailable",
                f"{credential_type} capacity is temporarily unavailable.",
                status_code=503,
            )

    def issue_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scope: str,
        resource: str,
        code_challenge: str,
    ) -> str:
        import token_store as _ts

        issuance_epoch = 0
        if self._hermes_root is not None:
            try:
                issuance_epoch = _ts.read_revocation_epoch(self._hermes_root)
            except Exception:  # noqa: BLE001 - A missing epoch is treated as zero and fenced on commit.
                issuance_epoch = 0
        payload = {
            "v": 2,
            "nonce": secrets.token_urlsafe(24),
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "resource": resource,
            "code_challenge": code_challenge,
            "expires_at": int(time.time()) + AUTH_CODE_TTL_SECONDS,
            "epoch": issuance_epoch,
        }
        encoded = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._authorization_code_key, encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded}.{_base64url_encode(signature)}"

    def _decode_authorization_code(self, code: str) -> dict[str, Any]:
        if len(code) > 4096:
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            )
        encoded, separator, encoded_signature = code.partition(".")
        if not separator:
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            )
        try:
            supplied_signature = _base64url_decode(encoded_signature)
            expected_signature = hmac.new(
                self._authorization_code_key,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(_base64url_decode(encoded))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            ) from exc
        required_types = {
            "v": int,
            "nonce": str,
            "client_id": str,
            "redirect_uri": str,
            "scope": str,
            "resource": str,
            "code_challenge": str,
            "expires_at": int,
        }
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), kind)
            for key, kind in required_types.items()
        ):
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            )
        if payload["v"] not in (1, 2) or not _NONCE.fullmatch(payload["nonce"]):
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            )
        # v2 codes are bound to the revocation epoch they were issued under;
        # a revocation since issuance invalidates every outstanding code.
        if payload["v"] == 2:
            epoch = payload.get("epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                raise OAuthError(
                    "invalid_grant",
                    "Invalid, expired, or already used authorization code.",
                )
            if self._hermes_root is not None:
                import token_store as _ts

                try:
                    current_epoch = _ts.read_revocation_epoch(self._hermes_root)
                except Exception:  # noqa: BLE001 - Reject codes if the revocation epoch is unreadable.
                    raise OAuthError(
                        "invalid_grant",
                        "Invalid, expired, or already used authorization code.",
                    )
                if epoch < current_epoch:
                    raise OAuthError(
                        "invalid_grant",
                        "Invalid, expired, or already used authorization code.",
                    )
        return payload

    def _access_token_key(self) -> bytes:
        """Derive the clustered access-token HMAC key from the shared client secret.

        Clustered origins (the same public MCP hostname served by more than one
        process) share ``HERMES_GPT_OAUTH_CLIENT_SECRET`` but not process memory.
        Opaque ``token_urlsafe`` access tokens therefore 401 on the origin that
        did not issue them. HMAC-SHA256 over a versioned payload lets any origin
        with the same confidential client secret validate the bearer without a
        shared token table. Rotating the client secret invalidates every signed
        access token.
        """
        return hashlib.sha256(
            _ACCESS_TOKEN_MAC_CONTEXT + self.config.client_secret.encode("utf-8")
        ).digest()

    def _new_access_token(
        self, *, client_id: str, scope: str, resource: str
    ) -> tuple[str, dict[str, Any]]:
        expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS
        payload = {
            "v": 1,
            "typ": "access",
            "nonce": secrets.token_urlsafe(24),
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": expires_at,
        }
        encoded = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._access_token_key(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        token_value = f"{ACCESS_TOKEN_PREFIX}{encoded}.{_base64url_encode(signature)}"
        item = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": float(expires_at),
        }
        return token_value, item

    def _decode_signed_access_token(self, token_value: str) -> dict[str, Any] | None:
        if not token_value.startswith(ACCESS_TOKEN_PREFIX) or len(token_value) > 4096:
            return None
        encoded, separator, encoded_signature = token_value[
            len(ACCESS_TOKEN_PREFIX) :
        ].partition(".")
        if not separator:
            return None
        try:
            supplied_signature = _base64url_decode(encoded_signature)
            expected_signature = hmac.new(
                self._access_token_key(),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(_base64url_decode(encoded))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None
        required_types = {
            "v": int,
            "typ": str,
            "nonce": str,
            "client_id": str,
            "scope": str,
            "resource": str,
            "expires_at": int,
        }
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), kind)
            for key, kind in required_types.items()
        ):
            return None
        if (
            payload["v"] not in (1, 2)
            or payload["typ"] != "access"
            or not _NONCE.fullmatch(payload["nonce"])
        ):
            return None
        if payload["expires_at"] <= time.time():
            return None
        if payload[
            "resource"
        ] != self.config.resource or not self.config.client_registered(
            payload["client_id"]
        ):
            return None
        # v2 codes are bound to the revocation epoch they were issued under;
        # a revocation since issuance invalidates every outstanding code.
        if payload["v"] == 2 and self._hermes_root is not None:
            import token_store as _ts

            try:
                current_epoch = _ts.read_revocation_epoch(self._hermes_root)
            except Exception:  # noqa: BLE001 - Fail closed if the durable epoch cannot be read.
                return None  # unreadable store: fail closed
            if int(payload.get("epoch", 0)) < current_epoch:
                return None
        try:
            self.normalize_scope(payload["scope"])
        except OAuthError:
            return None
        return payload

    def _new_refresh_token(
        self, *, client_id: str, scope: str
    ) -> tuple[str, dict[str, Any]]:
        token_value = secrets.token_urlsafe(48)
        item = {
            "client_id": client_id,
            "scope": scope,
            "expires_at": time.time() + REFRESH_TOKEN_TTL_SECONDS,
        }
        return token_value, item

    def _sync_epoch_for_fresh_grant(self) -> None:
        """Adopt the current durable epoch when holding no pre-revocation tokens.

        A fresh grant (authorization-code exchange) mints NEW credentials that
        must be persistable even right after a revocation. When this process
        holds no live access/refresh tokens, its epoch can only be stale by
        revocation — nothing here needs fencing — so adopting the current
        epoch is safe and unblocks the persist hook. When live tokens ARE
        held, keep the stricter epoch so a fenced persist still discards them.
        """
        if self._hermes_root is None:
            return
        if self.access_tokens or self.refresh_tokens:
            return
        self._adopt_current_epoch()

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        self.cleanup()
        self._sync_epoch_for_fresh_grant()
        item = self._decode_authorization_code(code)
        nonce = item["nonce"]
        if nonce in self.used_auth_codes or item.get("expires_at", 0) <= time.time():
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used authorization code."
            )
        if (
            item.get("client_id") != client_id
            or item.get("redirect_uri") != redirect_uri
        ):
            raise OAuthError("invalid_grant", "Authorization code validation failed.")
        challenge = item.get("code_challenge", "")
        if challenge and (
            not _valid_pkce_verifier(code_verifier)
            or not hmac.compare_digest(_s256(code_verifier), challenge)
        ):
            raise OAuthError("invalid_grant", "Authorization code validation failed.")

        scope = self.normalize_scope(item["scope"])
        self._require_capacity(
            self.used_auth_codes, self.max_auth_codes, "Authorization-code replay cache"
        )
        self._require_capacity(
            self.access_tokens, self.max_access_tokens, "Access-token"
        )
        if "offline_access" in scope.split():
            self._require_capacity(
                self.refresh_tokens, self.max_refresh_tokens, "Refresh-token"
            )

        access_value, access_item = self._new_access_token(
            client_id=client_id,
            scope=scope,
            resource=item["resource"],
        )
        response: dict[str, Any] = {
            "access_token": access_value,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "scope": scope,
        }
        refresh_value = ""
        refresh_item: dict[str, Any] | None = None
        if "offline_access" in scope.split():
            refresh_value, refresh_item = self._new_refresh_token(
                client_id=client_id, scope=scope
            )
            response["refresh_token"] = refresh_value

        self.used_auth_codes[nonce] = {"expires_at": item["expires_at"]}
        self.access_tokens[access_value] = access_item
        if refresh_item is not None:
            self.refresh_tokens[refresh_value] = refresh_item
        # Durable persistence is part of the exchange contract in server
        # mode: never hand out credentials that were not durably committed.
        try:
            _run_persist_hook_strict(self, "authorization_code")
        except OAuthError:
            self.access_tokens.pop(access_value, None)
            if refresh_item is not None:
                self.refresh_tokens.pop(refresh_value, None)
            raise
        except Exception as exc:
            self.access_tokens.pop(access_value, None)
            if refresh_item is not None:
                self.refresh_tokens.pop(refresh_value, None)
            raise OAuthError(
                "temporarily_unavailable",
                "Token persistence failed; no credentials were issued.",
                status_code=503,
            ) from exc
        return response

    def validate_refresh_token_grant(
        self, refresh_token: str, client_id: str
    ) -> dict[str, Any]:
        """Validate a refresh grant against the authoritative durable envelope.

        In server mode (``_hermes_root`` bound) the durable store is the
        revocation authority for refresh tokens exactly as for access tokens:
        a refresh token that is not currently present in the durable envelope
        is rejected, so an owner revocation cannot be outlived by a copy held
        in process memory. Returns the validated item.
        """
        item = self.refresh_tokens.get(refresh_token)
        if not item or item.get("expires_at", 0) <= time.time():
            self.refresh_tokens.pop(refresh_token, None)
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used refresh token."
            )
        if item.get("client_id") != client_id:
            raise OAuthError("invalid_grant", "Refresh token validation failed.")
        if self._hermes_root is None:
            return item
        durable_item: Any = None
        try:
            import token_store

            durable_item = token_store.lookup_token(
                self._hermes_root, "refresh", refresh_token
            )
        except Exception:  # noqa: BLE001 - Treat lookup errors as absent durable credentials.
            durable_item = None
        if not (
            isinstance(durable_item, dict)
            and durable_item.get("expires_at", 0) > time.time()
            and durable_item.get("client_id") == client_id
        ):
            self.refresh_tokens.pop(refresh_token, None)
            raise OAuthError(
                "invalid_grant", "Invalid, expired, or already used refresh token."
            )
        return item

    def exchange_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        requested_scope: str,
    ) -> dict[str, Any]:
        self.cleanup()
        item = self.validate_refresh_token_grant(refresh_token, client_id)
        original_scope = self.normalize_scope(item["scope"])
        scope = (
            self.normalize_scope(requested_scope)
            if requested_scope.strip()
            else original_scope
        )
        if not set(scope.split()).issubset(original_scope.split()):
            raise OAuthError(
                "invalid_scope", "Requested scope exceeds the originally granted scope."
            )

        self._require_capacity(
            self.access_tokens, self.max_access_tokens, "Access-token"
        )
        access_value, access_item = self._new_access_token(
            client_id=client_id,
            scope=scope,
            resource=self.config.resource,
        )
        rotated_value, rotated_item = self._new_refresh_token(
            client_id=client_id, scope=scope
        )

        if self._hermes_root is not None:
            # Atomic consume+issue: the presented refresh token is retired
            # and its replacements published in ONE transaction, so racing
            # peers cannot both spend the same token.
            import token_store

            try:
                token_store.exchange_commit(
                    self._hermes_root,
                    source_epoch=self._epoch,
                    presented_kind="refresh",
                    presented_value=refresh_token,
                    issue={
                        token_store.issue_key("access", access_value): _durable_record(
                            "access", access_value, access_item
                        ),
                        token_store.issue_key(
                            "refresh", rotated_value
                        ): _durable_record("refresh", rotated_value, rotated_item),
                    },
                )
            except token_store.TokenStoreError as exc:
                # Revoked/stale/spent: the exchange fails without publishing.
                self.refresh_tokens.pop(refresh_token, None)
                self.access_tokens.pop(access_value, None)
                self._sync_epoch_for_fresh_grant()
                raise OAuthError(
                    "invalid_grant",
                    "Refresh token could not be durably exchanged.",
                ) from exc
        self.refresh_tokens.pop(refresh_token, None)
        self._retired_refresh_tokens.add(refresh_token)
        self.refresh_tokens[rotated_value] = rotated_item
        self.access_tokens[access_value] = access_item
        return {
            "access_token": access_value,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": rotated_value,
            "scope": scope,
        }

    def _durable_access_token_valid(self, token_value: str) -> bool:
        """Validate bearer presence against the authoritative durable envelope.

        A clustered peer may not have the token in process memory, so a cache
        miss is resolved by reading the shared durable store. Conversely, once
        revocation removes that envelope, an already-cached token is rejected
        immediately instead of being resurrected solely from its MAC.
        """
        if self._hermes_root is None:
            item = self.access_tokens.get(token_value)
            return bool(
                item
                and item.get("expires_at", 0) > time.time()
                and item.get("resource") == self.config.resource
                and self.config.client_registered(item.get("client_id"))
            )
        try:
            import token_store

            item = token_store.lookup_token(self._hermes_root, "access", token_value)
        except Exception:  # noqa: BLE001 - Reject cached tokens when the store cannot verify them.
            self.access_tokens.pop(token_value, None)
            return False
        if not (
            isinstance(item, dict)
            and item.get("expires_at", 0) > time.time()
            and item.get("resource") == self.config.resource
            and self.config.client_registered(item.get("client_id"))
        ):
            self.access_tokens.pop(token_value, None)
            return False
        self.access_tokens[token_value] = item
        return True

    def validate_access_token(self, token_value: str) -> bool:
        if not token_value:
            return False
        self.cleanup()

        # In server mode the encrypted durable envelope is the revocation
        # authority for both legacy opaque and v1 signed tokens. Signed tokens
        # still require a valid MAC, but a MAC alone is never enough.
        if self._hermes_root is not None:
            if (
                token_value.startswith(ACCESS_TOKEN_PREFIX)
                and self._decode_signed_access_token(token_value) is None
            ):
                return False
            return self._durable_access_token_valid(token_value)

        # Standalone/in-memory OAuthState instances have no durable authority.
        # Preserve their local validation behavior for tests and embedded use.
        item = self.access_tokens.get(token_value)
        if (
            item
            and item.get("expires_at", 0) > time.time()
            and item.get("resource") == self.config.resource
            and self.config.client_registered(item.get("client_id"))
        ):
            return True
        return self._decode_signed_access_token(token_value) is not None

    # ------------------------------------------------------------------
    # Durable token persistence (v0.7 S5, ADR-001). Tokens are persisted
    # through token_store (AES-256-GCM envelope); no token material is
    # ever written to the audit log or returned on surfaces.
    # ------------------------------------------------------------------

    def clear_live_tokens(self) -> None:
        """Drop live bearer/refresh caches after a durable revocation.

        Used-code replay state is deliberately RETAINED: clearing it would let
        an already-exchanged authorization code be exchanged again (fresh
        credentials minted immediately after revocation) until its five-minute
        expiry. Rotating the authorization-code key below additionally
        invalidates every outstanding (unexchanged) code.
        """
        self.access_tokens.clear()
        self.refresh_tokens.clear()
        self._authorization_code_key = secrets.token_bytes(32)

    def _adopt_current_epoch(self) -> None:
        """Re-sync this process's view to the durable revocation epoch.

        Called after stale credentials were discarded because a commit was
        fenced off: with the stale caches dropped, adopting the current epoch
        lets FRESH issuance persist normally instead of being refused forever.
        """
        if self._hermes_root is None:
            return
        try:
            import token_store

            self._epoch = token_store.read_revocation_epoch(self._hermes_root)
        except Exception:  # noqa: BLE001, S110 - Keep the old epoch if refresh is unavailable.
            pass

    def persist_tokens(self, hermes_root: Path | None = None) -> dict[str, Any]:
        """Commit current live tokens to the shared durable store.

        Issuance and retirement commit as one locked, epoch-fenced
        transaction covering both the encrypted envelope (raw values) and
        the plaintext hash ledger (liveness/retirement). Other processes'
        tokens are never replaced, and retirement is permanent, so a stale
        peer cache can never resurrect a rotated or revoked token.
        """
        import token_store

        if not hermes_root:
            hermes_root = Path.home() / ".hermes"
        self._hermes_root = Path(hermes_root)
        now = time.time()
        issue: dict[str, dict[str, Any]] = {}
        for value, item in self.access_tokens.items():
            if item.get("expires_at", 0) > now:
                issue[token_store.issue_key("access", value)] = _durable_record(
                    "access", value, item
                )
        for value, item in self.refresh_tokens.items():
            if item.get("expires_at", 0) > now:
                issue[token_store.issue_key("refresh", value)] = _durable_record(
                    "refresh", value, item
                )
        retire: dict[str, list[str]] = {}
        if self._retired_refresh_tokens:
            retire["refresh"] = list(self._retired_refresh_tokens)
        try:
            result = token_store.commit_tokens(
                hermes_root,
                source_epoch=self._epoch,
                issue=issue,
                retire=retire,
            )
        except token_store.TokenStoreError:
            # A revocation fenced this commit off. Drop the stale live
            # caches, re-sync the epoch so fresh issuance can persist, and
            # surface the failure.
            self.clear_live_tokens()
            self._retired_refresh_tokens.clear()
            self._adopt_current_epoch()
            raise
        self._epoch = int(result.get("epoch", self._epoch))
        self._retired_refresh_tokens.clear()
        return result

    def restore_tokens(self, hermes_root: Path | None = None) -> dict[str, Any]:
        """Load + decrypt persisted tokens into the in-memory stores.

        Returns a bounded summary; never exposes token material.
        """
        import token_store

        if not hermes_root:
            hermes_root = Path.home() / ".hermes"
        self._hermes_root = Path(hermes_root)
        # Complete the legacy -> SQLite migration BEFORE loading, so an
        # upgrade restores existing credentials instead of seeing an empty
        # store. migrate_store imports the legacy revocation epoch
        # faithfully (migration is not credential issuance, so the grant
        # fence does not apply); it is idempotent and marker-closed.
        try:
            token_store.migrate_store(hermes_root)
        except token_store.TokenStoreError:
            pass  # corrupt/unmigratable store: fail closed below
        self._epoch = token_store.read_revocation_epoch(hermes_root)
        bundle = token_store.load_live_tokens(hermes_root)
        if not bundle:
            return {"restored": 0, "present": False}
        restored = 0
        for kind, store in (
            ("access_tokens", self.access_tokens),
            ("refresh_tokens", self.refresh_tokens),
        ):
            for value, item in (bundle.get(kind) or {}).items():
                if isinstance(item, dict) and item.get("expires_at", 0) > time.time():
                    store[value] = item
                    restored += 1
        return {"restored": restored, "present": True}
