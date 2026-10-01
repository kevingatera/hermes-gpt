# Use Hermes from ChatGPT

Ask Hermes to do work from ChatGPT using its configured tools, accounts,
providers, skills, and memory. Start with an authorized profile. You can ask
for research, check an integration, inspect schedules, continue a conversation,
or operate its browser. The plugin uses the existing Hermes setup.

This is a private developer-mode connection. The package is displayed as
**Hermes**. Its existing `hermes-sessions` package ID and `sessions` server
configuration remain valid.

Live connection settings and schedule actions: [guide](chatgpt-runtime-settings.md).

## ChatGPT account requirements

Enable Developer mode under Settings > Security and login, then add the MCP
connection in ChatGPT Plugins. Check that the target account can enable the
plugin's write tools before relying on browser actions. Account and workspace
policy can affect access. See OpenAI's
[connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt)
for the current setup steps.

Hermes GPT already exposes session and browser controls through its curated
`sessions` MCP toolset. This guide connects that toolset to ChatGPT through an
OpenAI Secure MCP Tunnel. The tunnel keeps the MCP server bound to loopback; it
does not replace Hermes profile, workspace, Operator, or confirmation gates.

This setup is for one trusted ChatGPT workspace. It is not a public plugin
deployment. Public plugin distribution requires a separately deployed HTTPS MCP
endpoint and its own authentication design.

## Configure the Hermes MCP process

Run the curated MCP server over Streamable HTTP on loopback. It exposes the
Codex core tools plus the session and browser tools, without registering the
main server's broader Operator tool catalog:

```bash
export HERMES_GPT_ENABLE_CODEX=1
export HERMES_GPT_ENABLE_MCP=1
export HERMES_GPT_CODEX_TOOLSET=sessions
export HERMES_GPT_ENABLE_SESSION_SEARCH=1
export HERMES_GPT_ENABLE_SESSION_CONTROL=1
export HERMES_GPT_ENABLE_SCOPED_TASKS=1

python -m hermes_gpt mcp --http --host 127.0.0.1 --port 4751
```

For a persistent service, put these values in its private service environment.
Do not put tunnel runtime credentials or Hermes provider keys in this process
command or in the plugin manifest.

Authorize only a dedicated Hermes profile. The example profile name is
`chatgpt`; replace it if the profile you created has a different name:

```bash
export HERMES_GPT_SESSION_CONTROL_ALLOWED_PROFILES=chatgpt
export HERMES_GPT_OPERATOR_ALLOWED_PROFILES=chatgpt
```

Normal `hermes_session_*` runs use the selected profile's Hermes configuration,
including its configured provider, authentication, tools, MCP servers, skills,
SOUL, memory, and session data. Hermes GPT lets Hermes resolve those resources
and does not require a separate provider API key for the plugin. Omit `model`
and `reasoning_effort` to use the profile's configured defaults; pass either
only for a requested per-turn override.

Secure MCP Tunnel still uses its OpenAI runtime key to carry MCP traffic. That
key authenticates `tunnel-client` to OpenAI; Hermes uses the selected profile's
provider authentication for model calls.

## Add managed workspaces and browser access

Managed tasks are an optional confined-workspace mode. They clone the selected
profile's supported Hermes resources into a private home, including provider
configuration and enabled tool and MCP settings. They keep their own session
database and need a configured workspace, Operator workspace authority, and
working OS confinement. On Linux,
install `bwrap`; on macOS, use the supported
`sandbox-exec` confinement. Add the workspace's parent directory to the
Operator path allowlist:

```bash
export HERMES_GPT_ENABLE_RUNNER_CONFINEMENT=1
export HERMES_GPT_TASK_WORKSPACES='{"project":"/path/to/authorized/project"}'
export HERMES_GPT_TASK_BROWSER_ALLOWED_PROFILES=chatgpt

export HERMES_GPT_OPERATOR_ENABLED=1
export HERMES_GPT_OPERATOR_LEVEL=workspace
export HERMES_GPT_OPERATOR_APPLY_MODE=direct
export HERMES_GPT_OPERATOR_ALLOWED_PATHS=/path/to/authorized
```

