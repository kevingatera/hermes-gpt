# Gemini Spark custom app

Connect Gemini's custom-app client to an authenticated Hermes GPT MCP server.
Use a dedicated read-only instance, or add a separate confidential-client
profile to an existing instance. The profile has its own credentials and
redirect allowlist. Operator authority is shared by all clients on an instance.

The [OAuth guide](oauth.md) owns scopes, PKCE, refresh rotation, and token
storage. This guide covers the Gemini-specific settings and callback discovery.

## Prerequisites

- A personal Google Account that is 18+, in the US, using English, with activity ("Keep Activity") on. Custom apps are added in the Gemini web app; see [Google's help page](https://support.google.com/gemini/answer/17209137).
- Hermes GPT OAuth configured and validated as documented in [OAuth and bearer authentication](oauth.md): `HERMES_GPT_OAUTH_ENABLE=1`, an HTTPS issuer, a confidential client, and a client secret of 43–128 URL-safe characters.
- HTTPS exposure Google can reach for both the authorization endpoints and `/mcp`. A [Cloudflare Tunnel deployment](cloudflare-tunnel.md) is one supported public-proxy path; the authenticated remote-mode rules in [oauth.md](oauth.md) still apply (keep the process loopback-bound and terminate HTTPS in a deliberately configured proxy).
- The MCP endpoint URL you will paste into Gemini: `https://<your-mcp-host>/mcp`.
- The built-in OAuth boundary requires streamable HTTP (`--http`); legacy SSE is rejected when OAuth is enabled.

## Authority and credentials

OAuth authenticates Gemini without enabling Operator mutations. Every client
on an instance reaches the same policy-gated tools. If the instance has direct
or Owner authority, Gemini can use that authority too. Prefer a dedicated
read-only instance.

Each client keeps its own ID, secret, and exact HTTPS redirect allowlist.
Another client's credentials or redirects are refused. Hermes GPT advertises
no dynamic client registration, so enter the Client ID and secret manually in
Gemini.

## Configuration

Choose a dedicated instance or an additional client profile.

### Dedicated instance

Serve the Gemini Spark connector from its own instance using the standard single-client variables:

```text
HERMES_GPT_OAUTH_ENABLE=1
HERMES_GPT_OAUTH_ISSUER=https://gemini-mcp.example.com
HERMES_GPT_OAUTH_CLIENT_ID=gemini-spark-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=<the exact Google callback discovered below>
HERMES_GPT_OAUTH_SCOPE=hermes
```

Generate the secret as documented in [oauth.md](oauth.md). Do not reuse a Hermes, gateway, or provider credential.

### Additional client profile

The primary client configuration is untouched; add the opt-in Gemini profile beside it:

```text
# Primary client, for example ChatGPT
HERMES_GPT_OAUTH_CLIENT_ID=chatgpt-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<existing-primary-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=https://chatgpt.com/connector/oauth/<exact-callback-id>

# Opt-in Gemini Spark client profile (default: off)
HERMES_GPT_OAUTH_GEMINI_ENABLE=1
HERMES_GPT_OAUTH_GEMINI_CLIENT_ID=gemini-spark-client
HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI=<the exact Google callback discovered below>
```

- The profile is enabled only when `HERMES_GPT_OAUTH_GEMINI_ENABLE` is exactly `1`; unset or any other value leaves it off.
- The profile is an addition to a complete primary configuration: it is evaluated only when `HERMES_GPT_OAUTH_ENABLE=1` and the required `HERMES_GPT_OAUTH_*` variables are present.
- Enabled but incomplete fails startup validation. The process does not serve, and the uncaught `ValueError` names the missing variables (for example `HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET`).
- `HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI` accepts one or more exact HTTPS URIs, comma-separated, parsed exactly like the primary redirect URI.
- Both clients share the issuer, the resource (`<issuer>/mcp`), and the one configured `HERMES_GPT_OAUTH_SCOPE` (default `hermes`); the compatibility scopes `openid` and `offline_access` are accepted for both.
- Client IDs must be unique across the primary client and every profile; a duplicate fails startup validation.
- Do not reuse the primary secret for the Gemini profile. Keep the credentials and redirect allowlists separate.

Restart the instance after changing any of these values.

## Callback discovery

Google assigns the callback URI per connection and there is no pattern to guess. The first connection attempt is rejected (the authorize response is an `invalid_request` error, `redirect_uri is not registered.`), and the server's HTTP access log records the request line for that `/oauth/authorize` call, including the `redirect_uri` query parameter, which is the exact callback Google used. The access log records client address, method, path with query string, and status; it never records bodies or headers, so no client secret, authorization code, or bearer token appears in it.

Observed callback shape:

```text
https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-<numeric-google-app-id>-<host-with-dots-as-underscores>
```

Example with anonymized values:

```text
https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-123456789012345678901-example_com
```

Procedure:

1. Attempt the connection once in Gemini and let it fail.
2. Read the rejected `/oauth/authorize` request line from the server's HTTP access log and copy the `redirect_uri` value verbatim into `HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI` (or `HERMES_GPT_OAUTH_REDIRECT_URI` on a dedicated instance).
3. Restart the instance so the allowlist is reloaded.
4. Retry the connection in Gemini.

Never add a wildcard, a suffix, or a "close enough" variant: exact matching means anything but the verbatim value is rejected.

## Connecting in Gemini

In the Gemini web app: **Settings & help → Connected Apps → Custom apps → Add a custom app**, paste the MCP URL (`https://<your-mcp-host>/mcp`), then open **Advanced features → Show more** and enter the Client ID and Client secret manually. This is Google's documented path when the server does not advertise dynamic client registration.

Success looks like: the custom app connects without Google's account-linking error, and the server log shows the authorize hop, then `POST /oauth/token` → 200, then authenticated `POST /mcp` traffic.

## Verification

Sanitized server-side evidence to expect (never paste tokens, codes, or secrets into a report):

- `GET /.well-known/oauth-protected-resource` and `GET /.well-known/oauth-authorization-server` return 200 anonymously;
- an unauthenticated `HEAD /mcp` or `POST /mcp` returns 401 with a `WWW-Authenticate: Bearer realm="hermes-gpt", resource_metadata="<issuer>/.well-known/oauth-protected-resource"` challenge;
- after the browser authorize hop, `POST /oauth/token` returns 200;
- authenticated `POST /mcp` calls arrive for `initialize`, `tools/list`, and `tools/call`.

Ask Gemini to list Hermes skills. That exercises the read-only
`hermes_skill_list` tool through the new credential. `hermes_oauth_status` (read-only) reports durable token-store presence and expiry only, without token material.

## Token lifecycle

- The token exchange returns an access token with `expires_in=3600` (one hour).
- When `offline_access` was granted, it also returns a refresh token. Each refresh rotates the refresh token; replaying the old value fails with `invalid_grant`. Refresh tokens live 30 days and survive restarts through the encrypted durable store.
- `hermes_oauth_revoke` (owner + direct + confirm) revokes every client on that instance's data root in one transaction and requires `dry_run=false` plus `confirm=true` with Owner Mode active; it also drops the live process's token caches and rotates the authorization-code key. See [OAuth and bearer authentication](oauth.md#token-lifecycle) for the store and key-management details.
- Rotating the deployment's primary client secret invalidates every signed access token, including tokens issued to the Gemini client.

## Limitations

- Enter credentials manually under Advanced features > Show more. The server
  has no `registration_endpoint`.
- PKCE accepts S256 only. Other challenge methods return `invalid_request`.
- The user's browser must resolve the MCP hostname during authorization.
  Backend reachability alone is insufficient.
- Google-side account-linking errors can be transient. Check the sanitized
  server trace before retrying.
- This guide covers the consumer custom-app client. Check Google's linked
  requirements before setup.

## Rollback

- Disable the profile: unset `HERMES_GPT_OAUTH_GEMINI_ENABLE` (and the three `HERMES_GPT_OAUTH_GEMINI_*` values) and restart. The primary client keeps working and its tokens remain valid; tokens issued to the Gemini client stop validating once the profile is gone, because validation requires the token's client to still be registered.
- Dedicated instance: stop that service (for example `systemctl --user stop hermes-gpt-server.service` for a user-service deployment) so the Gemini endpoint is no longer served.
- Gemini side: **Settings & help → Connected Apps → Custom apps** and remove the custom app.
- Optional: `hermes_oauth_revoke(confirm=true, dry_run=false)` with Owner Mode active retires the durable token rows for every client on that instance's data root.
