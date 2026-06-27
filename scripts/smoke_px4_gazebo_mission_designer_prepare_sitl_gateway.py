"""Runtime smoke for the Mission Designer prepare-SITL Gateway route (#484)."""

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

from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
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
    with tempfile.TemporaryDirectory(prefix="mission-designer-sitl-gateway-") as tmp:
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
                weakened_approval = dict(approved["scenario_approval"])
                weakened_approval["approved_for_gazebo_execution"] = True
                unsafe_response = await client.post(
                    "/px4-gazebo/mission-scenarios/prepare-sitl-execution",
                    json={
                        "scenario_proposal": proposed["scenario_proposal"],
                        "validation_result": proposed["validation_result"],
                        "scenario_approval": weakened_approval,
                        "scenario_compile_result": approved["scenario_compile_result"],
                        "bounded_simulation_request": approved[
                            "bounded_simulation_request"
                        ],
                    },
                )
                task_response = await client.get(
                    f"/tasks/{prepared['summary']['task_id']}"
                )
                task_response.raise_for_status()
                stored = task_response.json()["task"]

            request = prepared["sitl_execution_request"]
            persisted_request = stored["artifacts"][
                "px4_gazebo_mission_designer_sitl_execution_request"
            ]
            if unsafe_response.status_code != 400:
                raise RuntimeError("unsafe execution approval was not rejected")
            if request["target_endpoint"] != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
                raise RuntimeError("prepared request target endpoint drifted")
            if (
                persisted_request["execution_request_id"]
                != request["execution_request_id"]
            ):
                raise RuntimeError("persisted execution request artifact mismatch")
            for key in (
                "execution_invoked",
                "gazebo_execution_invoked",
                "external_dispatch_performed",
                "mavlink_dispatch_performed",
                "px4_mission_upload_performed",
                "hardware_target_allowed",
                "physical_execution_invoked",
            ):
                if request[key] is not False or prepared["summary"][key] is not False:
                    raise RuntimeError(f"prepared route weakened safety flag: {key}")

            return {
                "mission_designer_prepare_sitl_gateway_smoke_passed": True,
                "base_url": base_url,
                "task_id": prepared["summary"]["task_id"],
                "task_status": stored["status"],
                "artifact_persisted": True,
                "schema_version": request["schema_version"],
                "request_status": request["request_status"],
                "preparation_scope": request["preparation_scope"],
                "execution_mode": request["execution_mode"],
                "target_endpoint": request["target_endpoint"],
                "target_endpoint_whitelisted": request["target_endpoint_whitelisted"],
                "requires_explicit_execution_approval": request[
                    "requires_explicit_execution_approval"
                ],
                "execution_invoked": request["execution_invoked"],
                "gazebo_execution_invoked": request["gazebo_execution_invoked"],
                "external_dispatch_performed": request["external_dispatch_performed"],
                "mavlink_dispatch_performed": request["mavlink_dispatch_performed"],
                "px4_mission_upload_performed": request["px4_mission_upload_performed"],
                "hardware_target_allowed": request["hardware_target_allowed"],
                "physical_execution_invoked": request["physical_execution_invoked"],
                "unsafe_prepare_rejected": True,
                "environment_limitations": [
                    "Gateway prepared and persisted the SITL execution request only",
                    "no Gazebo/PX4 container was started",
                    "no MAVLink mission upload was dispatched",
                ],
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)


if __name__ == "__main__":
    summary = asyncio.run(_main())
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
