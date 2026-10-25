"""Loopback-only approval UI lifecycle for registered simulation principals.

The scripted principal can answer an existing, exact candidate-bound request;
it cannot create a model proposal, invent scope or stand in for human review.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import threading
import time
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from . import starship_final_shared as shared


@contextmanager
def loopback_gateway(directory):
    directory = Path(directory)
    directory.mkdir(exist_ok=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = {"MISSIONOS_LLM_BACKEND": "off", "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED": "0",
        "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "0", "GATEWAY_API_KEY": "", "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_PORT": str(port), "TASK_STORE_DB_PATH": str(directory/"tasks.db"),
        "MEMORY_DB_PATH": str(directory/"memory.db"), "AUDIT_LOG_PATH": str(directory/"audit.log")}
    with patch.dict(os.environ, config):
        from src.config.settings import reset_settings
        from src.gateway import server, starship_inflight_approval as routes
        import uvicorn
        reset_settings()
        service = routes.InflightApprovalService()
        with patch.object(routes, "_SERVICE", service):
            gateway = server.create_missionos_gateway()
            http = uvicorn.Server(uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
            thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
            thread.start()
            until = time.monotonic()+15.
            while not http.started and time.monotonic() < until:
                time.sleep(.01)
            try:
                shared.require(http.started, "final_gateway_start_failed")
                yield service, f"http://127.0.0.1:{port}"
            finally:
                http.should_exit = True
                thread.join(timeout=10.)
                sock.close()
                reset_settings()


def operator_http(address, context, token, *, request_id=None, body=None):
    from src.gateway.starship_inflight_approval import PREFIX
    shared.require(address.startswith("http://127.0.0.1:"), "final_operator_loopback_only")
    base = PREFIX+f"/runs/{context['run_id']}/missions/{context['mission_id']}/vehicles/{context['vehicle_id']}"
    path = base+("/pending" if request_id is None else f"/requests/{request_id}/approve")
    headers = {"Origin": address, "Referer": address+PREFIX, "Sec-Fetch-Site": "same-origin",
        "X-MissionOS-Operator": "starship-inflight-v1", "Authorization": "Bearer "+token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(address+path, data=None if body is None else shared.wire(body), headers=headers)
    try:
        with urlopen(request, timeout=3.) as response:
            return response.status, shared.parse(response.read(256*1024))
    except HTTPError as error:
        return error.code, shared.parse(error.read(256*1024))


def scripted_operator(plan, address):
    completed = set()
    expected_delta = plan["configuration"]["authority"]["allowed_scope_delta"]

    def step(host, token, transcript):
        bridge = None if host.approval_host is None else host.approval_host.bridge
        original = None if bridge is None else bridge.request
        if original is None or original["request_id"] in completed:
            return
        completed.add(original["request_id"])
        code, pending = operator_http(address, plan["context"], token)
        transcript.append({"method": "GET", "status": code, "response": pending, "real_human_review": False})
        if code != 200 or pending.get("resolvable") is not True:
            return
        current = pending["request"]
        shared.require(current["sha256"] == original["sha256"]
            and current["candidate_sha256"] == original["candidate_sha256"]
            and current["requested_scope_delta"] == expected_delta, "final_scripted_operator_request_or_scope_changed")
        body = {"request_sha256": current["sha256"], "candidate_sha256": current["candidate_sha256"],
            "observed_state_sha256": current["observed_state_sha256"]}
        code, response = operator_http(address, plan["context"], token, request_id=current["request_id"], body=body)
        transcript.append({"method": "POST", "status": code, "request_body": body,
            "response": response, "real_human_review": False})
    return step
