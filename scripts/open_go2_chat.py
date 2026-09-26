#!/usr/bin/env python3
"""Loopback browser client for the normal, authenticated Gateway chat route.

The API key stays on the server. This client exposes only the fixed Go2 catalog,
its session-bound conversation and three read-only artifact names. Browser writes
require matching Host, Origin and an unpredictable page token. It cannot dispatch
directly to the simulator or accept a caller-selected Gateway/session identity.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlparse
import uuid

import httpx

from src.gateway.go2_delivery_chat import COMMANDS, requested


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18791")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    target = urlparse(args.gateway_url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("this simulator browser client requires a loopback Gateway")
    token = secrets.token_urlsafe(32)
    session = "go2-browser-" + uuid.uuid4().hex
    template = (
        Path(__file__).resolve().parents[1] / "src/gateway/static/go2_chat.html"
    ).read_text()
    headers = (
        {"X-API-Key": os.environ["GATEWAY_API_KEY"]} if os.environ.get("GATEWAY_API_KEY") else {}
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def proxy(self, path, payload=None):
            try:
                with httpx.Client(timeout=30.0, trust_env=False) as client:
                    response = (
                        client.post(args.gateway_url + path, json=payload, headers=headers)
                        if payload is not None
                        else client.get(args.gateway_url + path, headers=headers)
                    )
                self.send(
                    response.status_code,
                    response.content,
                    response.headers.get("content-type", "application/json"),
                )
            except httpx.HTTPError:
                self.send(502, b'{"message":"Gateway is not reachable"}')

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, b"{}")
            if self.path == "/":
                return self.send(
                    200,
                    template.replace("__PAGE_TOKEN__", token).encode(),
                    "text/html; charset=utf-8",
                )
            match = re.fullmatch(
                r"/assets/(go2_[a-f0-9]{16})/(live.jpg|delivery.mp4|result.json)", self.path
            )
            if match:
                return self.proxy(f"/missionos/go2/{match[1]}/view/{match[2]}")
            self.send(404, b"{}")

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (
                self.path != "/conversation"
                or not self.allowed_host()
                or self.headers.get("Origin") != origin
                or not secrets.compare_digest(self.headers.get("X-Go2-Token", ""), token)
            ):
                return self.send(403, b"{}")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                instruction = data.get("operator_instruction", "")
                if not isinstance(instruction, str) or (
                    instruction not in COMMANDS and not requested(instruction)
                ):
                    raise ValueError()
                payload = dict(
                    operator_instruction=instruction,
                    session_id=session,
                    missionos_client_surface="chat",
                    robot_profile="go2",
                    mission_designer_context=data.get("mission_designer_context", {}),
                    go2_scenario=data.get("go2_scenario", "baseline"),
                    go2_supervision_mode=data.get("go2_supervision_mode", "agent"),
                )
            except (ValueError, TypeError, AttributeError):
                return self.send(
                    400,
                    json.dumps(
                        {"message": "配送先の依頼、承認、開始、状況確認、中止に対応しています。"}
                    ).encode(),
                )
            self.proxy("/missionos/autonomy-conversation/run", payload)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Go2 chat: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
