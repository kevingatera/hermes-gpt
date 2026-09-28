# Managed Hermes sessions and browser access

The optional managed-session tools let a trusted MCP client start and resume a real Hermes Agent session in an explicitly configured workspace. Each session has its own Hermes home and a browser shared with ChatGPT through a small MCP bridge. By default, the browser is isolated for that task. A task may instead attach to a local Chromium browser already configured by an explicitly allowlisted Hermes profile.

The attach option reads only `browser.cdp_url` from the selected browser profile. It accepts a loopback endpoint and does not copy the profile's config, plugins, MCP servers, cookies, or password vault into the task. The selected model's API key is read separately from `credential_profile`. The built-in Hermes `browser` toolset, terminal, raw CDP, and vault tools are not enabled.

## Configure a workspace

Install `bwrap` on Linux or use macOS `sandbox-exec`, install Hermes Agent and `agent-browser`, then configure the feature and existing Operator gates:

```bash
export HERMES_GPT_ENABLE_SCOPED_TASKS=1
export HERMES_GPT_ENABLE_RUNNER_CONFINEMENT=1
export HERMES_GPT_TASK_WORKSPACES='{"project":"/path/to/project"}'
export HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES=chatgpt-browser

export HERMES_GPT_OPERATOR_ENABLED=1
export HERMES_GPT_OPERATOR_LEVEL=workspace
export HERMES_GPT_OPERATOR_APPLY_MODE=direct
export HERMES_GPT_OPERATOR_ALLOWED_PATHS=/path/to
export HERMES_GPT_OPERATOR_ALLOWED_PROFILES=chatgpt-task,chatgpt-browser
export HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES=chatgpt-task,chatgpt-browser
```

Create a dedicated credential profile listed in both session-control and Operator profile allowlists. Put the provider API key in that profile's `.env`. Managed sessions support model IDs in `provider/model` form for the provider keys declared in `operator_session_tasks.py`; other profile keys are not copied.

To attach an existing browser, also list its Hermes profile in `HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES`, `HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES`, and `HERMES_GPT_OPERATOR_ALLOWED_PROFILES`. Set that profile's `browser.cdp_url` to its local Chromium DevTools endpoint. The MCP caller selects it with `browser_profile` when starting a task. The endpoint must use `localhost`, `127.0.0.1`, or `::1`, and cannot contain credentials or a query string. Remote browser services are not supported by this attach path. The selected profile controls which live browser the task and ChatGPT share; it does not change the task's Hermes home or model credentials.

The default model is `deepseek/deepseek-v4.1-flash` with high reasoning effort.

`HERMES_GPT_TASK_WORKSPACES` is a JSON object of up to 32 short aliases mapped to existing directories. The Operator path allowlist and denied-path policy are checked before use. MCP results show workspace aliases and directory names, not host paths.

## Start and resume a session

Enable the scoped-session feature to register `hermes_task_workspaces`, `hermes_task_list`, `hermes_task_start`, `hermes_task_continue`, `hermes_task_status`, `hermes_task_result`, and `hermes_task_cancel`.

Use `hermes_task_list` to find a managed session in a later conversation. Its bounded pages include the task ID, workspace alias, status, model, effort, browser source (`disabled`, `isolated`, or `hermes_profile`), whether the isolated browser is headed, turn count, and timestamps. The browser source identifies an attachment type without revealing the profile name. The list omits workspace paths, credential profile names, session IDs, prompts, and job output. Pass a listed task ID to `hermes_task_status` or `hermes_task_continue`.

Start a session with a workspace alias, prompt, allowed credential profile, model, reasoning effort, and optional workspace-write or headed-browser access. Omit `browser_profile` to use the task-owned isolated browser. Set `browser_profile` to an allowlisted profile to use its configured local browser. `headed_browser` applies only to isolated browsers. Starts require `dry_run=false`, `confirm=true`, Operator workspace level, and direct apply mode. Workspace access is read-only by default. A dry run reports the selected model, effort, and toolsets without launching Hermes or attaching to the browser.

Continue by task ID to resume its actual Hermes session. A follow-up can select a different model or reasoning effort for that turn. The session keeps the same workspace, private Hermes home, browser profile, and session history. Existing profile session tools such as `hermes_session_continue` also accept optional `model` and `reasoning_effort` overrides, but browser controls are available only for managed sessions started with `hermes_task_start`.

## Control the shared browser

Managed tasks expose `hermes_task_browser_status` and `hermes_task_browser_snapshot` for observation. Navigation, clicking, typing, scrolling, going back, and pressing a key require the tool call's `confirm=true` and `dry_run=false`, plus the Operator workspace/direct gates. Closing or restarting is supported for task-owned isolated browsers. A browser attached from a Hermes profile cannot be closed or restarted through the task tools.

Browser status and command output redact URL user information and common secret-bearing query or fragment parameters. Redaction is best-effort; do not pass secrets in URLs when a safer authentication method is available.

The Hermes task receives only the `hermes-gpt-browser` MCP toolset. ChatGPT receives the `hermes_task_browser_*` tools and operates the same named `agent-browser` session. An isolated session is separate from the user's ordinary Chrome profile; `headed_browser=true` opens a visible isolated browser where a display is available. Inactive isolated browser daemons expire after 24 hours. Restarting an expired or closed isolated browser creates a fresh browser context. A profile-attached browser remains the profile's live browser, including its open tabs and logged-in state.

Browser navigation can reach local and private-network addresses. Only enable this feature on the intended trusted local MCP connection. Do not expose it through a public unauthenticated server.

## Isolation and persistence

- Each task receives a private Hermes home under `profiles/<task-id>` with restrictive file permissions. Its browser descriptor is stored outside that writable home and mounted read-only into the confined session; the browser socket is a separate private directory. Hermes resumes from its private session database; it does not change the selected profile's sessions.
- Linux `bwrap` and macOS `sandbox-exec` expose the selected workspace, the read-only Hermes and plugin runtimes, the browser CLI runtime, and the task's private state. Workspace writes are enabled only for the selected task when explicitly requested.
- Hermes runs the supported `chat` subcommand with `--ignore-rules`, `--toolsets file,hermes-gpt-browser`, the selected model, and the selected reasoning effort. Its private task home contains only the browser MCP configuration created for that task. The browser bridge exposes no raw CDP or password-vault operations.
- Prompts travel over stdin, not process arguments or job metadata. Job records store a prompt length and digest, not prompt text. Standard output contains the answer; stderr stays in a separate local diagnostic file.
- Hermes writes its session ID to stderr in quiet one-shot chat mode; the job watcher records it for later turns and can recover it after a server restart. Older jobs that used a structured usage report remain readable. Process ownership is not inferred from a saved PID.

The MCP tools are the initial interface. A task panel or Hermes slash-command UI can be added later without changing the session and browser ownership model.
