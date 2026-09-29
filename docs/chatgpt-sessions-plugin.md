# ChatGPT session and browser plugin

Status: private developer-mode setup.

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

python server.py mcp --http --host 127.0.0.1 --port 4751
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

## Add managed workspaces and browser access

Managed tasks are an optional confined-workspace mode. They use a private Hermes
home and a limited toolset; they do not load the full profile tool and MCP
configuration. Use the normal profile session tools when the request needs all
capabilities configured for Hermes. Managed tasks also need a configured
workspace, Operator workspace authority, and working OS confinement. On Linux,
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

## Use the tools

- Use `hermes_session_list` and `hermes_session_read` to find and inspect an
  existing profile session. Use `hermes_session_continue` to resume it. Omit
  `model` and `reasoning_effort` to use that profile's configured settings.
- Use `hermes_session_start` to start a profile session. Save its `job_id`, poll
  `hermes_session_job_status`, and retrieve the bounded answer with
  `hermes_session_job_result`.
- Use `hermes_task_start` when the user asks for a confined workspace session
  with an isolated or profile-attached browser. Its private home and limited
  toolset are separate from a full profile session. Use `hermes_task_continue`
  with its task ID to resume that same Hermes session and workspace.
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
