"""Runtime smoke for the Mission Designer execute-SITL Gateway route (#486).

This smoke exercises the Gateway route and TaskStore persistence with the opt-in
gate intentionally unset. The route must attach a blocked SITL upload receipt
through the existing mission-upload receipt builder without starting Docker,
opening MAVLink sockets, or dispatching a mission.
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
    os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)


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
    with tempfile.TemporaryDirectory(prefix="mission-designer-execute-sitl-") as tmp:
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
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                proposed = await _post_json(
                    client,
                    "/px4-gazebo/mission-scenarios/propose",
                    {
                        "prompt": (
                            "３０００メートルの山の山頂に重さ５キロの水を届ける"
                            "ミッションを作成して"
                        )
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
                        "owner_session_id": "smoke-mission-designer",
                        "owner_user_id": "operator",
                    },
                )
                missing_approval = await client.post(
                    "/px4-gazebo/mission-scenarios/execute-sitl",
                    json={
                        "task_id": prepared["summary"]["task_id"],
                        "explicit_execution_approval": False,
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
                        f"expected non-opt-in execute route to return 409, got {blocked.status_code}"
                    )
                blocked_payload = blocked.json()
                task_response = await client.get(
                    f"/tasks/{prepared['summary']['task_id']}"
                )
                task_response.raise_for_status()
                stored = task_response.json()["task"]

            if missing_approval.status_code != 400:
                raise RuntimeError("missing explicit approval was not rejected")
            summary = blocked_payload["summary"]
            receipt = blocked_payload["px4_gazebo_sitl_mission_upload_receipt"]
            execution_result = blocked_payload[
                "px4_gazebo_mission_designer_sitl_execution_result"
            ]
            if blocked_payload["sitl_execution_opted_in"] is not False:
                raise RuntimeError("non-opt-in route reported opt-in")
            if summary["upload_status"] != "blocked":
                raise RuntimeError("non-opt-in route did not block upload")
            if (
                "SITL mission upload requires explicit opt-in"
                not in summary["blocked_reasons"]
            ):
                raise RuntimeError("non-opt-in blocked reason missing")
            for key in (
                "external_dispatch_performed",
                "mavlink_dispatch_performed",
                "px4_mission_upload_performed",
                "hardware_target_allowed",
                "physical_execution_invoked",
                "gazebo_entity_mutation_performed",
                "ros_dispatch_performed",
                "actuator_execution_performed",
            ):
                if summary[key] is not False or receipt[key] is not False:
                    raise RuntimeError(f"non-opt-in route weakened safety flag: {key}")
            if execution_result["result_status"] != "blocked":
                raise RuntimeError("execution result did not record blocked status")
            for key in (
                "actual_sitl_mission_upload_observed",
                "actual_sitl_flight_evidence_observed",
                "payload_release_observed",
                "payload_release_verified",
                "dropoff_verified",
                "synthetic_success_allowed",
            ):
                if execution_result[key] is not False:
                    raise RuntimeError(
                        f"non-opt-in execution result synthesized success: {key}"
                    )
            if (
                execution_result["payload_dropoff_success_requires_observed_facts"]
                is not True
            ):
                raise RuntimeError("payload/dropoff observed-fact gate missing")
            persisted_receipt = stored["artifacts"][
                "px4_gazebo_sitl_mission_upload_receipt"
            ]
            if persisted_receipt["receipt_id"] != receipt["receipt_id"]:
                raise RuntimeError("persisted receipt mismatch")
            persisted_result = stored["artifacts"][
                "px4_gazebo_mission_designer_sitl_execution_result"
            ]
            if persisted_result["result_id"] != execution_result["result_id"]:
                raise RuntimeError("persisted execution result mismatch")
            return {
                "mission_designer_execute_sitl_gateway_smoke_passed": True,
                "base_url": base_url,
                "task_id": prepared["summary"]["task_id"],
                "task_status": stored["status"],
                "execute_status_code": blocked.status_code,
                "sitl_execution_opted_in": blocked_payload["sitl_execution_opted_in"],
                "opt_in_env": MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
                "upload_status": summary["upload_status"],
                "sitl_execution_result_status": execution_result["result_status"],
                "blocked_reasons": summary["blocked_reasons"],
                "failure_reasons": execution_result["failure_reasons"],
                "receipt_persisted": True,
                "execution_result_persisted": True,
                "existing_sitl_upload_receipt_builder_used": True,
                "explicit_approval_required": True,
                "missing_explicit_approval_rejected": True,
                "actual_sitl_mission_upload_observed": execution_result[
                    "actual_sitl_mission_upload_observed"
                ],
                "actual_sitl_flight_evidence_observed": execution_result[
                    "actual_sitl_flight_evidence_observed"
                ],
                "payload_release_observed": execution_result[
                    "payload_release_observed"
                ],
                "payload_release_verified": execution_result[
                    "payload_release_verified"
                ],
                "dropoff_verified": execution_result["dropoff_verified"],
                "synthetic_success_allowed": execution_result[
                    "synthetic_success_allowed"
                ],
                "external_dispatch_performed": summary["external_dispatch_performed"],
                "mavlink_dispatch_performed": summary["mavlink_dispatch_performed"],
                "px4_mission_upload_performed": summary["px4_mission_upload_performed"],
                "hardware_target_allowed": summary["hardware_target_allowed"],
                "physical_execution_invoked": summary["physical_execution_invoked"],
                "environment_limitations": [
                    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION was intentionally unset",
                    "no PX4/Gazebo Docker container was started",
                    "no MAVLink socket upload was attempted",
                ],
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)


if __name__ == "__main__":
    smoke_summary = asyncio.run(_main())
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
