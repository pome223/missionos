"""Runtime smoke for Mission Designer envelope-violation advisory gate.

The smoke starts a real loopback Gateway, creates a Coordinate Route with
contract-envelope violations, and verifies that execute-sitl returns a Form 2b
advisory block before PX4 mission upload can be attempted.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
import uvicorn

from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp / "computer_trajectories.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )
    os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = "1"


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


async def _main() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="mission-designer-envelope-advisory-"
    ) as tmp:
        _configure_temp_paths(Path(tmp))

        from src.config.settings import reset_settings
        from src.gateway.server import create_gateway
        from src.runtime.task_store import reset_task_store

        reset_settings()
        reset_task_store()
        gateway = create_gateway()
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            gateway.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            coordinate_route = {
                "takeoff_latitude": 0.0,
                "takeoff_longitude": 0.0,
                "dropoff_latitude": 0.001,
                "dropoff_longitude": 0.001,
                "dropoff_roof_height_agl_m": 10,
                "payload_weight_kg": 9.9,
                "wind_speed_mps": 29.9,
                "wind_direction_deg": 0,
            }
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                proposed = await _post_json(
                    client,
                    "/px4-gazebo/mission-scenarios/propose",
                    {
                        "prompt": (
                            "Design a windy multi-waypoint delivery mission "
                            "with rough terrain, low battery margin, and sensor "
                            "uncertainty."
                        ),
                        "coordinate_route": coordinate_route,
                    },
                )
                approved = await _post_json(
                    client,
                    "/px4-gazebo/mission-scenarios/approve",
                    {
                        "scenario_proposal": proposed["scenario_proposal"],
                        "validation_result": proposed["validation_result"],
                    },
                )
                prepared = await _post_json(
                    client,
                    "/px4-gazebo/mission-scenarios/prepare-sitl-execution",
                    {
                        "scenario_proposal": proposed["scenario_proposal"],
                        "validation_result": proposed["validation_result"],
                        "scenario_approval": approved["scenario_approval"],
                        "scenario_compile_result": approved["scenario_compile_result"],
                        "bounded_simulation_request": approved[
                            "bounded_simulation_request"
                        ],
                        "mission_designer_coordinate_pair_route": proposed[
                            "mission_designer_coordinate_pair_route"
                        ],
                        "summary": proposed["summary"],
                        "owner_session_id": "smoke-envelope-advisory",
                        "owner_user_id": "operator",
                    },
                )
                blocked = await client.post(
                    "/px4-gazebo/mission-scenarios/execute-sitl",
                    json={
                        "task_id": prepared["summary"]["task_id"],
                        "explicit_execution_approval": True,
                    },
                )
                if blocked.status_code != 409:
                    raise RuntimeError(
                        "expected envelope violation execute route to return "
                        f"409, got {blocked.status_code}: {blocked.text}"
                    )
                blocked_payload = blocked.json()
                task_response = await client.get(
                    f"/tasks/{prepared['summary']['task_id']}"
                )
                task_response.raise_for_status()
                stored = task_response.json()["task"]

            summary = blocked_payload["summary"]
            advisory = blocked_payload["envelope_violation_advisory"]
            if summary["upload_status"] != "blocked":
                raise RuntimeError("envelope violation did not block upload")
            if summary["px4_mission_upload_performed"] is not False:
                raise RuntimeError("envelope violation allowed mission upload")
            if advisory["schema_version"] != "envelope_violation_advisory.v1":
                raise RuntimeError("missing envelope advisory artifact")
            if advisory["mission_response_kind"] != "advisory":
                raise RuntimeError("envelope advisory did not remain advisory-only")
            if advisory["automatic_dispatch_suppressed"] is not True:
                raise RuntimeError("automatic dispatch was not suppressed")
            violation_kinds = {
                item["violation_kind"] for item in advisory["violations"]
            }
            expected = {
                "payload_weight_exceeds_contract_envelope",
                "wind_speed_exceeds_contract_envelope",
            }
            if violation_kinds != expected:
                raise RuntimeError(f"unexpected violation kinds: {violation_kinds}")
            if stored["status"] != "blocked":
                raise RuntimeError("persisted task was not blocked")
            persisted = stored["artifacts"]["envelope_violation_advisory"]
            if persisted["advisory_id"] != advisory["advisory_id"]:
                raise RuntimeError("persisted advisory mismatch")
            return {
                "mission_designer_envelope_violation_advisory_smoke_passed": True,
                "base_url": base_url,
                "task_id": prepared["summary"]["task_id"],
                "task_status": stored["status"],
                "execute_status_code": blocked.status_code,
                "upload_status": summary["upload_status"],
                "px4_mission_upload_performed": summary[
                    "px4_mission_upload_performed"
                ],
                "advisory_schema": advisory["schema_version"],
                "causal_form": advisory["causal_form"],
                "form2_subtype": advisory["form2_subtype"],
                "trigger_level": advisory["trigger_level"],
                "mission_response_kind": advisory["mission_response_kind"],
                "operator_review_required": advisory["operator_review_required"],
                "automatic_dispatch_suppressed": advisory[
                    "automatic_dispatch_suppressed"
                ],
                "execution_upload_blocked": advisory["execution_upload_blocked"],
                "blocked_reasons": summary["blocked_reasons"],
                "violation_kinds": sorted(violation_kinds),
                "delivery_completion_claimed": advisory[
                    "delivery_completion_claimed"
                ],
                "payload_dropoff_success_claimed": advisory[
                    "payload_dropoff_success_claimed"
                ],
                "hardware_target_allowed": summary["hardware_target_allowed"],
                "physical_execution_invoked": summary["physical_execution_invoked"],
                "advisory_persisted": True,
                "environment_limitations": [
                    "Gateway ran on a loopback port with temporary stores",
                    "PX4/Gazebo was intentionally not reached because the pre-upload envelope gate blocked first",
                ],
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)


if __name__ == "__main__":
    smoke_summary = asyncio.run(_main())
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
