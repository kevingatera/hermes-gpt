# MCP compatibility

This guide describes the MCP tools the main server and the curated Codex server
expose. Both SDK 1.x and SDK 2.x are supported through the package floor
`mcp[cli]>=1.28.1,<3`, and an installation can move to SDK 2.x without changing
Hermes source.

## Protocol compatibility

The SDK package version and negotiated MCP protocol revision are separate.
The compatibility tests perform real HTTP `initialize` requests for legacy
revisions **2024-11-05** and **2025-11-25**, checking the exact negotiated
revision and Hermes GPT application version on both servers. The
existing subprocess stdio test also exercises **2025-06-18**.

SDK 2 adds the 2026-07-28 stateless protocol while keeping legacy client
support, and new-protocol clients skip the legacy initialization handshake.
That is SDK transport behavior, not a change to Operator authority. See the [SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/).

The shared `hermes_gpt.mcp_compat.HermesMCP` adapter preserves explicit HTTP/SSE options:
SDK 1 accepts them at construction; SDK 2 accepts them at ASGI app creation.
Both retain JSON, stateless Streamable HTTP when started with `--http` and
the existing host/origin restrictions. SDK 2 uses its public app-version
parameter; only SDK 1 needs the legacy private version assignment.

## Transport matrix

| Transport | Path | Notes |
|---|---|---|
| stdio | none | Default local mode (`hermes-gpt` or `python -m hermes_gpt`) |
| Streamable HTTP | `/mcp` | Enabled with `--http`; transport security host/origin allowlist |
| Legacy SSE | `/sse` (plus `/messages/`) | Retained for older clients |

OpenAI Secure MCP Tunnel is an external private bridge, not a fourth Hermes GPT server transport. For the recommended Hermes setup, `tunnel-client` reaches `http://127.0.0.1:4750/mcp` locally over the existing Streamable HTTP transport and carries those MCP requests through an outbound-only OpenAI tunnel. See [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).

Server transport security (`TransportSecuritySettings`) enforces an explicit
host/origin allowlist: loopback by default plus `HERMES_GPT_ALLOWED_HOSTS`
extensions and the OAuth issuer when configured. Public unauthenticated
hosting is unsupported (product invariant).

## Trusted-client authentication metadata

Every tool advertises its security scheme via MCP tool metadata
(`securitySchemes`), driven by `server.tool_meta()`:

| Config | Advertised scheme |
|---|---|
| OAuth configured (`HERMES_GPT_OAUTH_*`) | `oauth2` with the configured scope |
| Static bearer (`HERMES_GPT_BEARER_TOKEN`) | `http` / `bearer` |
| Neither | `noauth` (loopback / trusted-proxy only) |

For Secure MCP Tunnel, the baseline local hop can remain loopback/noauth while OpenAI tunnel identity and Hermes Operator policy protect separate layers. Static bearer can be added as local-hop defense in depth. Built-in OAuth requires separate browser-facing authorization-server reachability because the authorization server itself is not automatically tunneled.

## Binary embedded tool results

`hermes_export_file` returns a direct MCP `CallToolResult` containing safe structured metadata and `EmbeddedResource(BlobResourceContents)` for authorized file bytes. This uses the normal `tools/call` response content union; it is not a new transport and does not require a separate resource-read endpoint.

File export requires Operator `workspace` authority plus a non-empty `HERMES_GPT_OPERATOR_ALLOWED_PATHS`; see [Binary file export](file-export.md) for the complete confinement, size, extension, denied-path, and audit contract.

The MCP specification leaves rendering of embedded resources to the client. Hermes GPT guarantees the protocol-native blob representation and does not claim that ChatGPT, Codex, or another client will always render it as a downloadable attachment. No text/base64 fallback is emitted.

## Version advertisement

The `initialize` handshake advertises the hermes-gpt app version in
`serverInfo.version`, from `hermes_gpt.versioning.VERSION`. This lets a client detect a stale process that is still
exposing an old schema. `tests/server/test_mcp_compat.py::test_initialize_advertises_server_version`
asserts the handshake reports `hermes_gpt.versioning.VERSION` and that the pinned floor
(`2024-11-05`) remains negotiable on the running SDK.

Client notes:

- **ChatGPT (chatgpt.com connector)**: uses OAuth metadata for the
  connector flow when OAuth is configured. For private developer-mode access without a public Hermes GPT hostname, see [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).
- **Gemini Spark (consumer Custom apps)**: connects as a manually configured
  confidential client (Client ID and secret entered in the Gemini UI under
  "Advanced features → Show more") because the server advertises no
  `registration_endpoint`. Google's callback
  (`https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-<id>-<host-with-dots-as-underscores>`)
  must be allowlisted exactly. Wildcards are not accepted, and the first
  attempt is rejected so the exact value can be read from the server's HTTP
  access log. PKCE S256 is supported; the OAuth boundary is streamable HTTP
  only (`--http`). See [Gemini Spark custom app](gemini-spark.md).
- **Codex CLI**: uses stdio or streamable HTTP with the configured scheme;
  curated tool names (`hermes_extract_page` vs `hermes_web_extract`) are
  documented in `docs/codex.md`.
- **Any client showing an old or incomplete tool list**: compare
  `serverInfo.version` against the expected release, then refresh the client's
  cached tool list. See [docs/updating.md](updating.md) for the check-first
  update and cache-refresh behavior; it is the canonical guide and is not
  duplicated here.

## Package floor and regression coverage

`pyproject.toml` and `requirements.txt` allow `mcp[cli]>=1.28.1,<3`.
The minimum 1.x version is the previously documented verified SDK, rather
than the historical untested `>=1.0` metadata floor. SDK 3 is not admitted.

CI runs both SDK families on Python 3.10, 3.11 and 3.12, plus pinned 1.28.1
and 2.0.0 floor jobs. Tests inspect serialized MCP field aliases, so SDK 2's
Python snake_case attributes do not alter the expected wire contract.
The matrix covers tool inventory, annotations, result schemas, binary export,
authentication and permission gates. Codex Operator aliases return redacted content blocks without an output schema:
some callbacks return JSON objects and others return plain skill text. Their
signature omits an output schema instead of promising the original
callback's string result.
Core tools returning typed dictionaries continue to provide structured content.

Tests read MCP results through the `wire()` helper in `conftest.py`, which
serializes protocol field names and wraps SDK 1 direct-call tuples in the same
result envelope used by its HTTP handler. Hermes builds results with
those same names (`isError`, `structuredContent`), so a test never depends on
whether the installed SDK spells the Python attribute `isError` or `is_error`.

## Checking the other SDK locally

CI covers both families, but a contributor can reproduce either one in a
throwaway environment before pushing:

```sh
python -m venv .venv-sdk2 && .venv-sdk2/bin/pip install -e ".[dev]" "mcp>=2,<3"
.venv-sdk2/bin/python -m pytest -q
```

Swap the specifier for `"mcp>=1.28.1,<2"` to check SDK 1. Run the suite in both environments before claiming SDK compatibility.
