# OAuth and bearer authentication

Hermes GPT remains local-first. Remote Operator or Owner access is supported only when the HTTP endpoint is carried over HTTPS and protected with either a static bearer token or the built-in confidential-client OAuth boundary described here.

## Security model

The built-in authorization server is intentionally narrow:

- one statically configured confidential client by default, plus optional additional named client profiles (see [Additional clients](#additional-clients-gemini-spark-profile));
- exact HTTPS redirect-URI allowlisting;
- client authentication on every authorization-code and refresh exchange;
- optional PKCE S256 validation when a client supplies a challenge;
- one configured Hermes resource scope (required on every issued token) plus the connector compatibility scopes `openid` and `offline_access`;
- one-hour HMAC-signed access tokens (verifiable by any origin that shares the confidential client secret);
- 30-day refresh tokens with rotation and replay rejection;
- signed, five-minute stateless authorization codes plus bounded process-memory replay, access-token, and refresh-token stores;
- no dynamic client registration, user accounts, persistent plaintext token database, or OpenID Provider claims.

`openid` is accepted because ChatGPT may add it even with OIDC disabled. Hermes GPT does not advertise OpenID Provider metadata and does not issue ID tokens; `/.well-known/openid-configuration` is served as a public 404 (not an auth challenge) so clients that probe OIDC discovery with OIDC disabled do not mistake the connector for disconnected. Configure the ChatGPT connector with OIDC disabled.

The client secret is the credential that prevents an arbitrary network caller from exchanging an authorization code. Public clients using token endpoint authentication method `none` are not supported. Do not expose an OAuth-enabled endpoint until a strong client secret is configured.

## Generate a client secret

Generate a fresh URL-safe secret. Do not reuse a Hermes, GitHub, gateway, or provider credential.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The result must contain 43 to 128 URL-safe characters. Store it in a service-owned secret environment source, not in the repository, shell history, documentation, or command-line arguments.

## Required OAuth configuration

```text
HERMES_GPT_OAUTH_ENABLE=1
HERMES_GPT_OAUTH_ISSUER=https://mcp.example.com
HERMES_GPT_OAUTH_CLIENT_ID=chatgpt-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=https://chatgpt.com/connector/oauth/<exact-callback-id>
HERMES_GPT_OAUTH_SCOPE=hermes
```

Multiple exact redirect URIs may be comma-separated. Wildcards are not accepted. The issuer must use HTTPS except for an explicitly loopback-only test server.

The built-in OAuth boundary is available only with streamable HTTP (`--http`);
legacy SSE is rejected when OAuth is enabled so discovery and resource binding
cannot disagree.

Authenticated `remote` mode also requires one of these transport boundaries:

- direct TLS using both `--cert` and `--key`; or
- a loopback bind behind a trusted HTTPS reverse proxy, with every local proxy
  address explicitly listed in `HERMES_GPT_TRUSTED_PROXY_IPS`.

Wildcard, non-IP, and non-loopback trusted-proxy entries are rejected. Forwarded
headers are ignored unless this explicit loopback-proxy mode is active.

For a local HTTPS-terminating proxy, set:

```text
HERMES_GPT_TRUSTED_PROXY_IPS=127.0.0.1,::1
```

Then start the remote safety profile only after the configuration validates:

```bash
hermes-gpt --http --host 127.0.0.1 --port 4750 --profile remote
```

Keep the process loopback-bound and terminate HTTPS in a deliberately configured trusted proxy or private tunnel. The public issuer must resolve to that exact server.

## Additional clients (Gemini Spark profile)

One statically configured confidential client remains the default. An opt-in Gemini Spark client profile can be registered alongside it for Google's consumer Gemini Apps "Custom apps for Spark" connector:

```text
HERMES_GPT_OAUTH_GEMINI_ENABLE=1
HERMES_GPT_OAUTH_GEMINI_CLIENT_ID=gemini-spark-client
HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI=https://oauth-redirect.googleusercontent.com/r/<exact-google-callback>
```

- The profile is off unless `HERMES_GPT_OAUTH_GEMINI_ENABLE` is exactly `1`; enabling it without all three `HERMES_GPT_OAUTH_GEMINI_*` values fails startup validation with a `ValueError` naming the missing variables.
- `HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI` accepts one or more exact HTTPS URIs, comma-separated, parsed exactly like the primary redirect URI. Wildcards are not accepted.
- Both clients share the issuer, the resource, and the one configured `HERMES_GPT_OAUTH_SCOPE`; each keeps its own secret and its own exact-match redirect allowlist, and a client can only redirect to, or authenticate with, its own credentials.
- The primary `HERMES_GPT_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` values are unchanged and keep working.

Setup, callback discovery, verification, and rollback: [Gemini Spark custom app](gemini-spark.md).

## ChatGPT connector values

Configure the connector using values derived from the issuer:

```text
MCP URL:               https://mcp.example.com/mcp
Authorization endpoint:https://mcp.example.com/oauth/authorize
Token endpoint:        https://mcp.example.com/oauth/token
Client ID:             chatgpt-client
Client secret:         the generated confidential-client secret
Token auth method:     client_secret_post or client_secret_basic
Default scope:         hermes offline_access
OIDC:                  disabled
```

ChatGPT may add `openid` to the authorization request. `offline_access` is required for refresh-token issuance. A connector authorized before refresh discovery was available must be disconnected and connected once so discovery and authorization run again.

## Token lifecycle

An authorization-code exchange returns an access token with `expires_in=3600`. If `offline_access` was granted, it also returns a refresh token. A successful refresh returns a new access token and rotates the refresh token; replaying the old refresh token fails with `invalid_grant`.

Access tokens use HMAC-SHA256 signatures with a key derived from the primary
client secret. Origins sharing an issuer, client ID, client secret, resource,
and encrypted token store can validate one another's tokens. In server mode,
a valid signature is insufficient: the token must still be live in the shared
store. Opaque legacy access tokens remain valid on the issuing origin until
expiry, subject to the same in-memory and durable-store checks. Rotating the
primary client secret invalidates every signed access token.

### Durable storage

Access and refresh tokens survive restarts through
`<hermes_data>/secrets/hermes_gpt_tokens.db`. The store is SQLite with WAL and
0600 permissions. Each token row is AES-256-GCM ciphertext indexed by the
SHA-256 hash of its token value. Token material is never stored in plaintext
or included in audit records or MCP responses. `hermes_oauth_status` reports
presence and expiry only.

A process-shared mutation lock, `hermes_gpt_tokens.db.lock`, orders issuance,
refresh rotation, and revocation. Issuance merges records rather than replacing
another origin's valid tokens. Rotated or revoked hashes remain retired forever,
so stale peer caches cannot restore them. A durable revocation epoch also fences
out writes from peers holding pre-revocation state.

The pre-SQLite files `hermes_gpt_tokens.json`, `hermes_gpt_token_ledger`, and
`hermes_gpt_token_epoch` migrate in one transaction at first use. Migration
preserves retirement marks and the epoch, fails closed on invalid input, and
removes the legacy files afterward.

`HERMES_GPT_TOKEN_MASTER_KEY`, when set, takes precedence over stored keys and
is intended for CI or tests. Otherwise lookup uses the OS keyring, then
`<hermes_data>/secrets/hermes_gpt_token_key`, a 0600 file created on first use.
New keys go to the keyring when available, or to the key file. Without a
keyring service, the key sits beside the ciphertext; protect the entire
secret-bearing directory. Historical risk and legal-review context remains in
[the design archive](design/README.md).

### Revocation

`hermes_oauth_revoke` requires Owner authority, direct mode, `dry_run=false`,
and `confirm=true`. It retires every client's tokens on the instance's data
root and advances the epoch in one transaction. It then clears the live
process's caches and rotates the authorization-code signing key. The store
lock also protects optional master-key rotation after the transaction commits.
It can rotate the active master key by overwriting the keyring entry
or regenerating the key file. An environment-managed key is reported as not
rotated and must be changed externally.

Authorization codes are signed and expire after five minutes. Used-code replay
state, access-token caches, and refresh-token caches remain bounded in process
memory.

## Static bearer alternative

For clients that directly support a preconfigured bearer credential, set:

```text
HERMES_GPT_BEARER_TOKEN=<strong-random-token>
```

Static bearer authentication remains compatible with OAuth access tokens. Never put the bearer value in a URL, repository, log, prompt, or Operator audit record.

## Failure behavior

Hermes GPT fails closed when:

- OAuth is enabled but required configuration is missing;
- an enabled additional client profile (for example the Gemini Spark profile) is missing required configuration;
- the client secret is absent, malformed, or incorrect;
- the redirect URI, resource, grant type, or scope is unsupported;
- PKCE is supplied with a method other than S256 or the verifier does not match;
- an authorization code or refresh token is unknown, expired, used, or replayed;
- a refresh request attempts to increase scope;
- a bounded credential store is full.

Operator and Owner policy remains independent from transport authentication. Authenticating a connector does not activate mutations, direct mode, or Owner Mode.
