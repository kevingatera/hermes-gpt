# Managed Hermes sessions and browser access

The optional managed-session tools let a trusted MCP client start and resume a real Hermes Agent session in an explicitly configured workspace. Each task gets a private clone of an allowlisted Hermes profile, a separate session database, and a browser shared with ChatGPT through a small MCP bridge. Hermes's `profile create --clone-all` copies the selected profile's configuration, provider settings, credentials, tools, MCP servers, skills, memory, and other supported resources while leaving the source session history behind. By default, the task browser is isolated. A task may instead attach to a local Chromium browser configured by an explicitly allowlisted Hermes profile.

The attach option reads only `browser.cdp_url` from the selected browser profile. It accepts a loopback endpoint and keeps the live browser profile in place; the task gets only the local endpoint and its own browser descriptor. The managed task's Hermes home is a clone of the selected session profile, so no separate provider key is requested or injected by the MCP tool.

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

For a managed task, select a profile listed in both session-control and Operator profile allowlists. With no `model` override, Hermes resolves the model from that profile's own configuration. It uses the profile's provider settings and authentication, including custom providers; model IDs for explicit overrides use Hermes's `provider/model` form. The MCP server does not request or inject a separate provider key.

To attach an existing browser, also list its Hermes profile in `HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES`, `HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES`, and `HERMES_GPT_OPERATOR_ALLOWED_PROFILES`. Set that profile's `browser.cdp_url` to its local Chromium DevTools endpoint. The MCP caller selects it with `browser_profile` when starting a task. The endpoint must use `localhost`, `127.0.0.1`, or `::1`, and cannot contain credentials or a query string. Remote browser services are not supported by this attach path. The selected profile controls which live browser the task and ChatGPT share; it does not change the task's Hermes home or model credentials.

`HERMES_GPT_TASK_WORKSPACES` is a JSON object of up to 32 short aliases mapped to existing directories. The Operator path allowlist and denied-path policy are checked before use. MCP results show workspace aliases and directory names, not host paths.

## Start and resume a session

Enable the scoped-session feature to register the `hermes_task_*` session controls and the `hermes_browser_profile_*` tools for authorized local browsers.

Use `hermes_task_list` to find a managed session in a later conversation. Its bounded pages include the task ID, workspace alias, status, model and effort overrides, browser source (`disabled`, `isolated`, or `hermes_profile`), whether the isolated browser is headed, turn count, and timestamps. A null model or effort means Hermes used the selected profile's configured default. The browser source identifies an attachment type without revealing the profile name. The list omits workspace paths, credential profile names, session IDs, prompts, and job output. Pass a listed task ID to `hermes_task_status` or `hermes_task_continue`.

Start a session with a workspace alias, prompt, and allowed Hermes profile. Omit `model` and `reasoning_effort` to use the profile's configured defaults; pass either only when an override is requested. Omit `browser_profile` to use the task-owned isolated browser. Set `browser_profile` to an allowlisted profile to use its configured local browser. `headed_browser` applies only to isolated browsers. Starts require `dry_run=false`, `confirm=true`, Operator workspace level, and direct apply mode. Workspace access is read-only by default. A dry run reports any explicit model or effort override and the use of the profile's configured toolsets without launching Hermes or attaching to the browser.

Continue by task ID to resume its actual Hermes session. Each turn uses the profile's configured model and effort unless that call supplies an override. The session keeps the same workspace, private Hermes home, browser profile, and session history. Existing profile session tools such as `hermes_session_continue` also accept optional `model` and `reasoning_effort` overrides. Use the direct profile tools below when ChatGPT needs to operate a configured browser without a managed task.

## Control a configured browser profile directly

The `hermes_browser_profile_*` tools let ChatGPT inspect and use an authorized local Hermes browser profile without starting a model turn. `hermes_browser_profile_list` shows only profile aliases that pass the browser, session-control, and Operator profile allowlists and have a valid local Chromium endpoint. The list never returns the endpoint or host path.

