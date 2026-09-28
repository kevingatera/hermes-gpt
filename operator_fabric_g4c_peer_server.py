"""HTTP A2A endpoint for the deterministic G4-C peer service."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import ssl
from http.server import ThreadingHTTPServer
from typing import Any

import operator_fabric as base
import operator_fabric_peer_http as peer_http
from operator_fabric_g4c_peer_service import FabricPeerService
from operator_fabric_g4c_protocol import _validate_request

FabricError = base.FabricError
strict_json_loads = base.strict_json_loads


def _peer_handler_class() -> type[peer_http.PeerHandler]:
    class G4CPeerHandler(peer_http.PeerHandler):
        def do_POST(self) -> None:
            outer: Any = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > base._MAX_BODY:
                    raise FabricError(
                        "FABRIC_PAYLOAD_TOO_LARGE",
                        "A2A request has an invalid body size",
                    )
                outer = base._closed(
                    strict_json_loads(self.rfile.read(length)),
                    required={"jsonrpc", "id", "method", "params"},
                    name="A2A JSON-RPC request",
                )
                if outer["jsonrpc"] != "2.0" or outer["method"] not in {
                    "SendMessage",
                    "message/send",
                }:
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "only A2A SendMessage is accepted by Fabric peer",
                    )
                params = base._closed(
                    outer["params"],
                    required={"message"},
                    name="A2A params",
                )
                message = base._closed(
                    params["message"],
                    required={"role", "parts", "messageId", "contextId"},
                    name="A2A message",
                )
                if (
                    message["role"] != "ROLE_USER"
                    or not isinstance(message["parts"], list)
                    or len(message["parts"]) != 1
                ):
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "Fabric A2A message must contain one structured DataPart",
                    )
                raw_part = message["parts"][0]
                if (
                    not isinstance(raw_part, dict)
                    or "text" in raw_part
                    or "data" not in raw_part
                    or raw_part.get("mediaType") != "application/json"
                ):
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "Fabric accepts only structured JSON DataParts",
                    )
                part = base._closed(
                    raw_part,
                    required={"data", "mediaType"},
                    name="A2A DataPart",
                )
                request = _validate_request(part["data"])
                response = self.service.handle(
                    request,
                    self.headers.get("Authorization", ""),
                )
                context_id = request.get("dispatch_id") or message["contextId"]
                task_id = "ftask-" + hashlib.sha256(
                    f"{request['request_id']}:{request.get('attempt_id', '')}".encode()
                ).hexdigest()[:24]
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer["id"],
                        "result": {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {
                                "state": "TASK_STATE_COMPLETED",
                                "message": {
                                    "role": "ROLE_AGENT",
                                    "parts": [
                                        {
                                            "data": response,
                                            "mediaType": "application/json",
                                        }
                                    ],
                                    "messageId": "resp-" + task_id[6:],
                                    "contextId": context_id,
                                },
                            },
                        },
                    },
                )
            except FabricError as exc:
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

    return G4CPeerHandler


def peer_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hermes-gpt-fabric-peer",
        description="Run the deterministic Hermes GPT Fabric G4-C A2A peer endpoint.",
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
    base._require_secure_transport(advertised)
    service = FabricPeerService()
    server = ThreadingHTTPServer((args.host, args.port), _peer_handler_class())
    server.fabric_service = service
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
