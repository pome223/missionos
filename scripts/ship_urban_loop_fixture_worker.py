"""CPU-only explicit HTTP model double. Never a native VLA/WAM service."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time

from src.runtime.ship_urban_loop import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("vla", "wam"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--fault", default="none")
    args = parser.parse_args()
    identity = {
        "model": args.model,
        "process_nonce": args.nonce,
        "execution_scope": "fixture",
        "ready": True,
        "native_model_loaded": False,
        "gpu_invoked": False,
    }
    print(json.dumps({"event": "fixture_worker_started", **identity}), flush=True)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send(200 if self.path == "/health" else 404, identity)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/infer" or not 0 < length < 1_000_000:
                    raise ValueError("fixture_request_size_or_path")
                request = json.loads(self.rfile.read(length))
                if request["model"] != args.model:
                    raise ValueError("fixture_model_mismatch")
                print(
                    json.dumps({"event": "fixture_request", "request": request}),
                    flush=True,
                )
                if args.fault == "timeout" and args.model == "wam":
                    time.sleep(5)
                if args.fault == "http_failure" and args.model == "wam":
                    self.send(503, {"error": "explicit fixture failure", **identity})
                    return
                response = {
                    "model": args.model,
                    "process_nonce": args.nonce,
                    "session_id": request["session_id"],
                    "request_sha256": digest(request),
                    "dispatch_allowed": False,
                    "fixture": True,
                }
                if args.model == "vla":
                    n, e, d = request["observation"]["position_ned_m"]
                    response["candidates"] = [
                        {"id": "left", "target_ned_m": [n + 20, e - 5, d]},
                        {"id": "right", "target_ned_m": [n + 20, e + 5, d]},
                    ]
                    if args.fault == "unsafe_candidate":
                        response["candidates"][0]["target_ned_m"][0] = 990
                else:
                    response["proposal_sha256"] = digest(request["proposal"])
                    response["selected_candidate_id"] = "left" if request["cycle"] == 1 else "right"
                    if args.fault == "cross_cycle" and request["cycle"] == 2:
                        response["request_sha256"] = "0" * 64
                self.send(200, response)
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {"error": str(exc), **identity})

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
