"""HTTP transport for the bounded Fabric A2A peer protocol."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import ssl
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hermes_gpt.fleet import fabric
from hermes_gpt.fleet import fabric_protocol as protocol


class PeerHandler(BaseHTTPRequestHandler):
    """Adapt strict A2A JSON-RPC requests to the deterministic peer service."""

    server_version = "HermesGPTFabric/0.8"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def service(self) -> fabric.FabricPeerService:
        return self.server.fabric_service

    @property
    def advertised_url(self) -> str:
        return self.server.fabric_advertised_url

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = protocol.canonical_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if urllib.parse.urlparse(self.path).path not in {
            "/.well-known/agent-card.json",
            "/.well-known/agent.json",
        }:
            self._send(404, {"error": "not found"})
            return
        try:
            policy = self.service.policy_loader()
            self._send(
                200,
                {
                    "protocolVersion": "1.0",
                    "name": policy.identity,
                    "description": "Hermes GPT managed Fabric peer",
                    "version": "0.8",
                    "url": self.advertised_url,
                    "capabilities": {},
                    "defaultInputModes": ["application/json"],
                    "defaultOutputModes": ["application/json"],
                    "skills": [
                        {
                            "id": "hermes-fabric-v1",
                            "name": "Hermes Fabric v1",
                            "description": "Deterministic managed remote execution",
                            "tags": ["hermes", "fabric"],
                        }
                    ],
                    "supportedInterfaces": [
                        {
                            "url": self.advertised_url,
                            "protocolBinding": "JSONRPC",
                            "protocolVersion": "1.0",
                        }
                    ],
                },
            )
        except protocol.FabricError as exc:
            self._send(503, {"error": exc.code})

    def do_POST(self) -> None:
        outer: Any = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > protocol._MAX_BODY:
                raise protocol.FabricError(
                    "FABRIC_PAYLOAD_TOO_LARGE",
                    "A2A request has an invalid body size",
                )
            outer = protocol._closed(
                protocol.strict_json_loads(self.rfile.read(length)),
                required={"jsonrpc", "id", "method", "params"},
                name="A2A JSON-RPC request",
            )
            if outer["jsonrpc"] != "2.0" or outer["method"] not in {
                "SendMessage",
                "message/send",
            }:
                raise protocol.FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "only A2A SendMessage is accepted by the Fabric peer",
                )
            params = protocol._closed(
                outer["params"],
                required={"message"},
                name="A2A params",
            )
            message = protocol._closed(
                params["message"],
                required={"role", "parts", "messageId", "contextId"},
                name="A2A message",
            )
            if (
                message["role"] != "ROLE_USER"
                or not isinstance(message["parts"], list)
                or len(message["parts"]) != 1
            ):
                raise protocol.FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric A2A message must contain exactly one structured DataPart",
                )
            raw_part = message["parts"][0]
            if (
                not isinstance(raw_part, dict)
                or "text" in raw_part
                or "data" not in raw_part
                or raw_part.get("mediaType") != "application/json"
            ):
                raise protocol.FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric does not accept text or generic agent messages",
                )
            part = protocol._closed(
                raw_part,
                required={"data", "mediaType"},
                name="A2A DataPart",
            )
            if not isinstance(part["data"], dict):
                raise protocol.FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric DataPart must contain a structured JSON object",
                )
            request = fabric._validate_request(part["data"])
            response = self.service.handle(
                request,
                self.headers.get("Authorization", ""),
            )
            context_id = request.get("dispatch_id") or message["contextId"]
            task_id = "ftask-" + hashlib.sha256(
                f"{request['request_id']}:{request.get('attempt_id', '')}".encode()
            ).hexdigest()[:24]
            result = {
                "id": task_id,
                "contextId": context_id,
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "role": "ROLE_AGENT",
                        "parts": [{"data": response, "mediaType": "application/json"}],
                        "messageId": "resp-" + task_id[6:],
                        "contextId": context_id,
                    },
                },
            }
            self._send(200, {"jsonrpc": "2.0", "id": outer["id"], "result": result})
        except protocol.FabricError as exc:
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": outer.get("id") if isinstance(outer, dict) else None,
                    "error": {
                        "code": -32001,
                        "message": str(exc)[:300],
                        "data": {"code": exc.code},
                    },
                },
            )
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": outer.get("id") if isinstance(outer, dict) else None,
                    "error": {
                        "code": -32603,
                        "message": "internal Fabric peer error",
                        "data": {"code": "FABRIC_INTERNAL_ERROR"},
                    },
                },
            )


def peer_main(argv: list[str] | None = None) -> None:
    """Run the base Fabric peer with loopback HTTP or verified TLS."""
    parser = argparse.ArgumentParser(
        prog="hermes-gpt-fabric-peer",
        description="Run the deterministic Hermes GPT Fabric A2A peer endpoint.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4780)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--advertised-url", default="")
    args = parser.parse_args(argv)
    if bool(args.cert) != bool(args.key):
        raise SystemExit("Fabric TLS requires both --cert and --key.")
    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not (args.cert and args.key):
        raise SystemExit("Non-loopback verified Fabric requires direct TLS (--cert and --key).")
    scheme = "https" if args.cert else "http"
    advertised = args.advertised_url or f"{scheme}://{args.host}:{args.port}"
    protocol._require_secure_transport(advertised)
    server = ThreadingHTTPServer((args.host, args.port), PeerHandler)
    server.fabric_service = fabric.FabricPeerService()
    server.fabric_advertised_url = advertised
    if args.cert and args.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["PeerHandler", "peer_main"]
