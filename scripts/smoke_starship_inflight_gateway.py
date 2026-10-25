"""Real loopback Gateway HTTP against fixture operator ledgers; no flight/model."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "JEV_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ.pop(name, None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    os.environ.update({"MISSIONOS_LLM_BACKEND": "off", "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED": "0",
        "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "0", "MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED": "0",
        "GATEWAY_API_KEY": "", "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port),
        "TASK_STORE_DB_PATH": str(args.output_dir/"tasks.db"), "MEMORY_DB_PATH": str(args.output_dir/"memory.db"),
        "AUDIT_LOG_PATH": str(args.output_dir/"audit.log")})
    from src.config.settings import reset_settings
    reset_settings()
    from src.gateway import server
    from src.gateway.starship_inflight_approval import PREFIX, get_inflight_approval_service
    from src.runtime.starship_inflight_approval import InflightApprovalLedger, InflightApprovalError
    import uvicorn

    token = "explicit-http-fixture-operator-token-not-secret"
    scope = {"commands": ["hold"], "resource_limits": {"hold_seconds": 15.}}
    registry = {"commands": ["hold", "resume"], "resource_limits": {"hold_seconds": 30.}}
    service = get_inflight_approval_service()
    entries = {}

    def make(name, expiry=60.):
        now = time.monotonic()
        ledger = InflightApprovalLedger(args.output_dir/(name+".sqlite"), signing_key=b"fixture-only-integrity-key-32bytes",
            operator_token=token, operator_id="local-http-test-operator", registry=registry)
        obs = {"state_sha256": "a"*64, "simulation_time_s": 10., "wall_time_s": now}
        request = ledger.create(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2,
            scope=scope, delta={"add_commands": ["resume"], "resource_limits": {}}, observation=obs,
            simulation_time_s=10., simulation_deadline_s=25., wall_deadline_s=now+expiry, fallback="hold")
        def snapshot():
            return {"run_active": True, "simulation_time_s": 10., "revision": 2, "scope": deepcopy(scope),
                "observation": {**obs, "wall_time_s": time.monotonic()}, "pending_request_id": "request1"}
        service.register_run(run_id=name, mission_id="mission1", vehicle_id="ship", ledger=ledger, snapshot=snapshot)
        entries[name] = ledger, request, snapshot
        return name

    gateway = server.create_missionos_gateway()
    http = uvicorn.Server(uvicorn.Config(gateway.app, host="127.0.0.1", port=port,
        log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic()+15
    while not http.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert http.started, "gateway_did_not_start"
    origin = f"http://127.0.0.1:{port}"
    gateway_key = None
    http_trace, consumption_trace = [], []

    def call(path, body=None, authenticated=True):
        headers = {"Origin": origin, "Referer": origin+PREFIX, "Sec-Fetch-Site": "same-origin",
            "X-MissionOS-Operator": "starship-inflight-v1"}
        if authenticated:
            headers["Authorization"] = "Bearer "+token
        if gateway_key:
            headers["X-API-Key"] = gateway_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(origin+path, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            result = exc.code, json.load(exc)
        http_trace.append({"path": path, "method": "POST" if body is not None else "GET",
            "request_body": body, "status": result[0], "response_body": result[1],
            "authenticated_test_operator": authenticated})
        return result

    def base(name):
        return PREFIX+"/runs/"+name+"/missions/mission1/vehicles/ship"

    def body(name):
        request = entries[name][1]
        return {"request_sha256": request["sha256"], "observed_state_sha256": request["observed_state_sha256"]}

    def consume(name):
        ledger, _, snapshot = entries[name]
        state = snapshot()
        try:
            result = ledger.consume(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2,
                scope=scope, observation=state["observation"], simulation_time_s=state["simulation_time_s"], run_active=True)
        except InflightApprovalError as exc:
            consumption_trace.append({"run_id": name, "snapshot": state, "error": str(exc)})
            raise
        consumption_trace.append({"run_id": name, "snapshot": state, "result": result})
        return result

    checks = {}
    try:
        with urllib.request.urlopen(origin+PREFIX, timeout=5) as response:
            html = response.read().decode()
            checks["operator_ui_served"] = response.status == 200 and 'id="approve"' in html and 'id="reject"' in html
        make("approve")
        status, pending = call(base("approve")+"/pending")
        checks["authenticated_pending_received"] = status == 200 and pending["resolvable"] is True
        checks["unauthenticated_pending_rejected"] = call(base("approve")+"/pending", authenticated=False)[0] == 401
        checks["other_mission_rejected"] = call(base("approve").replace("mission1", "mission2")+"/pending")[0] == 404
        code, result = call(base("approve")+"/requests/request1/approve", body("approve"))
        checks["http_approval_recorded_without_dispatch"] = code == 200 and result["executor_command_issued"] is False
        checks["independent_consumption_approved"] = consume("approve")["approved"] is True
        try:
            consume("approve")
            checks["second_consumption_rejected"] = False
        except InflightApprovalError as exc:
            checks["second_consumption_rejected"] = str(exc) == "request_already_consumed"
        checks["duplicate_http_response_rejected"] = call(base("approve")+"/requests/request1/approve", body("approve"))[0] == 409
        make("reject")
        checks["http_rejection_recorded"] = call(base("reject")+"/requests/request1/reject", body("reject"))[0] == 200
        checks["rejection_preserves_fallback"] = consume("reject")["fallback"] == "hold"
        make("expire", expiry=.1)
        time.sleep(.15)
        checks["late_http_response_rejected"] = call(base("expire")+"/requests/request1/approve", body("expire"))[0] == 409
        expired = consume("expire")
        checks["no_response_preserves_fallback"] = expired["reason"] == "expired" and expired["fallback"] == "hold"
        gateway.settings.gateway_api_key = "fixture-gateway-key-not-secret"
        checks["configured_gateway_key_not_bypassed"] = call(base("approve")+"/pending")[0] == 401
        gateway_key = "fixture-gateway-key-not-secret"
        checks["gateway_key_and_operator_bearer_both_accepted"] = call(base("approve")+"/pending")[0] == 200
    finally:
        http.should_exit = True
        thread.join(timeout=5)
        sock.close()
        for run_id in entries:
            service.unregister_run(run_id)
    from src.runtime.starship_m3_live import sources
    evidence = {"schema": "missionos.starship_m3_scope_runtime_evidence.v1",
        "source_sha256": sources(), "operator_response_source": "scripted_test_operator",
        "real_gateway_http": True, "fixture_worker_snapshot": True,
        "live_model_inference": False, "physical_execution": False,
        "cases": {}}
    for role, name in (("scope_reject", "reject"), ("scope_expire", "expire"), ("scope_replay", "approve")):
        ledger, request, snapshot = entries[name]
        evidence["cases"][role] = {"run_id": name, "ledger": ledger.status("request1"),
            "original_scope": scope, "registry": registry, "snapshot": snapshot(),
            "http": [row for row in http_trace if row["path"].startswith(base(name)+"/")],
            "consumptions": [row for row in consumption_trace if row["run_id"] == name]}
    (args.output_dir/"scope-runtime-evidence.json").write_text(json.dumps(evidence, indent=2)+"\n")
    summary = {"schema": "missionos.starship_inflight_gateway_smoke.v1", "passed": all(checks.values()),
        "checks": checks, "real_gateway_http": True, "fixture_worker_snapshot": True,
        "operator_response_source": "scripted_test_operator", "browser_interaction_verified": False,
        "sso_identity_verified": False, "model_calls": 0, "simulator_runs": 0,
        "executor_commands_issued": 0, "physical_execution": False}
    (args.output_dir/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary))
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