Configure the `chatgpt` Hermes profile's `browser.cdp_url` to its local
Chromium DevTools endpoint when ChatGPT should use that profile's existing
browser. The endpoint must be on loopback. Keep the profile dedicated to this
connection: attaching grants access to its open tabs and login state. The task
uses that same browser only when `hermes_task_start` receives
`browser_profile="chatgpt"`; otherwise it creates an isolated browser.

Hermes Agent's `/browser connect` can set `BROWSER_CDP_URL` only in that agent
process. Hermes GPT reads `browser.cdp_url` from the selected profile and does
not inspect another process's environment. Hermes Agent's
`browser.use_real_profile` mode also works from a copy of the normal browser
profile, not its live tabs. For the same live browser in Hermes and ChatGPT,
configure both to use the same loopback CDP endpoint and the same dedicated
Chromium profile.

Browser attachment and page mutations require `dry_run=false` and
`confirm=true`. Read-only status, snapshots, and tab listings do not. Browser
tools can reach local and private-network pages, so keep the MCP connection
private and the profile allowlists explicit. See [managed Hermes sessions](managed-hermes-sessions.md)
for workspace, profile, and confinement details.

## Connect ChatGPT

1. Create an OpenAI Secure MCP Tunnel and configure `tunnel-client` to forward
   to `http://127.0.0.1:4751/mcp`. Follow the [private tunnel setup](openai-secure-mcp-tunnel.md)
   for tunnel creation, runtime credentials, profile validation, and the
   long-running client.
2. In ChatGPT developer mode, create an app using that tunnel. Keep it scoped
   to the intended workspace.
3. Copy the app's `plugin_asdk_app...` connection ID from the browser URL.
   It is account-specific and should stay out of the shared repository.
4. Build the account-bound plugin package from the repository root:

   ```bash
   python tools/build_chatgpt_sessions_plugin.py \
     --app-id 'plugin_asdk_app_<id-from-chatgpt>' \
     --output dist-plugin/hermes-sessions
   ```

   The generated `.app.json` contains the account-specific binding and remains
   under the git-ignored `dist-plugin/` directory. Install that output folder
   through a local plugin marketplace on the machine running ChatGPT Desktop.
5. Install the plugin in a new ChatGPT Work conversation and verify tool
   discovery before asking it to attach to a browser or start a session.

