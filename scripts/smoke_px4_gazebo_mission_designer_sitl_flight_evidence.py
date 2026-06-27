"""Runtime smoke for Mission Designer SITL flight-evidence attachment.

This smoke exercises the Gateway prepare/execute route and then attaches an
already-observed PX4/Gazebo horizontal-route summary to the same persisted task.
The summary must come from the opt-in horizontal-route smoke; this script does
not synthesize flight facts.
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
    attach_px4_gazebo_mission_designer_sitl_flight_evidence,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboSITLMissionUploader,
)

OPT_IN_ENV = "RUN_MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SMOKE"
SUMMARY_PATH_ENV = "MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SUMMARY_PATH"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run this smoke.")


def _load_horizontal_summary() -> dict[str, Any]:
    summary_path_value = os.getenv(SUMMARY_PATH_ENV, "").strip()
    if not summary_path_value:
        raise SystemExit(f"Set {SUMMARY_PATH_ENV}=<horizontal summary.json>.")
    summary_path = Path(summary_path_value).expanduser()
    if not summary_path.is_file():
        raise SystemExit(f"{SUMMARY_PATH_ENV} must point to summary.json.")
    summary = json.loads(summary_path.read_text())
    if summary.get("preupload_mission_performed") is not True:
        raise RuntimeError("flight evidence smoke requires preupload mission facts")
    if summary.get("preupload_mission_ack_observed") is not True:
        raise RuntimeError("flight evidence smoke requires observed preupload ACK")
    if summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise RuntimeError("flight evidence smoke requires accepted preupload ACK")
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is not True:
        raise RuntimeError("flight evidence smoke requires actual horizontal smoke")
    return summary


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


def _install_observed_uploader(horizontal_summary: dict[str, Any]):
    original_upload = PX4GazeboSITLMissionUploader.upload
    sequences = tuple(
        int(item) for item in horizontal_summary["preupload_mission_request_sequences"]
    )

    def _observed_upload(
        self,
        *,
        items,
        target_endpoint: str,
        timeout_seconds: float,
    ):
        if target_endpoint != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
            raise RuntimeError(f"unexpected SITL upload endpoint: {target_endpoint}")
        if timeout_seconds <= 0:
            raise RuntimeError("timeout_seconds must be positive")
        if len(items) != len(sequences):
            raise RuntimeError("observed mission request sequence count mismatch")
        return sequences, MAV_MISSION_ACCEPTED

    PX4GazeboSITLMissionUploader.upload = _observed_upload
    return original_upload


async def _main() -> dict[str, Any]:
    _require_opt_in()
    horizontal_summary = _load_horizontal_summary()
    previous_runner_opt_in = os.environ.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV)
    original_upload = _install_observed_uploader(horizontal_summary)
    try:
        with tempfile.TemporaryDirectory(
            prefix="mission-designer-sitl-flight-evidence-"
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
                async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
                    proposed = await _post_json(
                        client,
                        "/px4-gazebo/mission-scenarios/propose",
                        {
                            "prompt": (
                                "３０００メートルの山の山頂に重さ５キロの水を"
                                "届けるミッションを作成して"
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
                            "scenario_compile_result": approved[
                                "scenario_compile_result"
                            ],
                            "bounded_simulation_request": approved[
                                "bounded_simulation_request"
                            ],
                            "owner_session_id": "smoke-sitl-flight-evidence",
                            "owner_user_id": "operator",
                        },
                    )
                    executed = await _post_json(
                        client,
                        "/px4-gazebo/mission-scenarios/execute-sitl",
                        {
                            "task_id": prepared["summary"]["task_id"],
                            "explicit_execution_approval": True,
                        },
                    )
                    attached = attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                        prepared["summary"]["task_id"],
                        horizontal_summary=horizontal_summary,
                        task_store_factory=lambda: gateway.task_store,
                    )
                    stored_response = await client.get(
                        f"/tasks/{prepared['summary']['task_id']}"
                    )
                    stored_response.raise_for_status()
                    stored = stored_response.json()["task"]
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=10.0)

        result = attached["px4_gazebo_mission_designer_sitl_execution_result"]
        evidence = attached["px4_gazebo_mission_designer_sitl_flight_evidence"]
        if executed["summary"]["upload_status"] != "uploaded":
            raise RuntimeError("Gateway execute route did not observe upload")
        if (
            result["result_status"]
            != "flight_evidence_observed_payload_dropoff_pending"
        ):
            raise RuntimeError("flight evidence did not update result status")
        if stored["status"] != "completed":
            raise RuntimeError("stored task did not remain completed")
        if (
            "px4_gazebo_mission_designer_sitl_flight_evidence"
            not in stored["artifacts"]
        ):
            raise RuntimeError("stored task missing flight evidence artifact")
        for key in (
            "actual_sitl_flight_evidence_observed",
            "actual_takeoff_observed",
            "actual_dropoff_region_reached",
            "actual_land_observed",
        ):
            if result[key] is not True:
                raise RuntimeError(f"flight evidence result did not set {key}")
        for key in (
            "payload_release_observed",
            "payload_release_verified",
            "dropoff_verified",
            "synthetic_success_allowed",
        ):
            if result[key] is not False:
                raise RuntimeError(f"flight evidence slice weakened {key}")

        return {
            "mission_designer_sitl_flight_evidence_smoke_passed": True,
            "task_id": stored["task_id"],
            "task_status": stored["status"],
            "gateway_upload_status": executed["summary"]["upload_status"],
            "sitl_execution_result_status": result["result_status"],
            "flight_evidence_schema_version": evidence["schema_version"],
            "flight_evidence_ref": result["flight_evidence_ref"],
            "horizontal_summary_sha256": evidence["horizontal_summary_sha256"],
            "actual_sitl_mission_upload_observed": result[
                "actual_sitl_mission_upload_observed"
            ],
            "actual_sitl_flight_evidence_observed": result[
                "actual_sitl_flight_evidence_observed"
            ],
            "actual_takeoff_observed": result["actual_takeoff_observed"],
            "actual_dropoff_region_reached": result["actual_dropoff_region_reached"],
            "actual_land_observed": result["actual_land_observed"],
            "payload_release_observed": result["payload_release_observed"],
            "payload_release_verified": result["payload_release_verified"],
            "dropoff_verified": result["dropoff_verified"],
            "failure_reasons": result["failure_reasons"],
            "horizontal_route_artifact_dir": horizontal_summary["artifact_dir"],
            "payload_release_evidence_available_but_not_attached": evidence["metadata"][
                "payload_release_evidence_available_but_not_attached"
            ],
            "hardware_target_allowed": result["hardware_target_allowed"],
            "physical_execution_invoked": result["physical_execution_invoked"],
            "ros_dispatch_performed": result["ros_dispatch_performed"],
            "actuator_execution_performed": result["actuator_execution_performed"],
            "synthetic_success_allowed": result["synthetic_success_allowed"],
        }
    finally:
        PX4GazeboSITLMissionUploader.upload = original_upload
        if previous_runner_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = (
                previous_runner_opt_in
            )


if __name__ == "__main__":
    smoke_summary = asyncio.run(_main())
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
