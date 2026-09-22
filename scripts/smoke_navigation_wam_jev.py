"""Loopback navigation WAM HTTP and shared ADK graph smoke; all model IO is fixture.

Exercises the production HTTP prediction client and Jev response parser. It does
not run trained models, launch simulators, approve proposals, or move a vehicle.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from unittest.mock import patch

from missionos_core.prediction import prediction_digest

from src.intelligence import mission_assurance_agent as assurance
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.intelligence.missionos_mission_incident_graph import (
    run_missionos_mission_incident_graph,
)


FIXTURE_POLICY = {"policy_ref": "navigation-wam-loopback-fixture"}
FAILURE_CONDITIONS = (
    "rejected",
    "stale",
    "binding_mismatch",
    "candidate_mismatch",
    "malformed",
    "service_failure",
    "authority_claim",
)
AUTHORITY_FLAGS = (
    "approval_created",
    "dispatch_authority_created",
    "dispatch_request_sent",
    "executor_invoked",
    "physical_execution_invoked",
    "delivery_completion_claimed",
    "progress_counted",
)


def recovery_fixture():
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": {"target_x_m": 2.0, "target_y_m": 1.0},
            "action_feasibility": {
                "action": "avoid_obstacle",
                "feasibility_status": "verified_feasible",
            },
        },
        "agent_invocations": [{
            "agent_name": "missionos_runtime_recovery_agent",
            "provider": "fixture",
            "invocation_kind": "fixture",
            "model_id": "fixture-recovery",
        }],
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def fixture_forecast(request):
    """Echo binding and candidate identities, with explicit synthetic risk values."""
    return {
        "schema_version": "missionos_core_prediction.v1",
        "request_id": request["request_id"],
        "observation_id": request["observation_id"],
        "request_sha256": prediction_digest(request),
        "binding": deepcopy(request["binding"]),
        "status": "available",
        "reason": "",
        "forecasts": [{
            "option_id": option["option_id"],
            "horizon_seconds": option["horizon_seconds"],
            "risk_score": 0.8 if option["option_id"] == "avoid_obstacle" else 0.1,
            "future_state": {"source": "synthetic_navigation_fixture"},
        } for option in request["options"]],
        "verification_basis": "model_inferred",
        "approval_recorded": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
    }


@contextmanager
def navigation_fixture(
    root: Path,
    *,
    backend: str,
    wam_mode: str = "required",
    jev_mode: str = "primary",
    condition: str = "valid",
    policy=None,
):
    """Replace model transports only; keep production mode selection and graph."""
    calls = {"wam": 0, "primary": 0, "jev": 0}
    prompts = {"primary": [], "jev": []}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls["wam"] += 1
            assert self.path == "/predict"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            response = fixture_forecast(request)
            status = 200
            if condition == "rejected":
                response.update(status="unavailable", reason="fixture_rejected", forecasts=[])
            elif condition == "binding_mismatch":
                response["binding"]["model_sha256"] = "b" * 64
            elif condition == "candidate_mismatch":
                response["forecasts"][0]["option_id"] = "unbound_candidate"
            elif condition == "authority_claim":
                response["dispatch_authority_created"] = True
            elif condition == "service_failure":
                status = 503
                response = {"error": "fixture_service_failure"}
            data = b"{invalid-json" if condition == "malformed" else json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    def primary(_self, prompt):
        calls["primary"] += 1
        prompts["primary"].append(deepcopy(prompt))
        return assurance.ModelJudgment(
            output={
                "proposed_response_kind": "replan",
                "parameters": {},
                "rationale": "Fixture primary selects the bound recovery candidate.",
                "expected_outcome": "Operator reviews the candidate.",
                "uncertainty": "Fixture judgment only.",
                "operator_question": "Review the bound candidate?",
            },
            invocation_evidence={"invocation_kind": "fixture", "model_id": "fixture-primary"},
        )

    def jev(_self, payload):
        calls["jev"] += 1
        prompts["jev"].append(deepcopy(payload["state"]))
        if condition == "jev_failure":
            raise RuntimeError("fixture_jev_failure")
        return {
            "model": "fixture-jev",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": "hold",
                    "confidence": 0.9,
                    "probabilities": {
                        kind: float(kind == "hold")
                        for kind in payload["questions"]["response"]["criteria"]
                    },
                },
                "review": {"type": "choice", "choice": "bounded"},
            },
        }

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / "navigation-wam-fixture.json"
        manifest.write_text(json.dumps({
            "schema_version": "missionos_navigation_wam_config.v1",
            "backends": {backend: {
                "endpoint": f"http://127.0.0.1:{server.server_port}/predict",
                "provider_kind": "fixture",
                "binding": {
                    "model_id": "fixture-navigation-wam",
                    "model_sha256": "a" * 64,
                    "policy_sha256": prediction_digest(FIXTURE_POLICY if policy is None else policy),
                    "mission_contract": f"missionos.navigation.{backend}.v1",
                    "environment_contract": (
                        "px4_gazebo_sitl.v1" if backend == "px4"
                        else "ros2_nav2_turtlebot3_sim.v1"
                    ),
                    "input_schema": "missionos_navigation_prediction_input.v1",
                },
                "horizon_seconds": 5,
                "max_age_seconds": 30,
                "timeout_seconds": 2,
            }},
        }))
        with (
            patch.dict(os.environ, {
                "MISSIONOS_NAVIGATION_WAM_MODE": wam_mode,
                "MISSIONOS_NAVIGATION_WAM_CONFIG": str(manifest),
                "MISSIONOS_JEV_MODE": jev_mode,
                "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "1",
            }),
            patch.object(assurance._ADKJudge, "judge", primary),
            patch.object(JevAssuranceJudge, "_request", jev),
        ):
            yield {"calls": calls, "prompts": prompts, "requests": requests}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def assert_no_authority(graph):
    for key in AUTHORITY_FLAGS:
        assert graph[key] is False, (key, graph[key])
    assert graph["verifier_status"] == "not_started"


def run_case(root, *, backend, wam_mode, jev_mode, condition="valid"):
    with navigation_fixture(
        root, backend=backend, wam_mode=wam_mode, jev_mode=jev_mode, condition=condition,
    ) as fixture:
        observed_at = datetime.now(timezone.utc) - timedelta(seconds=120 if condition == "stale" else 0)
        graph = run_missionos_mission_incident_graph(
            telemetry_snapshot={
                "observed_at": observed_at.isoformat(),
                "sample_index": 1,
                "position": {"local_x_m": 0.0, "local_y_m": 0.0},
                "obstacle": {"local_avoidance_required": True},
            },
            mission_context={"task_id": "navigation-fixture", "execution_scope": "fixture"},
            recovery_policy=FIXTURE_POLICY,
            recovery_runner=lambda **_: recovery_fixture(),
            navigation_backend=backend,
        )
    assert_no_authority(graph)
    fixture["graph"] = graph
    return fixture


def verify_case(case, *, backend, wam_mode, jev_mode, condition="valid"):
    graph = case["graph"]
    calls = case["calls"]
    wam_blocked = wam_mode == "required" and condition in FAILURE_CONDITIONS
    jev_blocked = jev_mode == "primary" and condition == "jev_failure"
    blocked = wam_blocked or jev_blocked
    assert graph["graph_runtime_status"] == (
        "guardrail_blocked" if blocked else "proposal_guardrail_passed"
    ), graph
    assert calls["wam"] == int(wam_mode != "off" and condition != "stale"), calls
    assert calls["primary"] == int(not wam_blocked and jev_mode != "primary"), calls
    assert calls["jev"] == int(not wam_blocked and jev_mode != "off"), calls
    assert graph["navigation_prediction"]["mode"] == wam_mode
    if wam_blocked:
        assert graph["mission_assurance_agent_invoked"] is False
        assert graph["prediction_admission"]["status"] == "rejected"
        assert graph["decision_status"] == "operator_escalation"
    elif not jev_blocked:
        proposal = graph["mission_assurance_proposal"]
        assert proposal["proposed_response_kind"] == ("hold" if jev_mode == "primary" else "replan")
        if jev_mode == "shadow":
            shadow = proposal["model_invocation_evidence"]["jev_shadow"]
            assert shadow["used_for_decision"] is False
            assert shadow["status"] == ("failed" if condition == "jev_failure" else "observed")
        for prompts in case["prompts"].values():
            for prompt in prompts:
                uncertainty = prompt["mission_situation"]["uncertainty"]
                assert ("prediction_evidence" in uncertainty) is (wam_mode == "required")
        if wam_mode == "required":
            assert graph["prediction_admission"]["status"] == "adopted"
        if jev_mode == "shadow" and condition != "jev_failure":
            assert case["prompts"]["primary"] == case["prompts"]["jev"]
    assert_no_authority(graph)
    return {
        "backend": backend, "wam_mode": wam_mode, "jev_mode": jev_mode,
        "condition": condition, "model_io": "fixture", "calls": calls,
        "graph_status": graph["graph_runtime_status"],
        "response": graph.get("mission_assurance_response_kind"),
        "authority_created": False,
    }


async def gateway_task_case(root, *, condition):
    """Cross the real PX4 task-proposal HTTP route and persist the actual graph."""
    import httpx
    import uvicorn

    from scripts import smoke_operator_chat_mission_incident_graph as incident_fixture
    from scripts.smoke_runtime_recovery_action_feasibility_gateway import (
        _configure_temp_paths, _telemetry,
    )
    from src.config.settings import reset_settings
    from src.gateway import server as gateway_server
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    root.mkdir(parents=True, exist_ok=True)
    telemetry = _telemetry(sample_index=200, battery_percent=80.0)
    telemetry["observed_at"] = datetime.now(timezone.utc).isoformat()
    agent_result = incident_fixture._agent_result(telemetry)
    try:
        with patch.dict(os.environ):
            _configure_temp_paths(root)
            os.environ["GATEWAY_API_KEY"] = ""
            reset_settings()
            reset_task_store()
            audit._audit_logger = None
            with (
                navigation_fixture(
                    root, backend="px4", condition=condition, jev_mode="shadow",
                    policy=gateway_server._operator_recovery_proposal_policy(),
                ) as fixture,
                patch.object(gateway_server, "run_missionos_runtime_recovery_agent", lambda **_: agent_result),
            ):
                gateway = gateway_server.create_missionos_gateway()
                task_id = "navigation-wam-px4-task-fixture"
                gateway.task_store.create(
                    task_id=task_id, kind="mission_designer_sitl_execution",
                    title="Navigation WAM HTTP fixture", status="running",
                    artifacts={"missionos_runtime_recovery_agent_live_bridge": {"telemetry_snapshot": telemetry}},
                )
                port = incident_fixture._free_port()
                server = uvicorn.Server(uvicorn.Config(
                    gateway.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                ))
                serving = asyncio.create_task(server.serve())
                try:
                    await incident_fixture._wait_for_health(f"http://127.0.0.1:{port}")
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                        response = await client.post(
                            "/missionos/runtime-recovery-agent/propose-for-task",
                            json={"task_id": task_id, "requested_action": "avoid_obstacle"},
                        )
                    task = gateway.task_store.get(task_id)
                    graph = task["artifacts"]["missionos_mission_incident_graph"]
                finally:
                    server.should_exit = True
                    await serving
                accepted = condition == "valid"
                # This proposal route returns HTTP 200 for completed evaluations;
                # the graph and durable-proposal fields carry a rejected verdict.
                assert response.status_code == 200, {
                    "http_status": response.status_code, "condition": condition,
                    "graph_status": graph["graph_runtime_status"],
                    "blocking_reasons": graph["blocking_reasons"],
                }
                assert fixture["calls"] == {"wam": 1, "primary": int(accepted), "jev": int(accepted)}
                assert graph["prediction_admission"]["status"] == ("adopted" if accepted else "rejected")
                assert graph["graph_runtime_status"] == ("proposal_guardrail_passed" if accepted else "guardrail_blocked")
                assert response.json()["durable_proposal_created"] is accepted
                assert_no_authority(graph)
                return {
                    "route": "/missionos/runtime-recovery-agent/propose-for-task",
                    "condition": condition, "http_status": response.status_code,
                    "calls": fixture["calls"], "graph_status": graph["graph_runtime_status"],
                    "durable_proposal_created": accepted, "authority_created": False,
                }
    finally:
        reset_settings()
        reset_task_store()
        audit._audit_logger = None


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="missionos-navigation-wam-jev-") as directory:
        for backend in ("px4", "nav2"):
            scenarios = [
                (wam, jev, "valid")
                for wam in ("off", "shadow", "required")
                for jev in ("off", "shadow", "primary")
            ] + [("required", "primary", condition) for condition in FAILURE_CONDITIONS]
            for index, (wam_mode, jev_mode, condition) in enumerate(scenarios):
                arguments = dict(backend=backend, wam_mode=wam_mode, jev_mode=jev_mode, condition=condition)
                case = run_case(Path(directory) / backend / str(index), **arguments)
                results.append(verify_case(case, **arguments))
        gateway_results = [
            asyncio.run(gateway_task_case(Path(directory) / "gateway" / condition, condition=condition))
            for condition in ("valid", "service_failure")
        ]
    print(json.dumps({
        "runtime_boundary": "loopback WAM HTTP client, PX4 Gateway task proposal HTTP, and shared ADK mission incident graph",
        "model_io": "fixture",
        "external_model_calls": False,
        "simulator_motion_observed": False,
        "physical_execution_invoked": False,
        "results": results,
        "gateway_results": gateway_results,
    }, indent=2))


if __name__ == "__main__":
    main()
