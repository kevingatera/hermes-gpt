# Live Hermes settings in ChatGPT

On an explicitly administered private connection, ChatGPT can use
`hermes_connection_settings` to inspect settings and
`hermes_connection_configure` to change them on user request. The panel offers
the same controls. Preview is the default; apply with `confirm=true` and
`dry_run=false`. Settings changes take effect on subsequent calls without a
plugin metadata refresh. Newly deployed tool schemas still require one refresh.

## Server setup

Set `HERMES_GPT_CONNECTION_ADMIN=1` and
`HERMES_GPT_CONNECTION_ALLOWED_PROFILES` to explicit permitted profile names.
Administration is disabled by default. Use this on a private connection that
the user controls: a client with access can change these connection settings.
Existing transport, Operator enablement, level, path restrictions, and Owner
gates remain configured by the server and cannot be changed through this tool.

Known features are `history`, `delegation`, `managed_tasks`, `web`, `vision`,
`diagnostics`, `scheduling`, `schedule_changes`, `skill_changes`, and `changes`.
`profiles` updates the session, Operator, and task-browser profile allowlists
within the server ceiling. Browser availability also requires an actual local
browser configuration. `apply_mode` accepts `dry_run` or `direct`.

Settings persist in `$HERMES_HOME/hermes-gpt-connection.json` with mode 0600 on
POSIX and atomic replacement. `expected_revision` prevents stale panel writes
within the running server. Invalid persisted policy disables features and
profile access. Management and session tools keep a stable catalogue on these
connections; their backend permissions are evaluated at request time.

## Useful actions

- `hermes_ask`: work through the selected profile's configured resources;
  start or continue a turn, follow its job ID, and retrieve the actual result.
- `hermes_profile_config_get` / `hermes_profile_config_set`: inspect redacted
  configuration and change nonsecret defaults. Changes apply on the next turn.
- `hermes_schedule_create`: create a schedule through Hermes' canonical CLI,
  paused by default, with local delivery and optional model/effort overrides.
- `hermes_schedule_action`: pause, resume, queue a run, or remove an existing
  schedule. Queuing a run does not confirm completion.
- `hermes_cron_list` / `hermes_cron_status`: inspect observed scheduler state.
- `hermes_request_diagnostics`: inspect request outcomes and trace references
  without private request or response bodies.

Direct profile and schedule changes require explicit per-call confirmation and
server direct mode. Direct changes also require `changes`; schedule actions require `schedule_changes`. Protected
credentials remain unavailable to config writes. Hermes loads provider
credentials itself; this connection needs no separate inference key.
