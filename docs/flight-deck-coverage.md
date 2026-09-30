# Flight Deck coverage

What the read-only browser adapter and UI cover today, and what was left out on
purpose.

The adapter code is `src/hermes_gpt/ui/ops.py`, `src/hermes_gpt/ui/fabric.py`,
`src/hermes_gpt/ui/missions.py`, `src/hermes_gpt/fleet/fabric_view.py`, and the
React panels under `web/src/flight/`. Design background lives in
[docs/design/v0.7-flight-deck-architecture.md](design/v0.7-flight-deck-architecture.md)
and [docs/design/v0.8-fabric-architecture.md](design/v0.8-fabric-architecture.md).
Policy authority is unchanged and documented in [Operator Mode](operator-mode.md).

## Read-only routes

- [x] Durable Missions through `GET /api/ops/missions`, mission detail,
  mission-filtered cursor and long-poll wake-ups, and delegation detail. The
  browser re-reads durable Mission state after a wake-up and exposes no Mission
  mutation path.
- [x] Mission Control overview, health, profiles, fleet, Codex, cron,
  delegations, failures, approvals, vault, usage, and audit through
  `GET /api/ops/{surface}`.
- [x] Event History query and bounded tail through `GET /api/events`.
- [x] Contracts and review-acceptance evidence through
  `GET /api/ops/contracts`, `GET /api/ops/contracts/{contract_sha256}`, and
  `GET /api/ops/review/{contract_sha256}`.
- [x] Swarm workflow list and detail through `GET /api/ops/swarm` and
  `GET /api/ops/swarm/{workflow_id}`.
- [x] Codex job and cron-job detail routes.
- [x] Fleet status, policy summary, and OAuth-store presence and expiry status
  without token material.
- [x] Fabric node roster through `GET /api/ops/fabric/nodes`: enrolled
  identity, coordinator-owned capability freshness, a bounded capability
  summary, active and capacity observations, and the authority ceiling, each
  labeled observed, stale, unknown, or disabled, with no peer RPC.
- [x] Fabric remote attempt list and detail through
  `GET /api/ops/fabric/attempts` and
  `GET /api/ops/fabric/attempts/{attempt_id}`: node and backend,
  explicit-versus-auto placement, retry lineage, blocker and error state, a
  coarse write-epoch authority summary, bounded durable or reconciled peer
  observations of write-claim and execution-unit state, admitted evidence
  provenance, admitted artifact metadata, and bounded Fabric audit history.
  Peer observations are labeled as observations, never as coordinator authority
  or a completion verdict, and `LOST_AMBIGUOUS` is always shown as a blocker.
- [x] Durable auto-placement receipts through `GET /api/ops/fabric/routing`.
  Current receipts persist hard requirements, bounded candidate exclusions, the
  selected target, and the deterministic rank. Older receipts stay readable and
  report that the detailed explanation was not persisted instead of
  reconstructing or guessing it.

## Fabric trust boundary

- [x] `src/hermes_gpt/fleet/fabric_view.py` reads only. It does not call peer
  RPC, poll, reconcile, cancel, retry, collect evidence or artifacts, dispatch
  work, or create the coordinator journal.
- [x] Fabric browser routes live in their own GET-only module, so the `ui/ops.py`
  mutation allowlist is untouched.
- [x] Fabric views never expose A2A URLs, bearer credentials, coordinator
  principal secrets, local workspace mappings, artifact snapshot paths, or
  coordinator admission paths.
- [x] Active remote HTML, SVG, and JavaScript is never rendered in the trusted
  Flight Deck origin. The UI receives bounded metadata plus an
  `isolated_metadata_only` policy marker.
- [x] Remote worker observations stay evidence inputs rather than a completion
  verdict, and the UI states that coordinator validation remains
  authoritative.
- [x] Stale, unavailable, ambiguous, reconciling, and evidence-pending states
  appear as such; presentation logic never upgrades them to an optimistic
  green.
- [x] Router explanation fields are coordinator-generated, closed and bounded,
  and carry no raw peer logs or caller filesystem and network targets.
- [x] Any future Fabric intervention goes through an existing gated Hermes
  operator tool with the usual dry-run and confirm semantics. There is no
  browser-only peer mutation endpoint.

## General state handling

- [x] Browser payloads use the existing bounded and redacted operator read
  models plus the shared `ui/security.py` redaction envelope.
- [x] Mission allowlist semantics are preserved: unset permits all read-only
  surfaces, a list restricts to the listed surfaces, and an empty value denies
  all.
- [x] UI panels render loading, unavailable or empty, stale, and error or retry
  states.
- [x] Event History is poll-driven because the backend has no generic push
  event stream.
- [x] Every supported mutation goes through `POST /api/ops/action`, with a
  strict per-tool argument allowlist and server-side root resolution.
- [x] Mutation is dry-run first. Confirmation is a second explicit user action
  and is passed only as the existing tool's `confirm` argument.
- [x] Operator level, direct mode, confirmation, audit, and secret-path
  protections stay authoritative. Flight Deck adds no bypass.
- [x] Blocking cron execution returns `202 Accepted` and runs off the request
  path; the UI refreshes the cron read model for status.

## Deliberate omissions

- No independent contract registry. Contract list and detail are a bounded
  composition of existing review evidence and swarm workflow references.
- No generic approve or reject writer. The UI only exposes existing gated tool
  actions.
- No read-only review-acceptance list tool. The adapter reads the existing
  bounded review-evidence store.
- Fabric node health on the roster is coordinator-observation freshness, not a
  live probe. Live verification stays part of dispatch and routing correctness
  rather than presentation polling.
- The coordinator journal records whether a write epoch was granted, not the
  original write authorization subclass. Flight Deck labels that authority
  summary as coarse instead of inventing reversible-write versus high-impact
  detail.

## Verification

- `tests/ui/test_ui_ops.py`: mission and status envelopes, allowlist behavior,
  event query, route composition, and adversarial mutation-gate tests.
- `tests/fleet/test_fabric_view.py`: stale and unknown node semantics,
  non-mutating journal reads, routing-receipt compatibility, evidence
  redaction, artifact path isolation, and active-content policy.
- `tests/ui/test_ui_fabric.py`: GET-only Fabric routes, shared browser
  redaction, invalid-id handling, route composition, and no private artifact
  path leak.
- `tests/ui/test_ui_missions.py`: Mission list, detail, and event routes.
- `web` Vitest: Fabric stale and blocked rendering, routing explanations,
  active artifact isolation, absence of direct mutation controls, plus the
  operator, event, and approval suites.
- `web` Vite build: TypeScript and bundle validation.
