# Managed Hermes sessions and browser access

The optional managed-session tools let a trusted MCP client start and resume a real Hermes Agent session in an explicitly configured workspace. Each session has its own Hermes home and browser. Hermes and ChatGPT attach to the same named browser through a small MCP bridge that exposes ordinary navigation, snapshots, clicks, typing, scrolling, history, and key presses.

This flow does not attach to an arbitrary browser window or copy the selected credential profile's config, plugins, MCP servers, browser data, or password vault. It injects only the API key for the selected model provider. The built-in Hermes `browser` toolset, terminal, raw CDP, and vault tools are not enabled.

## Configure a workspace

Install `bwrap` on Linux or use macOS `sandbox-exec`, install Hermes Agent and `agent-browser`, then configure the feature and existing Operator gates:

```bash
export HERMES_GPT_ENABLE_SCOPED_TASKS=1
export HERMES_GPT_ENABLE_RUNNER_CONFINEMENT=1
export HERMES_GPT_TASK_WORKSPACES='{"project":"/path/to/project"}'

export HERMES_GPT_OPERATOR_ENABLED=1
export HERMES_GPT_OPERATOR_LEVEL=workspace
export HERMES_GPT_OPERATOR_APPLY_MODE=direct
export HERMES_GPT_OPERATOR_ALLOWED_PATHS=/path/to
export HERMES_GPT_OPERATOR_ALLOWED_PROFILES=chatgpt-task
export HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES=chatgpt-task
```

Create a dedicated Hermes profile listed in both profile allowlists. Put the provider API key in that profile's `.env`. Managed sessions support model IDs in `provider/model` form for the provider keys declared in `operator_session_tasks.py`; the profile's other keys are not copied. The default is `deepseek/deepseek-v4.1-flash` with high reasoning effort.

`HERMES_GPT_TASK_WORKSPACES` is a JSON object of up to 32 short aliases mapped to existing directories. The Operator path allowlist and denied-path policy are checked before use. MCP results show workspace aliases and directory names, not host paths.

## Start and resume a session

Enable the scoped-session feature to register `hermes_task_workspaces`, `hermes_task_list`, `hermes_task_start`, `hermes_task_continue`, `hermes_task_status`, `hermes_task_result`, and `hermes_task_cancel`.

Use `hermes_task_list` to find a managed session in a later conversation. Its bounded pages include the task ID, workspace alias, status, model, effort, browser setting, turn count, and timestamps. It omits workspace paths, credential profile names, session IDs, prompts, and job output. Pass a listed task ID to `hermes_task_status` or `hermes_task_continue`.

Start a session with a workspace alias, prompt, allowed credential profile, model, reasoning effort, and optional workspace-write or headed-browser access. Starts require `dry_run=false`, `confirm=true`, Operator workspace level, and direct apply mode. Workspace access is read-only by default. A dry run reports the selected model, effort, and toolsets without launching Hermes or the browser.

Continue by task ID to resume its actual Hermes session. A follow-up can select a different model or reasoning effort for that turn. The session keeps the same workspace, private Hermes home, browser profile, and session history. Existing profile session tools such as `hermes_session_continue` also accept optional `model` and `reasoning_effort` overrides, but browser controls are available only for managed sessions started with `hermes_task_start`.

## Control the shared browser

Managed tasks expose `hermes_task_browser_status` and `hermes_task_browser_snapshot` for observation. Navigation, clicking, typing, scrolling, going back, pressing a key, closing, and restarting require the tool call's `confirm=true` and `dry_run=false`, plus the Operator workspace/direct gates.

The Hermes task receives only the `hermes-gpt-browser` MCP toolset. ChatGPT receives the `hermes_task_browser_*` tools and operates the same named `agent-browser` session. This shared session is separate from the user's ordinary Chrome profile; `headed_browser=true` opens a visible isolated browser where a display is available. Inactive browser daemons expire after 24 hours. Restarting an expired or closed browser creates a fresh browser context.

Browser navigation can reach local and private-network addresses. Only enable this feature on the intended trusted local MCP connection. Do not expose it through a public unauthenticated server.

## Isolation and persistence

- Each task receives a private Hermes home under `profiles/<task-id>` with restrictive file permissions. Its browser descriptor is stored outside that writable home and mounted read-only into the confined session; the browser socket is a separate private directory. Hermes resumes from its private session database; it does not change the selected profile's sessions.
- Linux `bwrap` and macOS `sandbox-exec` expose the selected workspace, the read-only Hermes and plugin runtimes, the browser CLI runtime, and the task's private state. Workspace writes are enabled only for the selected task when explicitly requested.
- Hermes runs the supported `chat` subcommand with `--ignore-rules`, `--toolsets file,hermes-gpt-browser`, the selected model, and the selected reasoning effort. Its private task home contains only the browser MCP configuration created for that task. The browser bridge exposes no raw CDP or password-vault operations.
- Prompts travel over stdin, not process arguments or job metadata. Job records store a prompt length and digest, not prompt text. Standard output contains the answer; stderr stays in a separate local diagnostic file.
- Hermes writes its session ID to stderr in quiet one-shot chat mode; the job watcher records it for later turns and can recover it after a server restart. Older jobs that used a structured usage report remain readable. Process ownership is not inferred from a saved PID.

The MCP tools are the initial interface. A task panel or Hermes slash-command UI can be added later without changing the session and browser ownership model.
