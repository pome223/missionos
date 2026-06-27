"""Browser smoke for the Mission Designer SITL evidence-chain UI panel.

This smoke starts the real Gateway static UI, renders a persisted-task-shaped
Mission Designer SITL delivery chain in the browser, and verifies that the UI is
read-only. It does not invoke SITL, MAVLink, ROS, Gazebo mutation, actuator,
hardware, or physical execution.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
from playwright.async_api import async_playwright
import uvicorn


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


def _seed_task() -> dict[str, Any]:
    return {
        "task_id": "task-sitl-evidence-chain-ui-smoke",
        "kind": "px4_gazebo_mission_designer_sitl_execution_request",
        "title": "Mission Designer SITL evidence-chain UI smoke",
        "status": "completed",
        "created_at": "2026-01-01T12:00:00Z",
        "updated_at": "2026-01-01T12:01:00Z",
        "artifacts": {
            "px4_gazebo_sitl_mission_upload_receipt": {
                "schema_version": "px4_gazebo_sitl_mission_upload_receipt.v1",
                "receipt_id": "receipt-ui-smoke",
                "upload_status": "uploaded",
                "target_endpoint": "udp://127.0.0.1:14540",
                "mission_item_count": 4,
            },
            "px4_gazebo_mission_designer_sitl_execution_result": {
                "schema_version": (
                    "px4_gazebo_mission_designer_sitl_execution_result.v1"
                ),
                "result_id": "result-ui-smoke",
                "execution_request_ref": (
                    "px4_gazebo_mission_designer_sitl_execution_request:req"
                ),
                "delivery_mission_contract_ref": "delivery_mission_contract:contract",
                "simulator_command_execution_preflight_ref": (
                    "simulator_command_execution_preflight:preflight"
                ),
                "px4_gazebo_sitl_mission_upload_receipt_ref": (
                    "px4_gazebo_sitl_mission_upload_receipt:receipt-ui-smoke"
                ),
                "result_status": "flight_evidence_observed_payload_dropoff_pending",
                "sitl_execution_opted_in": True,
                "artifact_only_dry_run": False,
                "actual_sitl_mission_upload_observed": True,
                "actual_sitl_flight_evidence_observed": True,
                "flight_evidence_ref": (
                    "px4_gazebo_mission_designer_sitl_flight_evidence:flight"
                ),
                "mission_upload_observed": True,
                "mission_ack_observed": True,
                "mission_ack_type": 0,
                "mission_request_sequences": [0, 1, 2, 3],
                "actual_takeoff_observed": True,
                "actual_dropoff_region_reached": True,
                "actual_land_observed": True,
                "payload_release_observed": False,
                "payload_release_verified": False,
                "payload_release_event_ref": "",
                "payload_release_event_source": "",
                "dropoff_verified": False,
                "dropoff_verification_ref": "",
                "failure_reasons": [
                    "payload_release_event_not_observed",
                    "dropoff_verification_not_observed",
                ],
                "external_dispatch_performed": True,
                "gazebo_simulator_command_performed": False,
                "mavlink_dispatch_performed": True,
                "px4_mission_upload_performed": True,
                "gazebo_entity_mutation_performed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "ros_dispatch_performed": False,
                "actuator_execution_performed": False,
                "synthetic_success_allowed": False,
                "payload_dropoff_success_requires_observed_facts": True,
                "artifact_only_dry_run_cannot_verify_payload_or_dropoff": True,
                "observed_at": "2026-01-01T12:00:30Z",
                "metadata": {},
            },
            "px4_gazebo_mission_designer_sitl_flight_evidence": {
                "schema_version": (
                    "px4_gazebo_mission_designer_sitl_flight_evidence.v1"
                ),
                "flight_evidence_id": "flight",
                "actual_sitl_flight_evidence_observed": True,
                "actual_takeoff_observed": True,
                "actual_dropoff_region_reached": True,
                "actual_land_observed": True,
                "horizontal_summary_sha256": "abc123",
                "horizontal_summary_artifact_dir": "output/px4/ui-smoke",
                "horizontal_progress_m": 7.1,
                "completed_pose_z_m": 0.11,
                "route_geofence_violation": False,
                "payload_release_observed": False,
                "dropoff_verified": False,
                "synthetic_success_allowed": False,
            },
            "px4_gazebo_mission_designer_sitl_payload_release_observation": {
                "schema_version": (
                    "px4_gazebo_mission_designer_sitl_payload_release_observation.v1"
                ),
                "observation_id": "payload-observation",
                "payload_release_event_ref": (
                    "px4_gazebo_sitl_payload_release_event:event"
                ),
                "event_source": "gazebo_detachable_joint_detach_event",
                "payload_id": "pkg-sitl-dropoff",
                "payload_release_observed_at": "2026-01-01T12:00:10Z",
                "release_position_x_m": 5.02,
                "release_position_y_m": 4.93,
                "release_position_z_m": 0.04,
                "payload_release_observed": True,
                "payload_release_event_verified": True,
                "payload_release_does_not_verify_dropoff": True,
                "dropoff_verified": False,
                "synthetic_success_allowed": False,
            },
            "px4_gazebo_sitl_payload_release_event": {
                "schema_version": "px4_gazebo_sitl_payload_release_event.v1",
                "event_id": "event",
                "event_source": "gazebo_detachable_joint_detach_event",
                "payload_id": "pkg-sitl-dropoff",
                "observed_at": "2026-01-01T12:00:10Z",
            },
            "px4_gazebo_sitl_dropoff_flight_fact": {
                "schema_version": "px4_gazebo_sitl_dropoff_flight_fact.v1",
                "fact_id": "flight-fact",
            },
            "px4_gazebo_sitl_dropoff_verification": {
                "schema_version": "px4_gazebo_sitl_dropoff_verification.v1",
                "verification_id": "verification",
                "status": "verified",
                "predicate_mode": (
                    "position_in_zone_and_altitude_and_mission_item_and_payload_release"
                ),
                "pose_within_dropoff_zone": True,
                "altitude_within_tolerance": True,
                "mission_item_reached": True,
            },
            "px4_gazebo_mission_designer_sitl_dropoff_verification": {
                "schema_version": (
                    "px4_gazebo_mission_designer_sitl_dropoff_verification.v1"
                ),
                "verification_id": "mission-designer-dropoff",
                "predicate_mode": (
                    "position_in_zone_and_altitude_and_mission_item_and_payload_release"
                ),
                "sitl_dropoff_verification_ref": (
                    "px4_gazebo_sitl_dropoff_verification:verification"
                ),
                "payload_release_verified": True,
                "dropoff_verified": True,
                "observed_facts_only": True,
                "synthetic_success_allowed": False,
                "observed_distance_to_dropoff_m": 0.09,
                "release_distance_to_dropoff_m": 0.08,
                "release_time_delta_seconds": 0.4,
            },
            "simulator_command_execution_preflight": {
                "schema_version": "simulator_command_execution_preflight.v1",
                "preflight_id": "preflight",
                "delivery_scorecard_ref": "delivery_scorecard:scorecard",
                "delivery_episode_review_ref": "delivery_episode_review:review",
                "autonomy_gate_result_ref": "autonomy_gate_result:gate",
                "scorecard_passed": True,
                "episode_review_passed": True,
                "autonomy_gate_passed": True,
            },
        },
        "metadata": {},
        "timeline": [],
    }


async def _main() -> dict[str, Any]:
    screenshot_path = Path(
        "output/mission_designer/"
        "mission-designer-sitl-evidence-chain-control-ui-smoke.png"
    )
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mission-designer-chain-ui-smoke-") as tmp:
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
        server_task = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            task_payload = _seed_task()
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function('typeof renderTaskDetail === "function"')
                rendered = await page.evaluate(
                    """(task) => {
                      const panel = document.querySelector("#dashboardDetailPanel");
                      panel.classList.remove("selection-detail-empty");
                      panel.innerHTML = renderTaskDetail(task);
                      return panel.innerText;
                    }""",
                    task_payload,
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)
                forbidden_action_count = await page.locator(
                    '[data-action^="execute-sitl"], '
                    '[data-action^="mission-designer-sitl"], '
                    '[data-action^="payload-release"], '
                    '[data-action^="dropoff"], '
                    '[data-action^="mavlink"], '
                    '[data-action^="gazebo-mutation"]'
                ).count()
                await browser.close()

            required_text = (
                "Final SITL Evidence Chain",
                "Base execution result records the state when the execution artifact was created",
                "dropoff-verified",
                "Upload Receipt",
                "Flight Evidence Artifact",
                "Payload Release Observation",
                "Dropoff Verification Artifact",
                "SITL Dropoff Verifier",
                "Scorecard / Review / Gate",
                "scorecard=projected_passed",
                "synthetic_success_allowed=false",
                "hardware_target_allowed=false",
                "physical_execution_invoked=false",
            )
            missing = [item for item in required_text if item not in rendered]
            if missing:
                raise RuntimeError(f"UI smoke missing rendered text: {missing}")
            if forbidden_action_count != 0:
                raise RuntimeError(
                    f"UI smoke found forbidden SITL action controls: "
                    f"{forbidden_action_count}"
                )
            return {
                "base_url": base_url,
                "rendered_chain_state": "dropoff-verified",
                "required_text_count": len(required_text),
                "forbidden_action_count": forbidden_action_count,
                "screenshot_path": str(screenshot_path.resolve()),
                "production_boundary": "Gateway /chat static UI rendered in browser",
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=10.0)


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
