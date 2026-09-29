# Hermes session control

Hermes GPT can start a new session or send one bounded non-interactive turn to an existing Hermes session and expose its status and result as an asynchronous MCP job. Each run starts the Hermes CLI with the selected profile as `HERMES_HOME`, so Hermes loads that profile's own `config.yaml`, `.env`, authentication, tools, MCP servers, skills, SOUL, memory, and session data. Hermes GPT does not parse or copy provider credentials, and a normal profile session needs no second provider key.

## Enable locally

Session control is off and hidden by default. Before enabling it, create a dedicated Hermes profile with the tool, filesystem, and browser access intended for this MCP client. The selected profile must be named in both session-control and Operator allowlists:

```powershell
$env:HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES="chatgpt"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="chatgpt"
$env:HERMES_GPT_ENABLE_SESSION_CONTROL="1"
python server.py
```

The session-control allowlist is empty by default and does not accept `*`. The built-in `default` profile is denied unless it is explicitly listed. Do not list `default` for a remote client unless you intend to grant it the full authority of that profile. Profile selection is not an OS sandbox: the Hermes process receives the capabilities configured for that profile, so use Hermes tool restrictions and OS/container isolation to enforce filesystem and browser boundaries.

Read-only history remains separately controlled by `HERMES_GPT_ENABLE_SESSION_SEARCH=1`. Enable both when the client needs to list or inspect sessions before choosing one to continue. See [session history](session-history.md) for its four-tool read-only workflow and privacy defaults.

## Workflow

1. Call `hermes_session_start(prompt, profile="chatgpt", timeout=900)` to create a new session, or find a session ID with `hermes_session_list` when history is enabled and use `hermes_session_continue(session_id, prompt, timeout)` or its `hermes_session_send` alias.
2. Omit `model` and `reasoning_effort` to let the selected Hermes profile choose its configured defaults. Pass either value only when the user requests a per-turn override. The profile must be explicitly authorized in both allowlists.
3. Save the returned `job_id`.
4. Poll `hermes_session_job_status(job_id)` until the status is `completed`, `failed`, `timed_out`, `cancelled`, or `orphaned`. The new session ID appears in the job status after Hermes reports it.
5. Call `hermes_session_job_cancel(job_id)` to stop a running job owned by this server process.
6. Call `hermes_session_job_result(job_id)` for the bounded, redacted output.
7. Use `hermes_session_rename(session_id, title, profile)` to update its title, or `hermes_session_pin(session_id, pinned, profile)` to pin or unpin it. These commands use Hermes' session CLI, accept exact or unique-prefix IDs, and apply the same two profile allowlists. Rename titles are limited to 100 printable characters.

Metadata tools are exposed only while session control is enabled. They do not start a model call.

The continue call resolves exact or unique-prefix IDs through Hermes' existing read-only `SessionDB` API before launching anything. New sessions omit `--resume`. Both paths invoke the CLI with a fixed argument array equivalent to:

```text
hermes chat --resume <resolved-session-id> [--model <provider/model>] [--reasoning <effort>] --query-file - --oneshot -Q
hermes chat [--model <provider/model>] [--reasoning <effort>] --query-file - --oneshot -Q
```

No shell is used. The prompt travels over stdin rather than command arguments. Hermes restores the resumed session's recorded working directory using its normal CLI behavior.

## Bounds and persistence

- Prompt: maximum 65,536 characters.
- Timeout: clamped to 10–3600 seconds; default 900.
- Returned result: clamped to 500–24,000 characters.
- Concurrency: only one session-control job may run for a given session at a time.
- Cancellation: only a process still owned by this server instance can be signaled; persisted PIDs are never trusted. POSIX cancellation signals the task's process group and force-stops remaining group members after the main process exits or the three-second grace period expires.
- Events: `running`, `progress`, and terminal status events are published on the durable `session-job` topic. Progress events are emitted every 15 seconds and contain elapsed time only, never prompts or captured output; the job record remains authoritative after reconnect.
- Job metadata: stored under the Hermes data root in `session-jobs/`.
- Prompt privacy: raw prompts are not stored in metadata; only length and SHA-256 digest are retained.
- Output: stdout is captured locally for bounded result retrieval and redacted before MCP exposure. Stderr is kept in a separate local diagnostic file so process warnings do not contaminate the returned answer.
- Restart behavior: a persisted running job not owned by the current server process is marked `orphaned`; persisted PIDs are never trusted or signaled.

Session control can consume the configured provider's quota or incur provider charges. Do not enable it on an unauthenticated public endpoint, and review returned content before sharing it.

## Validation without a real model call

The automated tests include a real local child-process cancellation check and replace Hermes process launch with fakes for the other cases. They verify the fixed CLI arguments, `shell=False`, explicit profile authorization, prompt-free metadata, timeout bounds, restart reconciliation, redaction, tool registration gates, and status/result flow. The automated test suite does not resume a real Hermes session or contact a model provider.
