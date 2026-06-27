"""Browser smoke for the Mission Designer Control UI tab.

The smoke exercises the real Gateway static UI through a loopback browser:
prompt -> proposal -> approval -> SITL preparation -> operator-approved live
SITL execution route. The live execution environment opt-ins are intentionally
unset, so the Gateway persists a blocked live-flight receipt instead of invoking
SITL, MAVLink, Gazebo mutation, hardware, or physical execution.
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


async def _main() -> dict[str, Any]:
    screenshot_path = Path(
        "output/mission_designer/mission-designer-control-ui-smoke.png"
    )
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mission-designer-ui-smoke-") as tmp_dir:
        _configure_temp_paths(Path(tmp_dir))

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
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 980})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.get_by_role("button", name="Mission Designer").click()
                await page.locator("#missionScenarioPromptInput").fill(
                    "３０００メートルの山の山頂に重さ５キロの水を届けるミッションを作成して"
                )
                await page.locator("#missionScenarioGenerateBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#missionScenarioValidationStatus')?.textContent === 'accepted'"
                )
                await page.locator("#missionScenarioApproveBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#missionScenarioResult')?.innerText.includes('mountain_summit_payload_delivery')"
                )
                await page.locator("#missionScenarioPrepareSitlBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#missionScenarioResult')?.innerText.includes('prepared_waiting_for_sitl_execution_approval')"
                )
                execute_disabled_after_prepare = await page.locator(
                    "#missionScenarioExecuteSitlBtn"
                ).is_disabled()
                if execute_disabled_after_prepare:
                    raise RuntimeError("Execute Live SITL button remained disabled")
                await page.locator("#missionScenarioExecuteSitlBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#missionScenarioResult')?.innerText.includes('Live SITL Execution')"
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)
                status = await page.locator("#missionScenarioStatus").inner_text()
                validation = await page.locator(
                    "#missionScenarioValidationStatus"
                ).inner_text()
                dry_run = await page.locator(
                    "#missionScenarioDryRunStatus"
                ).inner_text()
                waypoints = await page.locator(
                    "#missionScenarioWaypointCount"
                ).inner_text()
                segments = await page.locator(
                    "#missionScenarioSegmentCount"
                ).inner_text()
                result_text = await page.locator("#missionScenarioResult").inner_text()
                sitl_status = await page.locator(
                    "#missionScenarioSitlStatus"
                ).inner_text()
                sitl_execution_status = await page.locator(
                    "#missionScenarioSitlExecutionStatus"
                ).inner_text()
                execute_button_count = await page.locator(
                    "#missionScenarioExecuteSitlBtn"
                ).count()
                execute_button_disabled = await page.locator(
                    "#missionScenarioExecuteSitlBtn"
                ).is_disabled()
                await browser.close()

            result_text_lower = result_text.lower()
            assert validation == "accepted"
            assert dry_run == "completed"
            assert int(waypoints) >= 3
            assert int(segments) >= 3
            assert "Live SITL execution blocked by server-side gates" in status
            assert sitl_status == "prepared_waiting_for_sitl_execution_approval"
            assert sitl_execution_status == "blocked"
            assert "Extracted Constraints" in result_text
            assert "3000" in result_text
            assert "5" in result_text
            assert "Digital Twin Planning Track" in result_text
            assert (
                "This track evaluates real-world terrain/weather planning evidence"
                in result_text
            )
            assert "heightmap artifact record" in result_text_lower
            assert "record materialized" in result_text_lower
            assert "world from artifact" in result_text_lower
            assert "heightmap file" in result_text_lower
            assert "file materialized" in result_text_lower
            assert "world from file" in result_text_lower
            assert "world candidate" in result_text_lower
            assert "generated_for_planning_only" in result_text
            assert "world artifact" in result_text_lower
            assert "materialized" in result_text_lower
            assert "gz_sim_sdf_world" in result_text
            assert "world materialized" in result_text_lower
            assert "execution binding" in result_text_lower
            assert "transform candidate" in result_text_lower
            assert "candidate_generated" in result_text
            assert "source frame" in result_text_lower
            assert "target frame" in result_text_lower
            assert "gazebo_world_local" in result_text
            assert "transform materialized" in result_text_lower
            assert "px4 item candidate" in result_text_lower
            assert "candidate_generated_for_planning_only" in result_text
            assert "candidate items" in result_text_lower
            assert "takeoff anchor" in result_text_lower
            assert "px4 upload allowed" in result_text_lower
            assert "sitl binding gate" in result_text_lower
            assert "binding eligible" in result_text_lower
            assert "binding allowed" in result_text_lower
            assert "operator approval" in result_text_lower
            assert "server opt-in" in result_text_lower
            assert "mission item candidate" in result_text_lower
            assert "px4 upload" in result_text_lower
            assert "Existing Safe-Route SITL Execution Track" in result_text
            assert "It is not yet bound to the Digital Twin world" in result_text
            assert "digital twin world bound" in result_text_lower
            assert "energy_budget_risk" in result_text
            assert "Approval" in result_text
            assert "compile_to_bounded_simulation_request_only" in result_text
            assert "mountain_summit_payload_delivery" in result_text
            assert "deterministic_bounded_mission_runner" in result_text
            assert "ready_for_deterministic_bounded_run" in result_text
            assert "Prepared SITL Execution Request" in result_text
            assert "prepare_sitl_execution_request_only" in result_text
            assert "udp://127.0.0.1:14540" in result_text
            assert "Live SITL Execution" in result_text
            assert "live mode" in result_text
            assert "true" in result_text
            assert (
                "Mission Designer SITL execution requires explicit opt-in"
                in result_text
            )
            assert (
                "Mission Designer live SITL flight requires explicit opt-in"
                in result_text
            )
            assert (
                "Payload release, dropoff verification, and epic-exit artifacts"
                in result_text
            )
            assert "mission upload" in result_text
            assert "false" in result_text
            assert execute_button_count == 1
            assert execute_button_disabled is True
            return {
                "base_url": base_url,
                "validation_status": validation,
                "dry_run_status": dry_run,
                "waypoints": int(waypoints),
                "segments": int(segments),
                "constraint_text_present": "Extracted Constraints" in result_text,
                "energy_budget_risk_present": "energy_budget_risk" in result_text,
                "approval_text_present": "Approval" in result_text,
                "approval_scope_present": (
                    "compile_to_bounded_simulation_request_only" in result_text
                ),
                "scenario_profile_present": (
                    "mountain_summit_payload_delivery" in result_text
                ),
                "sitl_status": sitl_status,
                "sitl_execution_status": sitl_execution_status,
                "prepared_sitl_request_present": (
                    "Prepared SITL Execution Request" in result_text
                ),
                "execution_button_present": execute_button_count == 1,
                "execution_button_disabled_after_attempt": execute_button_disabled,
                "digital_twin_track_present": (
                    "Digital Twin Planning Track" in result_text
                ),
                "safe_route_sitl_track_present": (
                    "Existing Safe-Route SITL Execution Track" in result_text
                ),
                "digital_twin_world_not_bound_present": (
                    "It is not yet bound to the Digital Twin world" in result_text
                ),
                "heightmap_artifact_present": (
                    "heightmap artifact record" in result_text_lower
                ),
                "heightmap_artifact_materialized_present": (
                    "record materialized" in result_text_lower
                ),
                "world_from_artifact_false_present": (
                    "world from artifact" in result_text_lower
                    and "false" in result_text_lower
                ),
                "heightmap_file_present": "heightmap file" in result_text_lower,
                "heightmap_file_materialized_present": (
                    "file materialized" in result_text_lower
                ),
                "world_from_file_false_present": (
                    "world from file" in result_text_lower
                    and "false" in result_text_lower
                ),
                "gazebo_world_candidate_present": (
                    "world candidate" in result_text_lower
                ),
                "gazebo_world_candidate_planning_only_present": (
                    "generated_for_planning_only" in result_text
                ),
                "gazebo_world_artifact_present": (
                    "world artifact" in result_text_lower
                ),
                "gazebo_world_artifact_materialized_present": (
                    "world artifact" in result_text_lower
                    and "materialized" in result_text_lower
                ),
                "gazebo_world_sdf_format_present": (
                    "gz_sim_sdf_world" in result_text
                ),
                "gazebo_world_materialized_false_present": (
                    "world materialized" in result_text_lower
                    and "false" in result_text_lower
                ),
                "gazebo_world_materialized_true_present": (
                    "world materialized" in result_text_lower
                    and "true" in result_text_lower
                ),
                "gazebo_world_execution_binding_false_present": (
                    "execution binding" in result_text_lower
                    and "false" in result_text_lower
                ),
                "coordinate_transform_candidate_present": (
                    "transform candidate" in result_text_lower
                ),
                "coordinate_transform_candidate_generated_present": (
                    "candidate_generated" in result_text
                ),
                "coordinate_transform_source_frame_present": (
                    "source frame" in result_text_lower
                ),
                "coordinate_transform_target_frame_present": (
                    "target frame" in result_text_lower
                    and "gazebo_world_local" in result_text
                ),
                "coordinate_transform_materialized_false_present": (
                    "transform materialized" in result_text_lower
                    and "false" in result_text_lower
                ),
                "mission_anchor_candidate_present": (
                    "anchor candidate" in result_text_lower
                ),
                "mission_anchor_candidate_available_present": (
                    "anchors_available_for_planning" in result_text
                ),
                "mission_anchor_takeoff_anchor_present": (
                    "takeoff anchor" in result_text_lower
                ),
                "px4_mission_item_candidate_present": (
                    "px4 item candidate" in result_text_lower
                ),
                "px4_mission_item_candidate_blocked_present": (
                    "blocked_by_missing_takeoff_anchor" in result_text
                ),
                "px4_mission_item_candidate_generated_present": (
                    "candidate_generated_for_planning_only" in result_text
                ),
                "px4_mission_item_candidate_empty_present": (
                    "candidate items" in result_text_lower
                    and "0" in result_text_lower
                ),
                "px4_mission_item_candidate_nonempty_present": (
                    "px4 mission items" in result_text_lower
                    and "2" in result_text_lower
                ),
                "px4_mission_item_candidate_takeoff_anchor_present": (
                    "takeoff anchor" in result_text_lower
                ),
                "px4_mission_upload_allowed_false_present": (
                    "px4 upload allowed" in result_text_lower
                    and "false" in result_text_lower
                ),
                "sitl_binding_gate_present": (
                    "sitl binding gate" in result_text_lower
                ),
                "sitl_binding_eligible_false_present": (
                    "binding eligible" in result_text_lower
                    and "false" in result_text_lower
                ),
                "sitl_binding_allowed_false_present": (
                    "binding allowed" in result_text_lower
                    and "false" in result_text_lower
                ),
                "sitl_binding_operator_approval_present": (
                    "operator approval" in result_text_lower
                ),
                "sitl_binding_server_opt_in_present": (
                    "server opt-in" in result_text_lower
                ),
                "live_execution_blocked_reason_present": (
                    "Mission Designer live SITL flight requires explicit opt-in"
                    in result_text
                ),
                "screenshot_path": str(screenshot_path.resolve()),
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
