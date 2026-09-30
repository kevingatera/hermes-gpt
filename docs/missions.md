# Missions

A Mission is the durable parent record for a larger objective. It stores bounded
metadata and references only; it is not a transcript or raw-source archive.

A Mission can contain:

- a title and objective;
- acceptance criteria;
- bounded context references with optional SHA-256 digests;
- an explicit skills manifest;
- attachments to Swarm workflows and delegation/run identifiers;
- lifecycle state and audit history;
- a final Owner approval requirement that defaults on; disabling it is an
  Owner-only creation-time decision and cannot be weakened later.

The implementation lives in `src/hermes_gpt/missions/`. The existing
`hermes_mission_*` Mission Control overview tools remain available and backward
compatible; the lifecycle tools below are additional.

## Lifecycle

Mission states are durable and restart-safe. The normal progression is:

`draft -> running -> awaiting_approval -> completed`

A Mission can also be paused or blocked when attached execution state requires
it. Reconciliation observes attached Swarm workflow state, and accepts
non-workflow success only from lifecycle adapters that mark the attachment as
verified with an evidence reference. Public attachment calls cannot assert
`succeeded`.

`final_approval_required` defaults to true and is immutable after creation.
Creating a Mission with that boundary disabled requires Owner authority.
Approval-required Missions enter `awaiting_approval` through reconciliation and
can complete only through `hermes_mission_approve`. A Mission cannot bypass the
existing Operator/Owner authority model.

## Tools

The Mission lifecycle tools are:

- `hermes_mission_create`
- `hermes_mission_get`
- `hermes_mission_list`
- `hermes_mission_update`
- `hermes_mission_attach`
- `hermes_mission_transition`
- `hermes_mission_reconcile`
- `hermes_mission_approve`

Read operations are read-only. Mutating operations keep the normal
workspace, direct, and confirm gates. Direct completion is Owner-gated, and
approval-required Missions complete only through explicit Owner approval.
Workflow references use the canonical `sw-*` identifier grammar and are confined
to the durable workflow directory.

## Context and skills

Mission context is represented as references rather than copied source bodies.
References are bounded and may include a digest for immutable identification.
Mission skill entries are explicit manifests containing a skill name and
optional version, ref, and digest metadata.

References keep Missions from becoming an unbounded prompt store, and keep
sensitive raw content in the source system that owns it.

## Persistence and audit

Mission state is stored durably under the Hermes data root in a SQLite database.
Mission lifecycle changes produce durable Mission events and Operator audit
records. Reads do not create Mission state when no Mission database exists.

## Relationship to Swarm and Fabric

Missions do not replace Work Contracts, Swarms, runners, or Fabric. They provide
the durable parent lifecycle above those existing execution and evidence layers:

`Mission -> Swarm/work -> Work Contract -> runner/Fabric -> observed evidence -> Mission reconciliation`

Completion remains based on coordinator-observed evidence and explicit approval
boundaries, preserving the Fabric safety model. A terminal backend self-report is
not sufficient: Mission-facing success must come from a verified lifecycle
adapter, and unverified legacy success is reconciled fail-closed to `blocked`.

## Delegation dispatch and reconciliation

Direct delegation dispatch reserves durable lineage before invoking a backend.
The delegation store records the phases `reserved`, `invoking`, and `dispatched`;
the `reserved -> invoking` claim is atomic. An exact retry with the same
delegation id, task id, Mission, backend, contract digest, and validation context
is idempotent. Any collision with different lineage is rejected. A definite
backend rejection with no possible side effect returns the row to `reserved`; an
ambiguous invocation remains `invoking` and is never automatically redispatched.

For Mission-bound work, Hermes commits a private `pending` delegation attachment
before backend invocation and rechecks the Mission and attachment immediately
before the call. The reservation is idempotent and cannot downgrade an existing
attachment. Public `hermes_mission_attach` calls cannot assert either `succeeded`
or `cancelled` for any attachment kind. A Mission cannot be cancelled while a
delegation child is reserved, invoking, pending, running, or ambiguous.

Delegation reconciliation does not require callers to resupply the Work Contract.
Dispatch stores a private, prompt-free validation manifest containing the
manifest schema, immutable contract and validation-context digests,
validation-only fields, and timestamps. It never stores objectives, prompts,
transcripts, raw inputs, constraints, secrets, or backend request/response
bodies; secret-like durable values are rejected before dispatch. Reconciliation
uses the same observed-state validation algorithm as `hermes_contract_validate`,
with or without a matching `contract_json` supplied for parity checking.

Missing, corrupt, mismatched, or secret-like manifests fail closed. Missing
current backend observation also fails closed, even if an earlier cached
lifecycle or validation value claimed success. Mission reconciliation and Owner
approval freshly re-observe delegation backend state, artifacts, review evidence,
authorization, and the manifest on every decision. Verified Mission evidence is
exactly `contract:<contract_sha256>`; cached attachment state and cached
delegation verdicts are not completion authority.
