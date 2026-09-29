---
name: hermes-control
description: Start, resume, and manage Hermes Agent sessions and operate authorized Hermes browsers through Hermes GPT.
---

Use the Hermes GPT tools when the user asks to control Hermes sessions or its
authorized browsers.

## Sessions

- Treat regular Hermes session IDs and managed task IDs as different identifiers.
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
