# Hermes Sessions plugin

This package adds session and browser workflows for the Hermes GPT MCP
connection. The connection itself must already be registered in ChatGPT
Developer Mode and reachable through the private tunnel.

Regular session tools run Hermes with the selected profile's configuration,
provider authentication, tools, MCP servers, skills, and session data. The
plugin does not need a separate provider API key. Confined workspace tasks
clone those supported profile resources into private task state; see the
repository's managed-session guide for their workspace and browser boundaries.

Build an account-bound package from the repository root:

```bash
python tools/build_chatgpt_sessions_plugin.py \
  --app-id 'plugin_asdk_app_<id-from-chatgpt>' \
  --output dist-plugin/hermes-sessions
```

The generated `.app.json` stays in the git-ignored `dist-plugin/` directory.
The builder refuses to replace an existing output directory; choose a new path
for another registered app.
Install that output folder through a local plugin marketplace on the machine
running ChatGPT Desktop. Follow the [OpenAI plugin packaging guide](https://developers.openai.com/plugins/build/plugins)
for marketplace setup and installation.

This package does not create or run the tunnel, start Hermes GPT, or grant
Operator/browser authority. Those remain configured on the Hermes host.
