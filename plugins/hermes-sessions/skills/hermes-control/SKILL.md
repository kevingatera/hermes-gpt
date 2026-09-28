---
name: hermes-control
description: Start, resume, and manage Hermes Agent sessions and operate authorized Hermes browsers through Hermes GPT.
---

Use the Hermes GPT tools when the user asks to control Hermes sessions or its
authorized browsers.

## Sessions

- Use `hermes_session_list` and `hermes_session_read` to identify the intended
  existing session before continuing it.
- Use `hermes_session_continue` with that session's ID. Pass `model` and
  `reasoning_effort` only when the user requested a choice or override.
- Use `hermes_session_start` for a new profile session. It returns a job ID;
  retain that ID, poll job status, and read the bounded job result. Do not use
  the job ID as a session ID.
- For a confined workspace session, use `hermes_task_start` and `hermes_task_continue`.
  Choose an authorized workspace alias and use the task ID to resume it.
- When a model run is requested without a different preference, use
  `deepseek/deepseek-v4.1-flash` with `high` reasoning effort when that model is
  available to the configured Hermes credentials.

## Browser access

- Check `hermes_browser_profile_list` and browser status before attaching to an
  authorized profile. Use a task browser only through its matching task ID.
- Read a fresh snapshot before clicking or typing, and use references from that
  snapshot.
- Browser attachment and page changes require `dry_run=false` and `confirm=true`.
  Make a page change only when the user asked for that action.
- Do not expose profile endpoints, login state, or credential material in
  replies. The server only accepts explicitly allowed local browser profiles.

## Delegation

Use A2A when a bounded task benefits from execution by another Hermes peer.
Keep same-host session and browser control on the Hermes GPT tools so it stays
bound to the selected local profile.
