# Delegations

A delegation tracks a Work Contract across runner or Fabric execution. Its
record stores lineage and state. Operator policy controls dispatch and
cancellation; backend observations and Work Contract validation determine
completion.

## Tools

| Tool | Use it to |
| --- | --- |
| `hermes_delegation_dispatch` | preview or dispatch a canonical Work Contract, with optional mission linkage |
| `hermes_delegation_get` | read a delegation and its bounded lifecycle events |
| `hermes_delegation_list` | filter records by mission or state |
| `hermes_delegation_reconcile` | derive state from backend observations; applying it requires workspace and direct authority |
| `hermes_delegation_cancel` | request cancellation through the selected backend and its existing gates |

States are `queued`, `running`, `reconciling`, `blocked`, `succeeded`, `failed`,
and `cancelled`.

The database stores bounded IDs, contract digests, backend references, states,
timestamps, and event hashes. It excludes contract objectives, prompts, and
model responses.

## Verify completion

A worker reporting success is only one part of the evidence. Reconciliation
reads runner or Fabric observations for the Work Contract task ID. Completed
backend execution stays `reconciling` until validation of the matching contract
returns `SATISFIED`.

The contract task ID and canonical SHA-256 must match the stored lineage.
Missing or unreadable evidence, and an `UNVERIFIED` verdict, cannot produce
success.

When linked to a mission, dispatch creates or updates a `delegation` attachment.
The attachment becomes `succeeded` after backend completion and matching
`SATISFIED` validation. It records the contract digest as its verification
reference.

## Cancel and reconcile

Cancellation records `cancelled` only after an explicit `cancelled` or
`canceled` backend response. Other successful responses remain `reconciling`.
An unsuccessful response that explicitly made no change releases the pending
cancellation claim. An ambiguous failure keeps the claim pending.

While `cancellation_in_progress` is set, an exact retry returns the pending
result without invoking cancellation again. The first caller or reconciliation
must resolve the claim.

Reconciliation needs a terminal backend observation ordered after the durable
cancellation claim. A parseable, timezone-aware `ended_at` or `completed_at`
must be strictly later than `cancellation_claimed_at`. Equivalent terminal
fields `finished_at` and `terminal_at` are also accepted. The current check
uses terminal timestamps, not backend generation counters.
A changed payload, a new hash, the time the server saw an observation, a generic
update time, or an equal timestamp cannot establish that ordering.

After a qualifying observation:

- Explicit cancellation records durable cancellation.
- Terminal failure clears the pending claim and records failure.
- Terminal completion clears the claim but still needs matching Work Contract
  validation before success.

Missing, stale, ambiguous, unknown, or nonterminal observations leave
`cancel_requested` and `cancellation_in_progress` set. The delegation remains
`reconciling` until authoritative evidence resolves it.

## OpenCode runner

The `opencode` backend invokes the installed CLI with
`opencode run --format json --pure --dir <workspace>`. It sends the objective
through stdin. Hermes never enables `--auto`.

The runner confinement layer enforces filesystem access. Read-only contracts
receive a read-only workspace; write-authorized contracts require the
`workspace-write` posture. The optional `model`, `agent`, and `variant` values
are bounded. Model selection also requires the runner model allowlist.

Provider authorization stays in the trusted Hermes worker. Each child gets a
random relay capability and sanitized provider configuration. The loopback
relay checks that capability before forwarding and adds upstream authorization
only after the check. The child configuration, arguments, workspace, and
environment never contain that upstream authorization.

## Live events

Delegation changes publish bounded notifications through [live events](live-events.md).
Use each notification to read the durable delegation or backend record again.
Notification failure cannot advance state or complete work.
