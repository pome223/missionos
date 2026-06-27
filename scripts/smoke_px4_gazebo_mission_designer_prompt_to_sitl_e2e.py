"""Epic-exit smoke for Mission Designer prompt to PX4/Gazebo SITL execution.

This smoke exercises the Gateway path end to end and sources mission-upload
evidence from the existing actual PX4/Gazebo SITL upload smoke. The Gateway
route still uses the production runner and TaskStore path; the uploader boundary
is fed with observed SITL MAVLink facts so this smoke does not depend on a host
UDP route into Docker.
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

from scripts import smoke_px4_gazebo_sitl_mission_upload as sitl_upload_smoke
from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboSITLMissionUploader,
)

OPT_IN_ENV = "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_E2E_SMOKE"
EXPECTED_PENDING_FAILURE_REASONS = (
    "observed_flight_evidence_not_attached",
    "payload_release_event_not_observed",
    "dropoff_verification_not_observed",
)
EXPECTED_UI_SUMMARY_KEYS = frozenset(
    (
        "actual_dropoff_region_reached",
        "actual_land_observed",
        "actual_sitl_flight_evidence_observed",
        "actual_sitl_mission_upload_observed",
        "actual_takeoff_observed",
        "actuator_execution_performed",
        "dropoff_verification_ref",
        "dropoff_verified",
        "execution_request_id",
        "explicit_execution_approval",
        "external_dispatch_performed",
        "failure_reasons",
        "flight_evidence_ref",
        "gazebo_entity_mutation_performed",
        "hardware_target_allowed",
        "mavlink_dispatch_performed",
        "mission_ack_observed",
        "mission_ack_type",
        "mission_item_count",
        "payload_dropoff_success_requires_observed_facts",
        "payload_release_event_ref",
        "payload_release_observed",
        "payload_release_verified",
        "physical_execution_invoked",
        "px4_mission_upload_performed",
        "ros_dispatch_performed",
        "sitl_execution_opted_in",
        "sitl_execution_result_status",
        "synthetic_success_allowed",
        "target_endpoint",
        "task_id",
        "task_status",
        "upload_status",
    )
)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the Mission Designer SITL E2E")


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


def _install_observed_uploader(observed_upload: dict[str, Any]):
    original_upload = PX4GazeboSITLMissionUploader.upload

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
        if len(items) != len(observed_upload["mission_request_sequences"]):
            raise RuntimeError("observed mission request sequence count mismatch")
        return (
            tuple(int(item) for item in observed_upload["mission_request_sequences"]),
            int(observed_upload["mission_ack_type"]),
        )

    PX4GazeboSITLMissionUploader.upload = _observed_upload
    return original_upload


async def _main() -> dict[str, Any]:
    _require_opt_in()
    previous_runner_opt_in = os.environ.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV)
    # These helpers are intentionally shared with the established actual SITL
    # mission-upload smoke so this epic-exit check uses the same Docker/PX4
    # startup and MAVLink observation path.
    sitl_upload_smoke._start_container()
    try:
        observed_upload = sitl_upload_smoke._actual_upload()
        if observed_upload.get("mission_ack_type") != MAV_MISSION_ACCEPTED:
            raise RuntimeError("actual SITL upload did not observe accepted ACK")
        if observed_upload.get("mission_ack_observed") is not True:
            raise RuntimeError("actual SITL upload did not observe mission ACK")

        original_upload = _install_observed_uploader(observed_upload)
        try:
            with tempfile.TemporaryDirectory(
                prefix="mission-designer-prompt-to-sitl-e2e-"
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
                    async with httpx.AsyncClient(
                        base_url=base_url, timeout=20.0
                    ) as client:
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
                                "owner_session_id": "smoke-mission-designer-e2e",
                                "owner_user_id": "operator",
                            },
                        )
                        prepared_task = prepared["task"]
                        prepared_artifacts = prepared_task["artifacts"]
                        if (
                            "px4_gazebo_sitl_mission_upload_receipt"
                            in prepared_artifacts
                        ):
                            raise RuntimeError(
                                "prepared-only path already contains upload receipt"
                            )
                        if (
                            "px4_gazebo_mission_designer_sitl_execution_result"
                            in prepared_artifacts
                        ):
                            raise RuntimeError(
                                "prepared-only path already contains execution result"
                            )
                        for key in (
                            "execution_invoked",
                            "gazebo_execution_invoked",
                            "external_dispatch_performed",
                            "mavlink_dispatch_performed",
                            "px4_mission_upload_performed",
                            "hardware_target_allowed",
                            "physical_execution_invoked",
                        ):
                            if prepared["summary"][key] is not False:
                                raise RuntimeError(
                                    f"prepared-only path claimed execution: {key}"
                                )

                        executed_response = await client.post(
                            "/px4-gazebo/mission-scenarios/execute-sitl",
                            json={
                                "task_id": prepared["summary"]["task_id"],
                                "explicit_execution_approval": True,
                            },
                        )
                        if executed_response.status_code != 200:
                            raise RuntimeError(
                                "opt-in execute route did not complete: "
                                + executed_response.text[-1000:]
                            )
                        executed = executed_response.json()
                        stored_response = await client.get(
                            f"/tasks/{prepared['summary']['task_id']}"
                        )
                        stored_response.raise_for_status()
                        stored = stored_response.json()["task"]
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=10.0)
        finally:
            PX4GazeboSITLMissionUploader.upload = original_upload

        summary = executed["summary"]
        result = executed["px4_gazebo_mission_designer_sitl_execution_result"]
        receipt = executed["px4_gazebo_sitl_mission_upload_receipt"]
        if stored["status"] != "completed":
            raise RuntimeError("executed task did not complete")
        if summary["upload_status"] != "uploaded":
            raise RuntimeError("opt-in execute route did not upload")
        if (
            summary["sitl_execution_result_status"]
            != "mission_upload_observed_flight_evidence_pending"
        ):
            raise RuntimeError("SITL result did not record pending flight evidence")
        if summary["mission_ack_type"] != MAV_MISSION_ACCEPTED:
            raise RuntimeError("Gateway summary did not preserve accepted ACK")
        if tuple(result["failure_reasons"]) != EXPECTED_PENDING_FAILURE_REASONS:
            raise RuntimeError("SITL result failure reasons changed")
        if tuple(receipt["mission_request_sequences"]) != tuple(
            int(item) for item in observed_upload["mission_request_sequences"]
        ):
            raise RuntimeError("Gateway receipt did not preserve observed sequences")
        missing_summary_keys = EXPECTED_UI_SUMMARY_KEYS.difference(summary.keys())
        if missing_summary_keys:
            raise RuntimeError(
                "UI-facing summary missing keys: "
                + ", ".join(sorted(missing_summary_keys))
            )
        for key in (
            "external_dispatch_performed",
            "mavlink_dispatch_performed",
            "px4_mission_upload_performed",
            "actual_sitl_mission_upload_observed",
            "mission_ack_observed",
        ):
            if summary[key] is not True:
                raise RuntimeError(f"opt-in path did not flip SITL flag: {key}")
        for key in (
            "hardware_target_allowed",
            "physical_execution_invoked",
            "gazebo_entity_mutation_performed",
            "ros_dispatch_performed",
            "actuator_execution_performed",
            "actual_sitl_flight_evidence_observed",
            "payload_release_observed",
            "payload_release_verified",
            "dropoff_verified",
            "synthetic_success_allowed",
        ):
            if summary[key] is not False:
                raise RuntimeError(f"opt-in path weakened safety/result flag: {key}")
        for key in (
            "delivery_mission_contract",
            "simulated_command_proposal",
            "simulated_command_approval",
            "simulator_command_execution_preflight",
            "px4_gazebo_sitl_mission_upload_receipt",
            "px4_gazebo_mission_designer_sitl_execution_result",
        ):
            if key not in stored["artifacts"]:
                raise RuntimeError(f"stored task missing full-chain artifact: {key}")

        e2e_summary = {
            "mission_designer_prompt_to_sitl_e2e_passed": True,
            "task_id": prepared["summary"]["task_id"],
            "task_status": stored["status"],
            "prepared_only_task_status": prepared["summary"]["task_status"],
            "prepared_only_execution_invoked": prepared["summary"]["execution_invoked"],
            "prepared_only_receipt_absent": True,
            "prepared_only_result_absent": True,
            "execute_status_code": 200,
            "sitl_execution_opted_in": executed["sitl_execution_opted_in"],
            "actual_px4_gazebo_sitl_upload_observed": True,
            "gateway_uploader_replayed_observed_px4_facts": True,
            "upload_status": summary["upload_status"],
            "sitl_execution_result_status": summary["sitl_execution_result_status"],
            "mission_ack_observed": summary["mission_ack_observed"],
            "mission_ack_type": summary["mission_ack_type"],
            "mission_request_sequences": receipt["mission_request_sequences"],
            "actual_sitl_mission_upload_observed": summary[
                "actual_sitl_mission_upload_observed"
            ],
            "actual_sitl_flight_evidence_observed": summary[
                "actual_sitl_flight_evidence_observed"
            ],
            "payload_release_observed": summary["payload_release_observed"],
            "payload_release_verified": summary["payload_release_verified"],
            "dropoff_verified": summary["dropoff_verified"],
            "failure_reasons": result["failure_reasons"],
            "external_dispatch_performed": summary["external_dispatch_performed"],
            "mavlink_dispatch_performed": summary["mavlink_dispatch_performed"],
            "px4_mission_upload_performed": summary["px4_mission_upload_performed"],
            "hardware_target_allowed": summary["hardware_target_allowed"],
            "physical_execution_invoked": summary["physical_execution_invoked"],
            "gazebo_entity_mutation_performed": summary[
                "gazebo_entity_mutation_performed"
            ],
            "ros_dispatch_performed": summary["ros_dispatch_performed"],
            "actuator_execution_performed": summary["actuator_execution_performed"],
            "synthetic_success_allowed": summary["synthetic_success_allowed"],
            "ui_facing_summary_keys": sorted(summary.keys()),
            "full_chain_artifacts_present": True,
        }
        return e2e_summary
    finally:
        if previous_runner_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = (
                previous_runner_opt_in
            )
        sitl_upload_smoke._stop_container()


if __name__ == "__main__":
    smoke_summary = asyncio.run(_main())
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
