# Capability manifest and mission ledger

Two read-only derived views answer questions that would otherwise mean opening
several stores by hand. Both are read models: they read the existing
authoritative registries and stores at query time and merge the results. Neither
adds a source of truth, a durable table, or a mutation path, and neither returns
raw prompts, transcripts, memory bodies, credentials, or secret-path content.
Refs, bounded metadata, and content-addressed hashes are all that cross.

## `hermes_capability_manifest`

Answers "what can X do" by folding the authoritative registries into one
normalized capability registry:

- Fabric node registry (`fleet/fabric_config.py`)
- Fleet authority manifest (`<root>/config/fleet-authority.json`)
- Profile toolsets and skills (per-profile `skills/` directory plus config)
- Provider dimension (derived from each profile's configured model and provider)

Each entity carries `capability_sha256` (content address) and `derived_from`
(source refs) for provenance. The `placement_cache` object is a TTL-bounded
derived snapshot, rebuilt from the registries when it expires. It lives in
process memory and is never a durable store.

Sources open read-only (`mode=ro` / `r`), and no write path exists.

Args: `source` (fabric|fleet|profile|provider, empty = all), `include_cache`,
`limit`. Allowlist env: `HERMES_GPT_CAPABILITY_ALLOWED_SOURCES`.

## `hermes_mission_ledger` and `hermes_mission_ledger_replay`

An append-only, replayable merged cursor stream per mission, reconciled at query
time from the existing authoritative stores:

- `mission_events`: `<root>/missions/missions.db`
- `delegation_events`: `<root>/delegations/delegations.db` (linked through the
  mission's delegations; tasks are carried for the kanban join)
- operator audit: `<root>/logs/hermes_gpt_operator_audit.jsonl`
- kanban `task_events`: `<root>/kanban/boards/<slug>/kanban.db` (for the tasks
  owned by this mission's delegations)

Every event carries an opaque per-source watermark `cursor` (prefix `ld1.`).
Each authoritative source keeps its own stable monotonic sequence
(`mission_events.seq`, `delegation_events.seq`, audit line number, kanban
`rowid`), and the merge orders events without reordering within a source, so a
late event with an older timestamp is still delivered after the watermark. The
stream is deterministic for the same store state, so
`hermes_mission_ledger_replay` reproduces the mission's event history.

Args: `mission_id`, `source` (mission|delegation|audit|kanban, empty = all),
`cursor` (opaque `next_cursor` token from a previous page, or 0 to start),
`limit`, `replay`. Allowlist env: `HERMES_GPT_LEDGER_ALLOWED_SOURCES`.

Every SQLite source opens `mode=ro`, and no mutation path exists. Tests assert
row counts and file sets are unchanged.
