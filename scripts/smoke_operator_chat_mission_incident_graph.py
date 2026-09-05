"""Runtime smoke for shared Recovery and Mission Assurance composition.

The smoke uses fixture model judgments but the production Gateway route, real
HTTP, TaskStore, and ADK v2 Mission Incident graphs. The accepted case crosses
an explicit fixture operator approval into the post-approval continuation graph
and a fixture active-runner queue boundary. It also runs battery/GPS and
delivery-shaped inputs through the judgment graph. It invokes no PX4 simulator,
MAVLink endpoint, hardware target, or physical executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from scripts import smoke_runtime_recovery_action_feasibility_gateway as fixture


class _AssuranceJudge:
    def __init__(self, response_kind: str) -> None:
        self.response_kind = response_kind

    def judge(self, _prompt: dict[str, Any]):
        from src.intelligence.mission_assurance_agent import ModelJudgment

        return ModelJudgment(
            output={
                "proposed_response_kind": self.response_kind,
                "parameters": {},
                "rationale": "Fixture mission-level judgment.",
                "expected_outcome": "The governed mission remains bounded.",
                "uncertainty": "Fixture-only runtime smoke.",
                "operator_question": "Review the bounded Recovery proposal?",
            },
            invocation_evidence={
                "invocation_kind": "fixture",
                "model_id": "fixture-mission-assurance",
            },
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _agent_result(telemetry: dict[str, Any]) -> dict[str, Any]:
    source = fixture._proposal(telemetry)
    assessment = dict(source["runtime_recovery_agent_result"]["assessment"])
    candidate = dict(assessment["recovery_planner_tool_candidate"])
    assessment.update(
        {
            "assessment_status": "proposal_guardrail_passed",
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": dict(candidate["proposed_parameters"]),
            "recovery_intent": source["recovery_intent"],
            "intent_compilation": source["intent_compilation"],
            "reachability_verification": source["reachability_verification"],
        }
    )
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": assessment,
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "provider": "fixture_hosted_model",
                "model_id": "fixture-recovery",
                "invocation_kind": "fixture",
                "prompt_sha256": "a" * 64,
                "response_sha256": "b" * 64,
            }
        ],
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _no_action_recovery_result() -> dict[str, Any]:
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "assessment_status": "proposal_guardrail_passed",
            "selected_bounded_action": "continue",
            "proposed_parameters": {},
            "backend_action_request_allowed": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "provider": "fixture_hosted_model",
                "model_id": "fixture-recovery",
                "invocation_kind": "fixture",
            }
        ],
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _runtime_evidence(label: str, telemetry: dict[str, Any]) -> dict[str, Any]:
    stdout = json.dumps(telemetry, sort_keys=True, separators=(",", ":"))
    observed_at = datetime.now(UTC).isoformat()
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "docker_exec",
        "invocation_target": f"fixture_px4_snapshot:{label}",
        "invocation_started_at": observed_at,
        "invocation_completed_at": observed_at,
        "invocation_exit_code": 0,
        "invocation_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "invocation_stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": "",
    }


async def _main() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="missionos-operator-chat-incident-graph-") as raw:
        runtime_root = Path(raw)
        fixture._configure_temp_paths(runtime_root)

        from src.config.settings import reset_settings
        from src.gateway import server as gateway_server
        from src.intelligence.mission_assurance_agent import (
            MissionAssuranceAgent,
        )
        from src.intelligence.missionos_mission_incident_graph import (
            run_missionos_mission_incident_graph as real_graph,
        )
        from src.runtime.task_store import reset_task_store
        from src.security import audit

        reset_settings()
        reset_task_store()
        audit._audit_logger = None
        telemetry = fixture._telemetry(sample_index=200, battery_percent=80.0)
        agent_result = _agent_result(telemetry)
        def recovery_result_for_task(**kwargs):
            context = dict(kwargs.get("mission_context") or {})
            if str(context.get("task_id") or "").endswith("continue_hold"):
                return _no_action_recovery_result()
            return agent_result

        gateway_server.run_missionos_runtime_recovery_agent = recovery_result_for_task

        def run_fixture_graph(**kwargs):
            context = dict(kwargs.get("mission_context") or {})
            response_kind = (
                "hold" if str(context.get("task_id") or "").endswith("hold") else "replan"
            )
            return real_graph(
                **kwargs,
                mission_assurance_agent=MissionAssuranceAgent(_AssuranceJudge(response_kind)),
            )

        gateway_server.run_missionos_mission_incident_graph = run_fixture_graph
        queued_requests: list[dict[str, Any]] = []

        def queue_fixture_operator_request(**kwargs):
            queued_requests.append(dict(kwargs["request_payload"]))
            return {
                "request_status": "queued",
                "container_name": "fixture-px4-runner",
                "container_path": kwargs["container_path"],
                "bytes_written": len(
                    json.dumps(kwargs["request_payload"], sort_keys=True)
                ),
            }

        gateway_server._write_missionos_auto_operator_recovery_request_to_container = (
            queue_fixture_operator_request
        )
        gateway = gateway_server.create_missionos_gateway()
        for task_id in (
            "task_operator_chat_graph_accept",
            "task_operator_chat_graph_hold",
            "task_operator_chat_graph_continue_hold",
        ):
            gateway.task_store.create(
                task_id=task_id,
                kind="mission_designer_sitl_execution",
                title="Operator chat Mission Incident graph smoke",
                status="running",
                artifacts={
                    "missionos_runtime_recovery_agent_live_bridge": {
                        "telemetry_snapshot": telemetry,
                    },
                    "missionos_auto_mission_gui_dispatch_running_receipt": {
                        "operator_recovery_request_container_path": (
                            "/tmp/fixture-missionos-operator-recovery.json"
                        ),
                    },
                    "missionos_auto_mission_runtime_snapshot": {
                        "sample_index": telemetry["sample_index"],
                        "elapsed_seconds": telemetry["elapsed_seconds"],
                        "local_x_m": telemetry["position"]["local_x_m"],
                        "local_y_m": telemetry["position"]["local_y_m"],
                        "altitude_above_home_m": telemetry["position"][
                            "altitude_above_home_m"
                        ],
                        "wind_speed_mps": telemetry["wind"]["speed_mps"],
                        "heartbeat_observed": True,
                        "landed": False,
                    },
                },
            )

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
                responses = {}
                for name, task_id in (
                    ("accepted", "task_operator_chat_graph_accept"),
                    ("suppressed", "task_operator_chat_graph_hold"),
                    (
                        "continuation_suppressed",
                        "task_operator_chat_graph_continue_hold",
                    ),
                ):
                    response = await client.post(
                        "/missionos/runtime-recovery-agent/propose-for-task",
                        json={
                            "task_id": task_id,
                            "operator_instruction": ("Propose a bounded obstacle recovery"),
                            "requested_action": "avoid_obstacle",
                        },
                    )
                    response.raise_for_status()
                    responses[name] = response.json()
                stored_for_dispatch = gateway.task_store.get(
                    "task_operator_chat_graph_accept"
                )
                proposal_for_dispatch = stored_for_dispatch["artifacts"][
                    "missionos_runtime_recovery_last_proposal"
                ]
                requested_parameters = {
                    key: value
                    for key, value in proposal_for_dispatch[
                        "intent_compilation"
                    ]["compiled_parameters"].items()
                    if key != "source_obstacle_name"
                }
                dispatch_response = await client.post(
                    "/px4-gazebo/mission-scenarios/recovery-dispatch",
                    json={
                        "task_id": "task_operator_chat_graph_accept",
                        "recovery_action": "avoid_obstacle",
                        "recovery_parameters": requested_parameters,
                        "explicit_recovery_dispatch_approval": True,
                    },
                )
                if dispatch_response.status_code != 200:
                    failure = dispatch_response.json()
                    raise RuntimeError(
                        "post-approval dispatch smoke failed: "
                        + json.dumps(
                            {
                                "response_status": failure.get(
                                    "response_status"
                                ),
                                "summary": failure.get("summary"),
                            },
                            sort_keys=True,
                        )
                    )
                dispatch_payload = dispatch_response.json()
        finally:
            server.should_exit = True
            await server_task

        accepted = responses["accepted"]
        suppressed = responses["suppressed"]
        continuation_suppressed = responses["continuation_suppressed"]
        if accepted.get("durable_proposal_created") is not True:
            raise RuntimeError("accepted graph did not create a v4 proposal")
        if accepted.get("checkpoint_status") != "awaiting_operator_approval":
            raise RuntimeError("accepted graph skipped the operator checkpoint")
        if accepted.get("operator_approval_required") is not True:
            raise RuntimeError("accepted graph did not require human approval")
        if suppressed.get("durable_proposal_created") is not False:
            raise RuntimeError("Assurance HOLD created a durable proposal")
        if suppressed.get("operator_approval_required") is not False:
            raise RuntimeError("Assurance HOLD created an approval checkpoint")
        if (
            suppressed.get("missionos_mission_incident_graph", {}).get(
                "dispatch_prevented_by_mission_assurance"
            )
            is not True
        ):
            raise RuntimeError("Assurance HOLD suppression was not recorded")
        if accepted.get("dispatch_authority_created") is not False:
            raise RuntimeError("operator chat created dispatch authority")
        post_approval = dispatch_payload.get(
            "missionos_mission_incident_continuation_graph", {}
        )
        if post_approval.get("recovery_agent_rerun") is not False:
            raise RuntimeError("post-approval graph reran Recovery")
        if post_approval.get("mission_assurance_agent_rerun") is not False:
            raise RuntimeError("post-approval graph reran Mission Assurance")
        if post_approval.get("human_approval_observed") is not True:
            raise RuntimeError("post-approval graph lost explicit human approval")
        if (
            post_approval.get("action_revalidation", {}).get(
                "validation_status"
            )
            != "valid"
        ):
            raise RuntimeError("post-approval graph skipped Rules revalidation")
        if post_approval.get("executor_invoked") is not True:
            raise RuntimeError("post-approval graph skipped executor boundary")
        if post_approval.get("dispatch_request_sent") is not True:
            raise RuntimeError("fixture active-runner request was not queued")
        if post_approval.get("effect_observed") is not False:
            raise RuntimeError("queue receipt was misreported as execution effect")
        if post_approval.get("next_mission_situation_created") is not False:
            raise RuntimeError("unchanged telemetry was claimed as a next situation")
        if (
            post_approval.get("next_mission_situation", {}).get(
                "observation_status"
            )
            != "awaiting_fresh_post_dispatch_observation"
        ):
            raise RuntimeError("post-approval graph did not preserve observation wait")
        if len(queued_requests) != 1:
            raise RuntimeError("fixture executor boundary was not called exactly once")
        continuation_graph = continuation_suppressed.get(
            "missionos_mission_incident_graph", {}
        )
        if continuation_suppressed.get("durable_proposal_created") is not False:
            raise RuntimeError("prevented continuation created a durable proposal")
        if continuation_suppressed.get("operator_approval_required") is not False:
            raise RuntimeError("prevented continuation created an approval checkpoint")
        if (
            continuation_graph.get(
                "mission_continuation_prevented_by_mission_assurance"
            )
            is not True
        ):
            raise RuntimeError("Assurance continuation intervention was not recorded")
        if continuation_graph.get("dispatch_prevented_by_mission_assurance") is not False:
            raise RuntimeError("continuation intervention claimed dispatch suppression")

        play_graphs = {}
        for name, telemetry_snapshot, mission_context, recovery_policy in (
            (
                "battery_gps",
                {
                    "observed_at": "2026-09-04T00:00:00+00:00",
                    "sample_index": 21,
                    "battery": {"percentage": 0.72},
                    "gps": {
                        "gps_denied": True,
                        "position_trustworthy": False,
                    },
                },
                {
                    "task_id": "play_battery_gps_fixture",
                    "mission_phase": "post_takeoff_runtime_observation",
                    "execution_scope": "simulator",
                },
                {"policy_ref": "missionos_play_live_sitl_recovery_policy.v1"},
            ),
            (
                "delivery",
                {
                    "observed_at": "2026-09-04T00:00:01+00:00",
                    "sample_index": 22,
                    "wind": {"speed_mps": 9.0},
                    "route": {"deviation_xy_m": 20.0},
                },
                {
                    "task_id": "play_delivery_fixture",
                    "mission_kind": "pickup_dropoff_delivery",
                    "mission_phase": "outbound",
                    "execution_scope": "simulator",
                },
                {"policy_ref": "missionos_play_delivery_recovery_policy.v1"},
            ),
        ):
            play_graph = await asyncio.to_thread(
                real_graph,
                telemetry_snapshot=telemetry_snapshot,
                mission_context=mission_context,
                recovery_policy=recovery_policy,
                recovery_runner=lambda **_kwargs: _no_action_recovery_result(),
                mission_assurance_agent=MissionAssuranceAgent(
                    _AssuranceJudge("continue")
                ),
            )
            if play_graph.get("decision_status") != "no_dispatch":
                raise RuntimeError(f"{name} did not complete the shared graph")
            play_graphs[name] = play_graph

        from src.runtime.px4_gazebo_route.live_mission_assurance import (
            evaluate_live_route_deviation,
        )

        def observe_live_projection(phase: str) -> dict[str, Any]:
            sample_index = 300 if phase == "original" else 301
            snapshot = fixture._telemetry(
                sample_index=sample_index,
                battery_percent=80.0,
            )
            snapshot["observed_at"] = datetime.now(UTC).isoformat()
            return {
                "telemetry_snapshot": snapshot,
                "runtime_invocation_evidence": _runtime_evidence(
                    phase,
                    snapshot,
                ),
            }

        live_projection = await asyncio.to_thread(
            evaluate_live_route_deviation,
            task_id="task_live_projection_continue_hold",
            artifact_dir=runtime_root,
            route={
                "route_plan_id": "fixture_route",
                "altitude_min_m": 1.0,
                "altitude_max_m": 40.0,
                "min_battery_margin_pct": 25.0,
            },
            deviation={
                "phase": "route",
                "sample_index": 300,
                "deviation_xy_m": 1.0,
                "threshold_xy_m": 0.85,
            },
            available_recovery_executor_action="rtl",
            operator_preapproval_observed=True,
            telemetry_observer=observe_live_projection,
            agent=MissionAssuranceAgent(_AssuranceJudge("hold")),
            recovery_agent_runner=lambda **_kwargs: _no_action_recovery_result(),
        )
        if live_projection.get("guard_status") != "no_dispatch":
            raise RuntimeError("live projection did not accept continuation intervention")
        if (
            live_projection.get(
                "mission_continuation_prevented_by_mission_assurance"
            )
            is not True
        ):
            raise RuntimeError("live projection lost continuation intervention")
        if live_projection.get("dispatch_prevented_by_mission_assurance") is not False:
            raise RuntimeError("live projection claimed a nonexistent dispatch candidate")

        stored_accepted = gateway.task_store.get("task_operator_chat_graph_accept")
        stored_hold = gateway.task_store.get("task_operator_chat_graph_hold")
        stored_continue_hold = gateway.task_store.get(
            "task_operator_chat_graph_continue_hold"
        )
        proposal = stored_accepted["artifacts"]["missionos_runtime_recovery_last_proposal"]
        return {
            "runtime_boundary": "real_loopback_gateway_http",
            "graph_runtime": "real_google_adk_v2_workflow",
            "model_inputs": "fixture_only",
            "accepted_schema_version": proposal["schema_version"],
            "accepted_decision_status": accepted["missionos_mission_incident_graph"][
                "decision_status"
            ],
            "accepted_operator_approval_required": accepted[
                "operator_approval_required"
            ],
            "suppressed_response_kind": suppressed["missionos_mission_incident_graph"][
                "mission_assurance_response_kind"
            ],
            "suppressed_durable_proposal_created": False,
            "suppressed_operator_approval_required": suppressed[
                "operator_approval_required"
            ],
            "suppressed_graph_persisted": bool(
                stored_hold["artifacts"].get("missionos_mission_incident_graph")
            ),
            "continuation_prevented_by_assurance": continuation_graph[
                "mission_continuation_prevented_by_mission_assurance"
            ],
            "continuation_dispatch_prevented_claimed": continuation_graph[
                "dispatch_prevented_by_mission_assurance"
            ],
            "continuation_graph_persisted": bool(
                stored_continue_hold["artifacts"].get(
                    "missionos_mission_incident_graph"
                )
            ),
            "live_projection_continuation_decision": live_projection[
                "guard_status"
            ],
            "live_projection_post_suppression_reobserved": bool(
                live_projection.get("post_suppression_observation")
            ),
            "battery_gps_graph_decision": play_graphs["battery_gps"][
                "decision_status"
            ],
            "delivery_graph_decision": play_graphs["delivery"][
                "decision_status"
            ],
            "judgment_graph_approval_created": False,
            "continuation_graph_human_approval_observed": post_approval[
                "human_approval_observed"
            ],
            "continuation_graph_recovery_agent_rerun": post_approval[
                "recovery_agent_rerun"
            ],
            "continuation_graph_mission_assurance_agent_rerun": post_approval[
                "mission_assurance_agent_rerun"
            ],
            "continuation_graph_revalidation_status": post_approval[
                "action_revalidation"
            ]["validation_status"],
            "dispatch_authority_created": post_approval[
                "dispatch_authority_created"
            ],
            "dispatch_request_sent": post_approval["dispatch_request_sent"],
            "executor_invoked": post_approval["executor_invoked"],
            "command_ack_observed": post_approval["command_ack_observed"],
            "effect_observed": post_approval["effect_observed"],
            "verifier_status": post_approval["verifier_status"],
            "next_mission_situation_created": post_approval[
                "next_mission_situation_created"
            ],
            "physical_execution_invoked": False,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
