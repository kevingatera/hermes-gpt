---
name: hermes-control
description: Start, resume, and manage Hermes Agent sessions and operate authorized Hermes browsers through Hermes GPT.
---

Use the Hermes GPT tools when the user asks to control Hermes sessions or its
authorized browsers.

## Sessions

- Treat regular Hermes session IDs and managed task IDs as different identifiers.
- For an existing regular session, use `hermes_session_list` and
  `hermes_session_read` to find the intended session, then call
  `hermes_session_continue` with its session ID and profile.
- Start a regular profile session with `hermes_session_start`. Save its job ID,
  poll `hermes_session_job_status`, and read `hermes_session_job_result`. Use
  the session ID returned in job status for later turns; never pass its job ID
  to a continue tool.
- For a confined workspace session, use `hermes_task_start` with an authorized
  workspace alias and credential profile. Use `hermes_task_list` to find it
  later, then resume it with `hermes_task_continue` and its task ID. A task
  continues in the same workspace and Hermes session.
- Pass the requested `model` and `reasoning_effort` explicitly. If the user
  leaves them unspecified, use `deepseek/deepseek-v4.1-flash` and `high` when
  the configured profile has that provider credential. If it does not, report
  the missing credential; do not silently switch models.

## Browser access

- Use `hermes_browser_profile_list` to find an authorized local profile, then
  check its status, tabs, and current snapshot before acting. Attach with
  `hermes_browser_profile_attach` only when the user asks to use that browser.
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

Use A2A when a bounded task benefits from execution by another Hermes peer.
Keep same-host session and browser control on the Hermes GPT tools so it stays
bound to the selected local profile.
