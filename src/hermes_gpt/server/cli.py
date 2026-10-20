"""CLI orchestration for the Hermes GPT entry point.

``server`` keeps the historical ``_run_codex_mcp``, ``_run_legacy_server`` and
``main`` names as explicit wrappers and passes a :class:`ServerCliContext`
snapshot built from its own module state. This module never imports ``server``:
the dependency stays one-directional, and because the wrappers read the
``server`` globals on every call, monkeypatching ``server.build_server`` (or any
other collaborator) keeps working exactly as before the split.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ServerCliContext:
    """Collaborators and release-safety values supplied by ``server``."""

    # Composition owned by server.py.
    build_server: Callable[..., Any]
    build_codex_mcp_server: Callable[..., Any]
    build_asgi_app: Callable[..., Any]
    run_codex_mcp: Callable[[list[str]], None]
    run_legacy_server: Callable[[list[str]], None]
    register_fleet_local_card: Callable[[], None]
    # Read lazily: the ``codex`` status callback reports the current gateway.
    gateway_status: Callable[[], Any]
    auth_enabled: Callable[[], bool]
    authenticated_http_security_options: Callable[..., Any]
    is_loopback_host: Callable[[str], bool]
    eprint: Callable[[str], None]
    env_enabled: Callable[[str], bool]
    # Profiles and the unsafe-remote acknowledgement surface stay defined in
    # server.py so existing imports of those names remain authoritative.
    local_dev_profile: str
    remote_profile: str
    unsafe_remote_ack: str
    unsafe_remote_env: str


def run_codex_mcp(argv: list[str], ctx: ServerCliContext) -> None:
    parser = argparse.ArgumentParser(prog="hermes-gpt mcp", description="Run the Hermes GPT Codex MCP server.")
    parser.add_argument("--http", action="store_true", help="Run streamable HTTP instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7677)
    args = parser.parse_args(argv)
    server = ctx.build_codex_mcp_server(host=args.host, port=args.port, http=args.http)
    if not args.http:
        ctx.eprint("hermes-gpt Codex MCP server starting in stdio mode.")
        server.run(transport="stdio")
        return
    ctx.eprint(f"hermes-gpt Codex MCP server running at http://{args.host}:{args.port}/mcp")
    import uvicorn

    # No forwarded_allow_ips override: uvicorn defaults to loopback-only
    # proxy trust (or the operator-set FORWARDED_ALLOW_IPS env). A wildcard
    # here would trust client-supplied X-Forwarded-For from any peer
    # (security review t_f9925699 hardening note).
    uvicorn.run(server.streamable_http_app(), host=args.host, port=args.port, proxy_headers=True)


def run_legacy_server(argv: list[str], ctx: ServerCliContext) -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent MCP sidecar.")
    parser.add_argument("--http", action="store_true", help="Run streamable HTTP transport instead of stdio.")
    parser.add_argument("--sse", action="store_true", help="Run legacy SSE transport instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7677)
    parser.add_argument("--cert", help="Path to SSL certificate file (enables HTTPS)")
    parser.add_argument("--key", help="Path to SSL key file (enables HTTPS)")
    parser.add_argument(
        "--profile",
        choices=[ctx.local_dev_profile, ctx.remote_profile],
        default=ctx.local_dev_profile,
        help="Release safety profile. Remote mode requires authentication unless unsafe no-auth is explicitly acknowledged.",
    )
    parser.add_argument(
        ctx.unsafe_remote_ack,
        action="store_true",
        dest="unsafe_remote_ack",
        help="Allow remote profile without auth. For experiments only; not release-safe.",
    )
    args = parser.parse_args(argv)

    if args.http and args.sse:
        raise SystemExit("Choose only one of --http or --sse.")
    configured_auth = ctx.auth_enabled()
    proxy_headers, forwarded_allow_ips = ctx.authenticated_http_security_options(
        profile=args.profile,
        host=args.host,
        cert=args.cert,
        key=args.key,
        configured_auth=configured_auth,
    )
    remote_unsafe_noauth = args.unsafe_remote_ack and ctx.env_enabled(ctx.unsafe_remote_env)
    if args.profile == ctx.remote_profile and not (configured_auth or remote_unsafe_noauth):
        raise SystemExit(
            "Remote profile requires real authentication. Configure a static bearer token or confidential-client OAuth. "
            f"For temporary experiments only, pass {ctx.unsafe_remote_ack} and set {ctx.unsafe_remote_env}=1."
        )
    if args.profile == ctx.local_dev_profile and not ctx.is_loopback_host(args.host) and not configured_auth:
        ctx.eprint(
            "WARNING: local-dev profile is bound to a non-loopback host. "
            "Do not expose hermes-gpt without real authentication."
        )
    if args.profile == ctx.remote_profile and remote_unsafe_noauth and not configured_auth:
        ctx.eprint("WARNING: remote no-auth mode is explicitly unsafe and intended only for temporary experiments.")

    transport = "streamable-http" if args.http else "sse" if args.sse else "stdio"
    server = ctx.build_server(host=args.host, port=args.port, http=args.http)
    if transport == "stdio":
        ctx.eprint("hermes-gpt MCP server starting in stdio mode.")
        server.run(transport="stdio")
    else:
        path = "/mcp" if args.http else "/sse"
        ctx.eprint(f"hermes-gpt MCP server running at http://{args.host}:{args.port}{path}")

        # Run with uvicorn instead of FastMCP.run() so TLS can be enabled for
        # local-only testing when cert/key are provided.
        import uvicorn
        app = ctx.build_asgi_app(server, http=args.http)
        ctx.register_fleet_local_card()

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=args.cert if args.cert else None,
            ssl_keyfile=args.key if args.key else None,
            proxy_headers=proxy_headers,
            forwarded_allow_ips=forwarded_allow_ips,
        )


def main(argv: list[str] | None, ctx: ServerCliContext) -> None:
    """Run legacy MCP, the Codex MCP alias, or the Codex installer helpers."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "mcp":
        ctx.run_codex_mcp(args[1:])
        return
    if args and args[0] == "update":
        from hermes_gpt.workspace import updater

        updater.main(args[1:])
        return
    if args and args[0] == "codex":
        if len(args) > 1 and args[1] == "mcp":
            ctx.run_codex_mcp(args[2:])
            return
        from hermes_gpt.clients.codex import config as codex_config

        def list_tools() -> list[str]:
            return [tool.name for tool in asyncio.run(ctx.build_codex_mcp_server().list_tools())]

        def status() -> dict[str, Any]:
            try:
                data = json.loads(ctx.gateway_status())
                return {
                    "ok": bool(data.get("success")),
                    "gateway": "running" if data.get("gateway_running") else "not_running",
                    "gateway_pid_source": data.get("gateway_pid_source"),
                }
            except Exception:  # noqa: BLE001 - Diagnostics must return a stable unavailable state.
                return {"ok": False, "gateway": "unknown"}

        codex_config.main(args[1:], list_tools=list_tools, status=status)
        return
    ctx.run_legacy_server(args)
