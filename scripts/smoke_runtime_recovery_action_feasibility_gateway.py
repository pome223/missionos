"""Loopback Gateway smoke for the multi-hazard action-feasibility boundary."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
import uvicorn

from src.gateway import server as gateway_server
from src.intelligence.missionos_agent_runtime import (
    plan_runtime_recovery_maneuver,
)
from src.runtime.px4_gazebo_route.action_feasibility import (
    verify_runtime_recovery_action_feasibility,
)
from src.runtime.px4_gazebo_route.hazard_state import (
    build_runtime_recovery_hazard_state,
)
from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    build_runtime_recovery_intent,
    compile_runtime_recovery_intent,
    verify_runtime_recovery_reachability,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(
        tmp / "computer_trajectories.db"
    )
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )


def _policy() -> dict[str, Any]:
    return gateway_server._operator_recovery_proposal_policy()


def _telemetry(*, sample_index: int, battery_percent: float) -> dict[str, Any]:
    return {
        "source": "fixture_multi_hazard_gateway_smoke",
        "sample_index": sample_index,
        "elapsed_seconds": 48.2 + (sample_index - 200),
        "telemetry": {"stale": False, "dropout": False},
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "distance_to_home_m": 25.0,
            "frame_id": "local_ned_xy_altitude_up",
            "source_refs": ["fixture.position"],
        },
        "battery": {
            "remaining_percent": battery_percent,
            "source_refs": ["fixture.battery"],
        },
        "wind": {
            "speed_mps": 2.0,
            "gust_mps": 3.0,
            "source_refs": ["fixture.wind"],
        },
        "terrain": {
            "terrain_clearance_m": 50.0,
            "terrain_clearance_target_m": 30.0,
            "frame_id": "amsl",
            "source_refs": ["fixture.terrain"],
        },
        "obstacle": {
            "obstacle_detected": True,
            "frame_id": "local_ned_xy_altitude_up",
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "fixture_obstacle",
                        "x_m": 30.0,
                        "y_m": 0.0,
                        "size_x_m": 4.0,
                        "size_y_m": 4.0,
                        "size_z_m": 20.0,
                        "bounds_local_xyz_m": {
                            "min_x_m": 28.0,
                            "max_x_m": 32.0,
                            "min_y_m": -2.0,
                            "max_y_m": 2.0,
                            "min_z_m": 0.0,
                            "max_z_m": 20.0,
                        },
                        "source_refs": ["fixture.obstacle_manifest"],
                    }
                ]
            },
            "conflict_assessment": {
                "local_avoidance_required": True,
                "source_refs": ["fixture.obstacle_conflict"],
                "nearest_obstacle": {
                    "obstacle_name": "fixture_obstacle",
                    "time_to_conflict_s": 120.0,
                    "source_refs": ["fixture.obstacle_manifest"],
                },
            },
        },
        "route": {
            "active_leg": {
                "from_x_m": 0.0,
                "from_y_m": 0.0,
                "to_x_m": 100.0,
                "to_y_m": 0.0,
            }
        },
        "temperature": {
            "temperature_c": 35.0,
            "battery_capacity_factor": 0.95,
            "motor_thrust_factor": 0.9,
            "source_refs": ["fixture.temperature"],
        },
        "landing_zone": {
            "safe": True,
            "source_refs": ["fixture.landing_zone"],
        },
        "recovery": {
            "performance_observation": {
                "action": "avoid_obstacle",
                "sample_count": 12,
                "duration_seconds": 20.0,
                "horizontal_distance_m": 120.0,
                "observed_horizontal_speed_mps": 6.0,
                "source_refs": [
                    "fixture.prior_bounded_offboard_maneuver"
                ],
            }
        },
    }


def _proposal(telemetry: dict[str, Any]) -> dict[str, Any]:
    policy = _policy()
    planner = plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        requested_action="avoid_obstacle",
        request_reason="fixture source-backed local conflict",
    )
    candidate = planner["recommended_candidate"]
    agent_output = {
        "strategy": "local_avoidance",
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": candidate["proposed_parameters"],
        "intent_constraints": {"maximum_duration_s": 75.0},
        "requires_human_approval": True,
    }
    intent = build_runtime_recovery_intent(agent_output=agent_output)
    compilation = compile_runtime_recovery_intent(
        intent=intent,
        candidate=candidate,
        recovery_policy=policy,
    )
    reachability = verify_runtime_recovery_reachability(
        compilation=compilation,
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
    )
    hazard_state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
    )
    feasibility = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=hazard_state,
        recovery_policy=policy,
    )
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v3",
        "proposal_id": "fixture_gateway_smoke_proposal",
        "proposal_status": "awaiting_operator_approval",
        "source_obstacle_name": "fixture_obstacle",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "recovery_intent": intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "hazard_state": hazard_state,
        "hazard_state_id": hazard_state["hazard_state_id"],
        "hazard_state_sha256": hazard_state["hazard_state_sha256"],
        "action_feasibility": feasibility,
        "action_feasibility_id": feasibility["action_feasibility_id"],
        "action_feasibility_sha256": feasibility[
            "action_feasibility_sha256"
        ],
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": candidate,
                "hazard_state": hazard_state,
                "action_feasibility": feasibility,
            }
        },
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
    }


def _artifacts(
    proposal: dict[str, Any],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    position = telemetry["position"]
    wind = telemetry["wind"]
    return {
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/missionos_fixture_action_feasibility_recovery.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_last_proposal": proposal,
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": position["local_x_m"],
            "local_y_m": position["local_y_m"],
            "altitude_above_home_m": position["altitude_above_home_m"],
            "wind_speed_mps": wind["speed_mps"],
            "heartbeat_observed": True,
            "landed": False,
        },
    }


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


async def _main() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="missionos-action-feasibility-gateway-"
    ) as tmp:
        _configure_temp_paths(Path(tmp))
        from src.config.settings import reset_settings
        from src.gateway.server import create_missionos_gateway
        from src.runtime.task_store import reset_task_store
        from src.security import audit

        reset_settings()
        reset_task_store()
        audit._audit_logger = None
        queued_requests: list[dict[str, Any]] = []

        def _fixture_runner_write(**kwargs) -> dict[str, Any]:
            queued_requests.append(deepcopy(kwargs))
            return {
                "request_status": "queued",
                "container_name": "fixture-px4-gazebo-sitl",
                "container_path": kwargs["container_path"],
                "bytes_written": 1,
            }

        gateway_server._write_missionos_auto_operator_recovery_request_to_container = (
            _fixture_runner_write
        )
        gateway = create_missionos_gateway()
        proposal_telemetry = _telemetry(
            sample_index=200,
            battery_percent=80.0,
        )
        proposal = _proposal(proposal_telemetry)
        valid_telemetry = _telemetry(
            sample_index=201,
            battery_percent=80.0,
        )
        blocked_telemetry = _telemetry(
            sample_index=201,
            battery_percent=20.1,
        )
        missing_payload_telemetry = _telemetry(
            sample_index=201,
            battery_percent=80.0,
        )
        missing_payload_telemetry["payload"] = {
            "requested_mass_kg": 1.5,
            "mass_kg": None,
            "observation_status": "configured_unverified",
            "source_refs": ["fixture.payload_request"],
        }
        calibration_telemetry = _telemetry(
            sample_index=201,
            battery_percent=80.0,
        )
        calibration_telemetry["recovery"] = {}
        calibration_telemetry["obstacle"][
            "conflict_assessment"
        ].update(
            local_avoidance_required=False,
            nearest_obstacle={},
        )
        for task_id, telemetry in (
            ("task_fixture_feasibility_valid", valid_telemetry),
            ("task_fixture_feasibility_blocked", blocked_telemetry),
            (
                "task_fixture_payload_unverified",
                missing_payload_telemetry,
            ),
            (
                "task_fixture_offboard_calibration",
                calibration_telemetry,
            ),
        ):
            gateway.task_store.create(
                task_id=task_id,
                kind="mission_designer_sitl_execution",
                title="Action feasibility Gateway smoke",
                status="running",
                artifacts=_artifacts(deepcopy(proposal), telemetry),
            )

        parameters = proposal["intent_compilation"]["compiled_parameters"]
        requested = {
            key: value
            for key, value in parameters.items()
            if key != "source_obstacle_name"
        }
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
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=10.0,
            ) as client:
                valid = await client.post(
                    "/px4-gazebo/mission-scenarios/recovery-dispatch",
                    json={
                        "task_id": "task_fixture_feasibility_valid",
                        "recovery_action": "avoid_obstacle",
                        "recovery_parameters": requested,
                        "explicit_recovery_dispatch_approval": True,
                    },
                )
                blocked = await client.post(
                    "/px4-gazebo/mission-scenarios/recovery-dispatch",
                    json={
                        "task_id": "task_fixture_feasibility_blocked",
                        "recovery_action": "avoid_obstacle",
                        "recovery_parameters": requested,
                        "explicit_recovery_dispatch_approval": True,
                    },
                )
                payload_unverified = await client.post(
                    "/px4-gazebo/mission-scenarios/recovery-dispatch",
                    json={
                        "task_id": "task_fixture_payload_unverified",
                        "recovery_action": "avoid_obstacle",
                        "recovery_parameters": requested,
                        "explicit_recovery_dispatch_approval": True,
                    },
                )
                calibration = await client.post(
                    "/px4-gazebo/mission-scenarios/recovery-dispatch",
                    json={
                        "task_id": "task_fixture_offboard_calibration",
                        "recovery_action": "calibrate_offboard",
                        "recovery_parameters": {
                            "target_x_m": 10.0,
                            "target_y_m": 0.0,
                            "target_altitude_m": 30.0,
                        },
                        "explicit_recovery_dispatch_approval": True,
                    },
                )
        finally:
            server.should_exit = True
            await server_task

        if valid.status_code != 200:
            raise RuntimeError(
                f"verified dispatch was not accepted: {valid.text}"
            )
        if blocked.status_code != 409:
            raise RuntimeError(
                f"blocked dispatch was not rejected: {blocked.text}"
            )
        if payload_unverified.status_code != 409:
            raise RuntimeError(
                "unverified payload dispatch was not rejected: "
                + payload_unverified.text
            )
        if calibration.status_code != 200:
            raise RuntimeError(
                "verified calibration was not accepted: "
                + calibration.text
            )
        if len(queued_requests) != 2:
            raise RuntimeError(
                "fixture runner must receive the verified dispatch and "
                "explicit calibration only"
            )
        valid_body = valid.json()
        blocked_body = blocked.json()
        valid_revalidation = valid_body["summary"]["proposal_revalidation"]
        blocked_revalidation = blocked_body["summary"][
            "proposal_revalidation"
        ]
        payload_unverified_body = payload_unverified.json()
        payload_unverified_revalidation = payload_unverified_body[
            "summary"
        ]["proposal_revalidation"]
        calibration_body = calibration.json()
        calibration_revalidation = calibration_body["summary"][
            "proposal_revalidation"
        ]
        return {
            "gateway_loopback_smoke_passed": True,
            "valid_http_status": valid.status_code,
            "valid_revalidation_status": valid_revalidation[
                "validation_status"
            ],
            "valid_feasibility_status": valid_revalidation[
                "dispatch_action_feasibility"
            ]["feasibility_status"],
            "blocked_http_status": blocked.status_code,
            "blocked_revalidation_status": blocked_revalidation[
                "validation_status"
            ],
            "blocked_feasibility_status": blocked_revalidation[
                "dispatch_action_feasibility"
            ]["feasibility_status"],
            "blocked_reason_observed": (
                "action_feasibility_projected_battery_reserve_negative"
                in blocked_revalidation["reasons"]
            ),
            "payload_unverified_http_status": (
                payload_unverified.status_code
            ),
            "payload_unverified_revalidation_status": (
                payload_unverified_revalidation["validation_status"]
            ),
            "payload_missing_reason_observed": (
                "action_feasibility_payload_applied_mass_missing"
                in payload_unverified_revalidation["reasons"]
            ),
            "calibration_http_status": calibration.status_code,
            "calibration_revalidation_status": (
                calibration_revalidation["validation_status"]
            ),
            "calibration_feasibility_status": (
                calibration_revalidation[
                    "dispatch_action_feasibility"
                ]["feasibility_status"]
            ),
            "fixture_runner_request_count": len(queued_requests),
            "physical_execution_invoked": False,
            "completion_claimed": False,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