Call `hermes_browser_profile_attach` with `confirm=true` and `dry_run=false` to attach. The MCP server stores a private browser descriptor under the Hermes data root and reconnects to that profile's existing tabs and login state. This descriptor contains browser-session metadata, not cookies or a copied profile. Attach and page mutations default to dry-run. Applying them requires Operator workspace level, direct apply mode, and `confirm=true`. Status and snapshots are read-only. These tools do not close or restart the shared browser.

Direct profile tools operate the configured local Chromium browser, even when no managed task is running. They do not attach to an agent's private in-memory browser session or a remote browser service. To share the browser with a Hermes run and ChatGPT at the same time, start a managed task with `browser_profile` as described above.

## Control the shared browser

Managed tasks expose `hermes_task_browser_status`, `hermes_task_browser_snapshot`, and `hermes_task_browser_tabs` for observation. The tabs tool returns at most 20 entries with a stable tab ID, label, title, redacted URL, type, and active flag. Use `hermes_task_browser_select_tab` or `hermes_browser_profile_select_tab` to switch the shared browser to a tab by ID such as `t2` or by label. Switching changes the page targeted by later browser commands, so take a fresh snapshot after switching. Selection requires the tool call's `confirm=true` and `dry_run=false`, plus the Operator workspace/direct gates. Navigation, clicking, typing, scrolling, going back, and pressing a key use the same gates. Closing or restarting is supported for task-owned isolated browsers. A browser attached from a Hermes profile cannot be closed or restarted through the task tools.

Browser status and command output redact URL user information and common secret-bearing query or fragment parameters. Redaction is best-effort; do not pass secrets in URLs when a safer authentication method is available.

The task receives its cloned profile's enabled tools plus a per-task MCP browser bridge with `task_browser_*` tools. ChatGPT receives the `hermes_task_browser_*` tools and operates the same named `agent-browser` session. An isolated session is separate from the user's ordinary Chrome profile; `headed_browser=true` opens a visible isolated browser where a display is available. Inactive isolated browser daemons expire after 24 hours. Restarting an expired or closed isolated browser creates a fresh browser context. A profile-attached browser remains the profile's live browser, including its open tabs and logged-in state.

Browser navigation can reach local and private-network addresses. Only enable this feature on the intended trusted local MCP connection. Do not expose it through a public unauthenticated server.

## Isolation and persistence

- Each task receives a private clone under `profiles/<task-id>` with restrictive directory permissions. Its browser descriptor is stored outside that writable home and mounted read-only into the confined session; the browser socket is a separate private directory. Hermes resumes from its private session database; it does not change the selected profile's sessions. Existing task sessions created by older versions keep their session database during the one-time profile upgrade.
- Linux `bwrap` and macOS `sandbox-exec` expose the selected workspace, the read-only Hermes and plugin runtimes, the browser CLI runtime, and the task's private state. Profile-configured MCP runtime paths are checked before mounting; known credential files and protected directories are refused, while unreadable or oversized runtime trees fail closed. Python source and type stubs are allowed; a typeshed `credentials` package qualifies only when it contains a bounded, flat set of regular `.pyi` files and an `__init__.pyi`. Data files and symlinks in that package are refused. Checks cover both symlink names and targets. Ancestors of the user home or Hermes data directory are skipped, including when Hermes data lives outside the user home. Workspace writes are enabled only for the selected task when explicitly requested.
- Hermes runs its supported `chat` command with model and effort flags only for explicit per-turn overrides. Otherwise Hermes resolves both from the private clone's profile configuration. It reads profile rules, memory, skills, provider authentication, configured providers, and enabled toolsets from that clone. When task browser access is enabled, the bridge is added to that clone's MCP configuration without replacing existing servers. The task clone disables Hermes tool search so the model receives the browser bridge and the selected profile's MCP tools directly; the source profile is unchanged. The bridge exposes no raw CDP or password-vault operations.
- Prompts travel over stdin, not process arguments or job metadata. Job records store a prompt length and digest, not prompt text. Standard output contains the answer; stderr stays in a separate local diagnostic file.
- Hermes writes its session ID to stderr in quiet one-shot chat mode; the job watcher records it for later turns and can recover it after a server restart. Older jobs that used a structured usage report remain readable. Process ownership is not inferred from a saved PID.

The MCP tools are the initial interface. A task panel or Hermes slash-command UI can be added later without changing the session and browser ownership model.
