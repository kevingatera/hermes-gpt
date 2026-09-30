# Hermes GPT

[![PyPI version](https://img.shields.io/pypi/v/hermes-gpt.svg)](https://pypi.org/project/hermes-gpt/)

Hermes GPT connects MCP clients such as ChatGPT and Codex to a local Hermes
Agent installation. It can read workspace files, find skills, start or continue
Hermes sessions, and control an authorized browser. You choose which tools and
profiles the client can use.

Hermes supplies the model providers, credentials, tools, memory, and profile
configuration. The connection does not need another model-provider key.

## Start locally

Python 3.10 or newer is required. Install Hermes Agent separately and configure
its profile before asking Hermes GPT to run a model.

```bash
python -m pip install hermes-gpt
hermes-gpt
```

This starts a stdio MCP server for a client that launches local processes.
To run Streamable HTTP instead:

```bash
hermes-gpt --http --host 127.0.0.1 --port 7677
```

The endpoint is `http://127.0.0.1:7677/mcp`. Keep it on loopback.

This checkout is version 0.12.0. PyPI and GitHub releases are published
separately, so check the installed version before following instructions for
newer tools. To work on this fork:

```bash
git clone https://github.com/kevingatera/hermes-gpt.git
cd hermes-gpt
python -m pip install -e '.[dev]'
hermes-gpt
```

## Connect a client

| What you want to do | Guide |
| --- | --- |
| Start and resume Hermes sessions from ChatGPT, with model and effort overrides | [ChatGPT session plugin](docs/chatgpt-sessions-plugin.md) |
| Share an authorized Hermes browser or use an isolated task browser | [Managed sessions and browser access](docs/managed-hermes-sessions.md) |
| Reach the local server privately from supported OpenAI products | [OpenAI Secure MCP Tunnel](docs/openai-secure-mcp-tunnel.md) |
| Use Codex as an MCP client or delegate a job to the Codex CLI | [Codex integration](docs/codex.md) |
| Configure bearer authentication or OAuth | [Authentication](docs/oauth.md) |
| Configure another remote client through a public HTTPS proxy | [Cloudflare Tunnel](docs/cloudflare-tunnel.md) |

A remote client cannot reach your computer's loopback address directly. The
OpenAI tunnel carries traffic to it through an outbound connection. Its runtime
credential authenticates that connection; Hermes still uses its existing
provider credentials for model calls.

## Choose what the client can do

The default tools read files, search files, and list or view skills. Memory
mutation, session history, session control, browser access, and terminal
execution require their own configuration.

For session access, use a dedicated Hermes profile and explicit profile
allowlists. Managed tasks also require an authorized workspace and working OS
confinement. They can use an isolated browser or the live browser configured by
an allowed Hermes profile. Attaching to a live browser gives access to its tabs
and login state.

Operator Mode adds workspace maintenance, jobs, missions, and delegation.
Mutations require Operator policy, the appropriate apply mode, and per-call
confirmation. Dry runs describe the proposed action before it runs. Owner Mode
is a separate emergency authority and still denies protected secret paths.
See [Operator configuration](docs/operator-mode.md) for the exact gates.

Keep the server private. Public unauthenticated Operator hosting is unsupported.
Protected commands use fixed arguments without a shell. Audit records omit raw
prompts, and Mission Control excludes transcripts and credential bodies. Work
Contract completion requires observed evidence. Swarm approval remains a human
step.

## Find the right documentation

[The documentation index](docs/README.md) links setup, operation, and contributor
guides. Detailed configuration belongs in those guides. [CHANGELOG.md](CHANGELOG.md)
records version changes; `docs/design` and `docs/releases` preserve historical
plans rather than current setup instructions.

## Contribute

Read [AGENTS.md](AGENTS.md) before editing and the
[contributor guide](docs/development/contributing.md) for ownership and checks.
The [repository cleanup plan](docs/development/repository-layout.md) describes
the package layout and migration checks.

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m build
python -m twine check dist/*
python tools/check_package_hygiene.py dist/*
```

The Python tests live in `tests/`, grouped by subsystem. The React application
and its tests live in `web/`. Release checks are in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## License

MIT. See [LICENSE](LICENSE).
