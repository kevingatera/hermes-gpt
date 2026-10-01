---
name: hermes-control
description: Interact with Hermes Agent from ChatGPT using its configured tools, accounts, skills, scheduled jobs, conversations, and browsers.
---

Use the Hermes GPT tools when the user asks Hermes for information or actions,
including work with configured accounts, conversations, schedules, and browsers.

## Optional control panel

Use `hermes_console` when the user asks to open the Hermes panel or wants
buttons for sending work, choosing a profile, continuing a conversation,
inspecting schedules, or following a job. The panel calls the same authorized
tools as the conversation. If the console tool is absent, use the conversational
workflow below. No browser login or separate provider key is needed for the
panel. It does not configure account credentials.

## Ordinary requests and scheduled jobs

When the user asks Hermes to do something, delegate through a regular profile
session. This includes checking email through Hermes's configured integrations.
Prefer `hermes_ask` after discovering an authorized profile. It starts or
continues a turn and returns an answer or a job ID. Poll a pending job and retrieve the
answer. Report missing integrations from the actual result. Do not infer their
availability from the bridge's direct-tool capability flags. Do not claim the
request is complete merely because the job was accepted.

Use `hermes_cron_list` and `hermes_cron_status` to inspect existing schedules in
an authorized profile. These reads do not need the cron creation gate. Do not
turn on scheduling writes to answer a question about existing jobs.

## Live configuration

When available, `hermes_connection_settings` describes the connection's live
features, authorized profiles, and revision. On the user's request, preview
changes with `hermes_connection_configure`, then apply with `confirm=true` and
`dry_run=false`. Known feature and profile changes take effect immediately,
without refreshing the plugin. The server limits profiles to its configured
ceiling and retains protected-path and Owner restrictions. Tell the user that
they can ask to enable a feature or change a profile during the conversation.

Use `hermes_profile_config_get` and `hermes_profile_config_set` for nonsecret
profile defaults; changes apply to the next Hermes turn. Do not ask for a
separate provider key. Use `hermes_schedule_create` and
`hermes_schedule_action` for requested schedule changes. New schedules default
to paused, local delivery. A run queues work for the next scheduler tick;
check its status before reporting completion. Direct mutations require
`confirm=true`, `dry_run=false`, and server direct mode. Never change settings
to answer a read-only request.

## Sessions

- Treat regular Hermes session IDs and managed task IDs as different identifiers.
- Call `hermes_session_profiles` when you need to discover which configured
  profiles this connection may use and their default model, provider, and
  reasoning effort. Use only a listed profile.
- Regular profile sessions use the selected profile's `HERMES_HOME`; Hermes
  loads its configuration and credentials itself. Managed tasks clone the
  selected profile's supported resources into private task state while keeping
  session history separate. Neither workflow needs a separate provider key.
- For an existing regular session, use `hermes_session_list` and
  `hermes_session_read` to find the intended session, then call
  `hermes_session_continue` with its session ID and profile.
- Start a regular profile session with `hermes_session_start`. Save its job ID,
  poll `hermes_session_job_status`, and read `hermes_session_job_result`. Use
  the session ID returned in job status for later turns; never pass its job ID
  to a continue tool.
- Use `hermes_task_start` when the user asks for a confined workspace session.
  Start it with an authorized workspace alias and Hermes profile. Use
  `hermes_task_list` to find it later, then resume it with
  `hermes_task_continue` and its task ID. A task continues in the same
  workspace and private Hermes session.
- Pass `model` and `reasoning_effort` only when the user requests an override.
  Otherwise omit both so Hermes uses the selected profile's configuration. Do
  not request a separate provider key or silently switch models.

## Browser access

- Use `hermes_browser_profile_list` to find an authorized local profile and
  check its status. If it is not attached, attach with
  `hermes_browser_profile_attach` only when the user asks to use that browser.
  Then list its tabs and read a fresh snapshot before acting.
- For a managed task, use only its `hermes_task_browser_*` tools with the task
  ID. Set `browser_profile` when starting the task if it should share a
  configured profile browser; otherwise the task uses an isolated browser.
- Read a fresh snapshot before clicking or typing, and use only references from
  that snapshot. Browser attachment and page changes require `dry_run=false`
  and `confirm=true`; make a page change only when the user asked for it.
- Hermes Agent's `/browser connect` setting belongs to that agent process. It
  is not automatically visible to Hermes GPT. If the requested live browser is
  not listed, explain that an administrator must configure the same loopback
  Chromium endpoint in an authorized Hermes profile before it can be shared.
- Do not expose profile endpoints, login state, or credential material in
  replies. The server only accepts explicitly allowed local browser profiles.

## Delegation

Use `hermes_fleet_*` tools for bounded work on another configured Hermes peer
when those tools are available on the active connection. The curated sessions
plugin does not register peer-routing tools. If remote execution is needed but
those tools are unavailable, explain that it requires a separately authorized
Operator connection; keep same-host session and browser control on this
connection so it stays bound to the selected local profile.

## When a request fails

Use `hermes_request_diagnostics` to inspect the recent call sequence or filter
by the `hermes_request_id` in MCP result metadata. Keep the job ID when a turn
was accepted. Distinguish bridge rejection, a running job, a failed Agent turn,
and an Agent answer reporting an unavailable integration. Report the failure
and next useful action. Never resubmit a running request to obtain its status.
These diagnostics omit request bodies, private answers, credentials, and URLs.
