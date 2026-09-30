# Flight Deck missions

Flight Deck exposes first-class Missions as a read-only operational view. The
browser does not gain Mission mutation, dispatch, cancellation, reconciliation,
or approval authority.

## Routes

- `GET /api/ops/missions`: bounded Mission list with current durable state.
- `GET /api/ops/missions/{mission_id}`: durable Mission detail plus linked
  delegation summaries.
- `GET /api/ops/missions/{mission_id}/events`: bounded cursor and long-poll
  wake-up events filtered to one Mission.
- `GET /api/ops/delegations/{delegation_id}`: one normalized delegation read
  model.

All browser payloads pass through the existing Flight Deck redaction boundary.
Mission and delegation stores remain authoritative; live-event payloads are
wake-up notices only. The detail screen responds to a wake-up by re-reading
durable Mission state rather than treating the event payload as completion
evidence.

## Visible Mission state

The Mission list and detail screens expose bounded title and objective metadata,
owner profile, status and version, acceptance criteria, context references and
digests, explicit skills manifests, approval presence and requirement,
attachments, linked delegation state, and recent Mission events.

The detail endpoint captures its live-event cursor before reading the Mission
snapshot. That ordering stops a state transition that races with the snapshot
from being skipped: the change is either already reflected in the durable
snapshot or remains after the returned cursor and wakes the browser for another
durable read.

## Authority boundary

The Mission UI contains no direct mutation controls. State transitions,
attachment writes, reconciliation, delegation dispatch and cancel, and Owner
approval continue through their existing operator tools and policy gates. Flight
Deck is presentation and observation only.
