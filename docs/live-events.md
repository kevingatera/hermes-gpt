# Live events and WebSocket stream

Hermes GPT adds a durable, bounded live-event bus for clients and parent
orchestrators that need completion or wake-up delivery without polling every
underlying store.

## Authority model

Live events are notifications, not proof. Mission, Swarm, Work Contract,
runner, Fabric, and session-job records remain authoritative. A missing,
delayed, duplicated, or reconnected event must never advance work or change an
authority decision. Clients use event references to re-read durable state before
acting.

## MCP tools

- `hermes_live_events_cursor()` returns the current durable high-water cursor.
- `hermes_live_events_since(cursor, mission_id, topic, kind, limit, wait_ms)`
  returns bounded events after a cursor and may long-poll for up to the
  configured maximum.

Reads are non-creating when the live-event store does not yet exist.

## WebSocket stream

`/events/ws` provides the same durable stream over WebSocket. Operator mode must
be enabled for a connection to be accepted. In unauthenticated loopback
deployments, no additional credential is required. In static-bearer
deployments, non-browser WebSocket clients must send the same
`Authorization: Bearer <token>` credential used by Hermes HTTP/MCP before the
socket is accepted. OAuth access tokens use the same middleware checks as MCP
requests. Browser WebSocket APIs cannot attach an Authorization header; those
clients use the authenticated MCP cursor and long-poll tools instead. A
WebSocket client that can send headers may authenticate with a valid token.

Supported client control frames are intentionally narrow:

- `ping`: liveness response only;
- `subscribe`: change cursor/topic/kind/mission filters;
- `ack`: advance the client-side acknowledgement cursor.

The WebSocket accepts no Hermes mutation commands.

## Event safety

Events are bounded and redacted before persistence. Secret-like keys,
prompt/body/content/transcript fields, credentials, authorization material,
cookies, passwords, private keys, and API/access/refresh key fields are replaced
with `[REDACTED]`. Oversized payloads are represented by bounded digest metadata
rather than raw content.

Event IDs are idempotent. Consumers should persist the returned cursor and
resume from it after reconnects.

## Producers

The producers are Mission lifecycle changes, Swarm operator actions,
and Hermes session-control jobs. Session jobs publish `running`, elapsed-time
`progress`, and terminal status events on the `session-job` topic; those payloads
do not contain prompts or captured output. Session-job records remain
authoritative, and event publication failure is non-fatal: clients re-read job
status and result after a wake-up or reconnect.

## Retention

The live-event journal is bounded. `HERMES_GPT_LIVE_EVENT_RETENTION` sets the
retained row count, defaulting to 20000 and clamped to a hard cap of 100000.
Retention affects notification history only; it never removes the underlying
Mission/Swarm/Fabric/session-job evidence stores.
