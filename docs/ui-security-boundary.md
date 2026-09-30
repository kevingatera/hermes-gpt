# UI security and state boundary

The Hermes ChatGPT UI runs in the same process as the MCP server and is served
from the same origin, so its security boundary is the redaction function every
browser-bound payload passes through. The UI adds no authority of its own.

`src/hermes_gpt/ui/security.py` owns that boundary, `/api/me`, and
`/api/connection`. `src/hermes_gpt/ui/routes.py` composes the route registry,
and `src/hermes_gpt/server/http.py` mounts it into the ASGI app. Browser code
lives under `web/src/shared/` and `web/src/stores/`. The authority model is
[Operator Mode](operator-mode.md); the UI contract is
[the Flight Deck UI contract](design/v0.7-flight-deck-ui-contract.md).

## What crosses the boundary

`redact_browser` serializes every browser-bound payload, both JSON response
bodies and SSE `data` lines. Handlers build responses through the `ok` and
`err` helpers, which call it, so a handler cannot skip redaction by accident.

- `ok(data)` applies strict redaction.
- `ok(data, content_allowed=True)` is used only for the user's own conversation
  text: chat thread `content` and SSE `token` / `reasoning` deltas. Every other
  value in that payload still gets strict treatment.
- `err(code, message)` redacts the message before it leaves the server.

Tool events (`tool_start`, `tool_end`) always use the strict default.

## What strict mode removes

Values under secret key names become `[REDACTED]`: `prompt`, `token`, `secret`,
`password`, `credentials`, `memory_body`, `transcript`, `request_dump`,
`client_secret`, `access_token`, `refresh_token`, `authorization`, `cookie`,
`private_key`, `profile_secret`, plus any key ending in `_key`, `_token`,
`_secret`, or `_password`. The marker is written rather than an empty value, so
a dropped value stays visible.

Strict mode treats `content` and `delta` as raw message bodies and redacts them
outright. Those keys survive only when a caller passes `content_allowed=True`.

Every string also runs through the shared output redactor in
`src/hermes_gpt/policy/redaction.py`, which removes `sk-…` / `sk-proj-…` OpenAI
keys, `AKIA…` AWS keys, `Bearer <token>`, and `token=|secret=|password=|api_key=`
values. Text derived from operator records loses more: emails, phone numbers,
`@handles`, and name labels, the same sanitizing used in
`src/hermes_gpt/missions/common.py`. Absolute paths then become
`[REDACTED_PATH]`, and secret-file references (`secrets/…`, `.env`,
`auth.json`, `hermes_gpt_tokens.json`, `hermes_gpt_tokens.db`,
`hermes_gpt_token_key`, `.ssh/…`) become `[REDACTED_SECRETS_PATH]`.

Strict-mode strings are truncated to `HERMES_GPT_UI_TOOL_PREVIEW_BYTES`
(default 8192) with a `…[truncated]` marker. The user's own conversation text
uses a 1 MiB bound instead, because it is chat and not a tool preview.
`content_allowed=True` skips the PII and path mangling and the 8 KiB cap for
that text only; unambiguous secret shapes are still removed.

## Authentication and authorization

The UI reuses `BearerAuthMiddleware` from `src/hermes_gpt/auth/http.py` and the
existing `build_asgi_app` wiring. Loopback with no auth is the default; static
bearer or the confidential-client OAuth boundary applies when configured, and
the existing server gates still block the remote profile. The UI adds no auth
path of its own. See [OAuth and bearer authentication](oauth.md).

With `HERMES_GPT_UI_ENABLED=1`, the UI routes register before the catch-all
`Mount("/", app=mcp_app)`, so same-origin `/api/*` and `/ui` requests never fall
through to the MCP app. With the variable unset, `server/http.py` does not
import the UI modules, so an installed wheel without them is unaffected.

## Account status and capabilities (`GET /api/me`)

`accountStatus` is derived read-only from the durable token store, never from
token material:

| State | Meaning |
| --- | --- |
| `ok` | no auth configured (loopback), static bearer, or a valid store |
| `expired` | durable store present with `expires_at` in the past |
| `revoked` | durable store unreadable or corrupt, so tokens are unusable |
| `unauthorized` | OAuth configured with no usable durable store, so re-auth is needed |

The same response carries `operatorLevel`, `allowedSurfaces`, `uiCapabilities`,
`model`, and `serverVersion`. `allowedSurfaces` follows the Mission allowlist:
unset allows all read-only surfaces, a list restricts to the listed ones, and
an empty value denies all. The unset state is not "deny by default".
`uiCapabilities` is permission-aware: `chat`, `flight`, and `events` always
appear, `fleet` appears when the fleet surface is allowed, and the mutating
`approvals` capability requires an `ok` account, an allowed `approvals`
surface, and a level at or above `workspace`.

## Restart and stale-state handling

`GET /api/connection` returns a per-process `serverStartupId`. The connection
store (`web/src/stores/connection.ts`) compares it across polls, so a server
restart in the middle of a session surfaces the in-flight turn as interrupted
and recoverable through persisted messages and the turn lease. It is never
shown as still running. `is_stale_lease()` treats a turn lease older than
`HERMES_GPT_UI_STALE_LEASE_S` (default 600) the same way.

`web/src/shared/ConnectionStatus.tsx` renders transport health.
`web/src/shared/AccountStatusBanner.tsx` renders the expired, revoked, and
unauthorized recovery state: a re-auth affordance, mutating controls disabled,
and read-only chat history still visible.

## Mutations

The UI exposes no mutation path of its own. Every mutation it offers goes
through the existing gated tool call at `POST /api/ops/action`, so the
read-only default, dry-run-first flow, confirm gate (`409 CONFIRM_REQUIRED`),
secret-path protections, and Operator audit record all still apply. The
boundary guarantees two things about the response: it is redacted, and the
`ok`/`error` envelope keeps its gate codes. `test_error_envelope_preserves_gate_codes`
asserts the second.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_GPT_UI_ENABLED` | unset (off) | mount UI routes and static serving |
| `HERMES_GPT_UI_PROFILE` | `default` | profile the UI runs as |
| `HERMES_GPT_UI_DIR` | `web/dist` | static build output override |
| `HERMES_GPT_UI_STALE_LEASE_S` | `600` | stale turn-lease threshold |
| `HERMES_GPT_UI_TOOL_PREVIEW_BYTES` | `8192` | per-string and tool-preview cap |

## Verification

```bash
python -m pytest tests/ui/test_ui_security.py
cd web && npm install && npx tsc --noEmit
```

`tests/ui/test_ui_security.py` covers redaction properties, account states, the
auth boundary, allowlist semantics, and a sweep over every mounted `GET /api/*`
route. `test_property_all_api_get_routes_redacted` asserts no route body
contains a forbidden pattern, and `test_property_sse_payloads_redacted` runs
each SSE event shape through the boundary. Routes added later are covered by
the same sweep. Chat and browser routes have their own suites
(`tests/ui/test_ui_chat.py`, `tests/ui/test_ui_ops.py`,
`tests/ui/test_ui_missions.py`, `tests/ui/test_ui_fabric.py`).
