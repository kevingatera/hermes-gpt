# Documentation

Start with the guide for the task you need to do. Each guide owns its setup,
configuration, and failure behavior. The root [README](../README.md) gives the
project overview.

## Connect and configure

| Guide | Use it for |
| --- | --- |
| [Hermes in ChatGPT](chatgpt-sessions-plugin.md) | conversation and optional panel for Hermes work, schedules, and results |
| [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md) | outbound access to a loopback MCP server |
| [Authentication](oauth.md) | bearer tokens, OAuth, refresh rotation, and revocation |
| [Codex](codex.md) | Codex as an MCP client and the separate Codex CLI worker |
| [Gemini Spark](gemini-spark.md) | an optional additional OAuth client |
| [MCP compatibility](mcp-compatibility.md) | supported SDKs, transports, and protocol checks |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | HTTPS proxy setup and its authentication boundary |
| [Windows deployment](windows-chatgpt-codex.md) | ChatGPT and delegated Codex CLI jobs on Windows |

## Sessions and files

| Guide | Use it for |
| --- | --- |
| [Managed Hermes sessions](managed-hermes-sessions.md) | scoped tasks, model and effort choices, shared browser controls |
| [Session control](session-control.md) | asynchronous start, continue, send, rename, and pin jobs |
| [Session history](session-history.md) | bounded reads and exports of private session data |
| [File export](file-export.md) | binary transfers, size limits, and denied paths |
| [Updating](updating.md) | checking and updating Git or PyPI installations |
| [Retention](retention-policy.md) | local diagnostic cleanup |
| [Runtime checkout](runtime-checkout.md) | verifying which checkout a running service uses |

## Operator and web application

| Guide | Use it for |
| --- | --- |
| [Operator Mode](operator-mode.md) | policy, Owner authority, diagnostics, contracts, swarms, and fleet execution |
| [Missions](missions.md) | mission lifecycle, context, attachments, and approval |
| [Capabilities and mission ledger](capabilities-and-mission-ledger.md) | derived capability records and replayable mission events |
| [Delegations](delegations.md) | worker jobs, lineage, cancellation, and reconciliation |
| [Live events](live-events.md) | durable cursors, polling, and WebSocket notifications |
| [Finance](finance.md) | local finance evidence and decisions |
| [Flight Deck missions](flight-deck-missions.md) | mission views and live refresh |
| [Flight Deck coverage](flight-deck-coverage.md) | browser checks and mutation decisions |
| [UI security](ui-security-boundary.md) | browser access and the optional UI mount |

## Development

Read [AGENTS.md](../AGENTS.md) for repository rules, the
[contributor guide](development/contributing.md) for ownership and checks, and
[the cleanup plan](development/repository-layout.md) for the directory migration.
[RELEASE_CHECKLIST.md](../RELEASE_CHECKLIST.md)
covers packaging and publication checks. The package version and distributed
files are declared in `pyproject.toml`.

Verify a claim against the current implementation and tests first, then the
operational guide. Release notes describe their version. They do not override
current code, and a repository version does not prove a package is on PyPI.

Tool names depend on the active MCP server. For example, the main server uses
`hermes_web_extract`; the curated Codex server uses `hermes_extract_page`.
Inspect tool registration before copying a call between clients.

## Historical and internal artifacts

[CHANGELOG.md](../CHANGELOG.md) and the `release-notes-*` files record past
versions. [Design documents](design/README.md), [release plans](releases/README.md),
and `FEASIBILITY.md` preserve earlier decisions and experiments. Read them for
rationale, then check the current code before treating a statement as a runtime
contract. Keep private host details, credentials, prompts, and transcripts out
of shared documentation.