ChatGPT requires the server connection to be registered before a local plugin
package can map to it. See [OpenAI's plugin packaging guide](https://developers.openai.com/plugins/build/plugins)
for the current registration and `.app.json` mapping flow. The plugin source is
in [`plugins/hermes-sessions`](../plugins/hermes-sessions/README.md). The package
builder does not create the tunnel or the ChatGPT connection.

## Optional panel in ChatGPT

ChatGPT can render an [MCP Apps component](https://developers.openai.com/plugins/build/chatgpt-ui)
alongside a tool result. Enable `HERMES_GPT_ENABLE_CHATGPT_UI=1` on the existing
`sessions` server, restart it, and refresh its connection in ChatGPT Plugins.
Then ask "Open the Hermes panel". This calls read-only `hermes_console`.

The panel lets you choose an authorized profile and conversation, send work,
set model and effort overrides for a turn, inspect scheduled jobs, follow or
cancel a job, and read recent request diagnostics. Defaults come from the
selected Hermes profile. It uses the host's MCP connection and existing tool
permissions. It has no provider keys, direct network access, external scripts,
or persistent browser storage. A closed panel does not cancel work. Reopen it
and follow the job reference to retrieve the result.

This is an optional inline interface, not a replacement for the conversational
tools. Initial resource delivery and the host bridge can be tested locally;
verify rendering and approval behavior in the target ChatGPT account after
refreshing. Account credentials remain configured in Hermes. Changing them
through this panel is unsupported.

## Ask Hermes

Use `hermes_session_profiles` to discover an authorized profile, then
`hermes_ask(prompt=..., profile=...)`. Supply `session_id` to continue a
conversation, or omit it to start one. Model and effort overrides are optional.
The tool waits up to 20 seconds by default, with a maximum of 30. A completed
turn includes its bounded result. A pending turn includes a job ID and the next
polling action. Retrieve its result before reporting completion. Polling never
requires submitting the prompt again.

A disabled direct web, vision, or scheduling tool does not describe all of
Hermes's own abilities. The selected profile's enabled tools and configured
accounts determine what a delegated request can do. Hermes reports missing
integrations in its answer. The bridge does not promise that a mail account is
connected and does not interpret TUI slash commands as an arbitrary CLI API.
For Hermes's own tool and command behavior, see its
[tools guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)
and [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands/).

## Inspect schedules

Use `hermes_cron_list(profile=...)` or `hermes_cron_status(profile=...)`.
These are read-only and do not require `HERMES_GPT_ENABLE_CRON`, which controls
the curated planning and creation tools. `hermes_capabilities` reports cron read
profiles separately from session delegation.

By default cron reads inherit `HERMES_GPT_OPERATOR_ALLOWED_PROFILES`. To inspect
a scheduler in another profile without granting mutation authority, set
`HERMES_GPT_CRON_READ_ALLOWED_PROFILES=default,chatgpt`. An explicit empty value
denies reads. This setting grants no session, browser, or cron write access.
An unnamed job is displayed as `cron job`, never as an excerpt of its prompt.

## Diagnose a failed request

`hermes_request_diagnostics` returns the recent call sequence. It records tool
names, start and finish times, durations, outcomes, safe error codes, and job,
task, and session IDs. It excludes arguments, prompts, answers, credentials,
page URLs, and exception messages. A submitted job stores its originating
`request_id`, allowing later polling calls to be linked back to submission.

MCP SDK 2 also returns `hermes_request_id` in result metadata. Pass it as
`trace_id` to the diagnostics tool. With SDK 1, use recent records or the job's
`request_id`. Validation and thrown exceptions are recorded as failures; the
original tool failure behavior is preserved. An Agent's natural-language
answer about an unavailable account is still an answer, so inspect the result
instead of interpreting transport success as task completion.

The local JSONL log is `$HERMES_HOME/logs/hermes_gpt_requests.jsonl`, defaulting
to `~/.hermes/logs/`. Files use mode 0600 on POSIX. Rotation keeps the active
5 MB file and two backups. Diagnostic reads accept `limit=1` through `limit=100` and scan only
the last 1 MB of the active file. They describe requests that reached this MCP
server; an upstream ChatGPT or tunnel failure requires the corresponding
service logs. Keep the connection private because IDs and activity timing are
operational data.

After a server update, refresh tool discovery in ChatGPT. Rebuild an installed
local plugin package to pick up its display name and skill instructions. Server
instructions also provide the workflow when the local package is older.

## Advanced controls

- Use `hermes_session_list` and `hermes_session_read` to find and inspect an
  existing profile session. Use `hermes_session_continue` to resume it. Omit
  `model` and `reasoning_effort` to use that profile's configured settings.
- Use `hermes_session_profiles` to see which profiles this connection may use
  and their non-secret model, provider, and reasoning-effort defaults.
- Use `hermes_session_start` to start a profile session. Save its `job_id`, poll
  `hermes_session_job_status`, and retrieve the bounded answer with
  `hermes_session_job_result`.
- Use `hermes_task_start` when the user asks for a confined workspace session
  with an isolated or profile-attached browser. It clones the selected Hermes
  profile's configuration and enabled resources into its private home while
  keeping session state separate. Omit `model` and `reasoning_effort` to use
  the cloned profile's defaults; pass either only for a requested per-turn
  override. Use `hermes_task_continue` with its task ID to resume that same
  Hermes session and workspace.
- Use `hermes_browser_profile_*` to operate an authorized profile browser
  without starting a Hermes model turn. Use `hermes_task_browser_*` for the
  browser owned by a managed task.
- The curated `sessions` connection does not register peer-routing tools. Use
  `hermes_fleet_*` for bounded work on another configured Hermes peer when
  those tools are available through a separately authorized Operator
  connection. Keep direct session and browser control on this MCP connection
  so it remains attached to the selected local profile and browser.

For the available tools, bounds, and error behavior, see [Hermes session control](session-control.md)
and [managed Hermes sessions](managed-hermes-sessions.md).
