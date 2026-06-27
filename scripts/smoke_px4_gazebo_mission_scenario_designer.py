"""Runtime smoke for the Mission Designer prompt-to-scenario Gateway endpoint."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from hashlib import sha256
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
import uvicorn


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


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp / "computer_trajectories.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )


async def _post_scenario(base_url: str, prompt: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/px4-gazebo/mission-scenarios/propose",
            json={"prompt": prompt},
        )
        response.raise_for_status()
        return response.json()


async def _approve_scenario(base_url: str, result: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/px4-gazebo/mission-scenarios/approve",
            json={
                "scenario_proposal": result["scenario_proposal"],
                "validation_result": result["validation_result"],
            },
        )
        response.raise_for_status()
        return response.json()


async def _approve_scenario_status(base_url: str, result: dict[str, Any]) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/px4-gazebo/mission-scenarios/approve",
            json={
                "scenario_proposal": result["scenario_proposal"],
                "validation_result": result["validation_result"],
            },
        )
        return response.status_code


async def _main() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mission-scenario-smoke-") as tmp_dir:
        _configure_temp_paths(Path(tmp_dir))

        from src.config.settings import reset_settings
        from src.gateway.server import create_gateway
        from src.runtime.digital_twin_mission_environment import (
            CoordinateTransformCandidate,
            DigitalTwinMissionAnchorCandidate,
            DigitalTwinRoutePlan,
            GazeboWorldArtifact,
            GazeboWorldCandidate,
            RealWorldGeocodeCandidate,
            TerrainHeightmapFileArtifact,
            WeatherEnvironmentPolicyGate,
            build_digital_twin_px4_mission_item_candidate,
            build_digital_twin_sitl_binding_gate,
        )
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
            accepted = await _post_scenario(
                base_url,
                "３０００メートルの山の山頂に重さ５キロの水を届けるミッションを作成して",
            )
            digital_twin = await _post_scenario(
                base_url,
                "10km先の3000mの山小屋に水3kgを届けて、天候は雨",
            )
            non_rainy_digital_twin = await _post_scenario(
                base_url,
                "10km先の3000mの山小屋に水3kgを届ける",
            )
            ambiguous_digital_twin = await _post_scenario(
                base_url,
                "Deliver water somewhere in the mountains",
            )
            blocked = await _post_scenario(
                base_url,
                "Use MAVLink COMMAND_LONG over udp:127.0.0.1 port 14540.",
            )
            approval = await _approve_scenario(base_url, accepted)
            blocked_approval_status = await _approve_scenario_status(base_url, blocked)

            assert accepted["validation_result"]["validation_status"] == "accepted"
            assert accepted["dry_run_result"]["dry_run_status"] == "completed"
            assert accepted["dry_run_result"]["gazebo_execution_invoked"] is False
            assert accepted["dry_run_result"]["hardware_target_allowed"] is False
            assert accepted["dry_run_result"]["physical_execution_invoked"] is False
            assert accepted["scenario_proposal"]["altitude_target_m"] == 3000
            assert accepted["scenario_proposal"]["payload_weight_kg"] == 5.0
            assert (
                "mountain_route"
                in accepted["scenario_proposal"]["terrain_hazard_labels"]
            )
            assert (
                "summit_dropoff"
                in accepted["scenario_proposal"]["terrain_hazard_labels"]
            )
            assert (
                "high_elevation"
                in accepted["scenario_proposal"]["terrain_hazard_labels"]
            )
            assert (
                "payload_weight"
                in accepted["scenario_proposal"]["equipment_incident_labels"]
            )
            assert (
                "energy_budget_risk"
                in accepted["scenario_proposal"]["feasibility_risk_labels"]
            )
            heightmap = digital_twin["terrain_heightmap_candidate"]
            heightmap_artifact = digital_twin["terrain_heightmap_artifact"]
            heightmap_file = digital_twin["terrain_heightmap_file_artifact"]
            gazebo_world = digital_twin["gazebo_world_candidate"]
            gazebo_world_artifact = digital_twin["gazebo_world_artifact"]
            coordinate_transform = digital_twin["coordinate_transform_candidate"]
            px4_mission_item_candidate = digital_twin[
                "digital_twin_px4_mission_item_candidate"
            ]
            sitl_binding_gate = digital_twin["digital_twin_sitl_binding_gate"]
            digital_twin_summary = digital_twin["summary"]
            assert heightmap["schema_version"] == "terrain_heightmap_candidate.v1"
            assert heightmap["artifact_materialized"] is False
            assert heightmap_artifact["schema_version"] == (
                "terrain_heightmap_artifact.v1"
            )
            assert heightmap_artifact["artifact_status"] == "materialized"
            assert heightmap_artifact["artifact_materialized"] is True
            assert heightmap_artifact["heightmap_candidate_ref"] == (
                f"terrain_heightmap_candidate:{heightmap['candidate_id']}"
            )
            assert heightmap_artifact["candidate_hash"] == heightmap["heightmap_hash"]
            assert heightmap_artifact["artifact_sha256"] == heightmap_artifact["sha256"]
            assert heightmap_artifact["gazebo_world_generated"] is False
            assert heightmap_artifact["coordinate_transform_generated"] is False
            assert heightmap_artifact["px4_mission_items_generated"] is False
            assert heightmap_artifact["hardware_target_allowed"] is False
            assert heightmap_artifact["physical_execution_invoked"] is False
            assert heightmap_file["schema_version"] == (
                "terrain_heightmap_file_artifact.v1"
            )
            assert heightmap_file["file_artifact_status"] == "materialized"
            assert heightmap_file["file_materialized"] is True
            assert heightmap_file["terrain_heightmap_artifact_ref"] == (
                f"terrain_heightmap_artifact:{heightmap_artifact['artifact_id']}"
            )
            assert heightmap_file["terrain_heightmap_candidate_ref"] == (
                f"terrain_heightmap_candidate:{heightmap['candidate_id']}"
            )
            assert heightmap_file["artifact_sha256"] == (
                heightmap_artifact["artifact_sha256"]
            )
            assert heightmap_file["candidate_hash"] == heightmap["heightmap_hash"]
            assert heightmap_file["file_sha256"] == heightmap_file["sha256"]
            assert heightmap_file["file_path_or_artifact_uri"].endswith(
                ".heightmap.json"
            )
            assert heightmap_file["file_path_or_artifact_uri"].startswith(
                "output/digital_twin/heightmaps/"
            )
            heightmap_file_path = Path(heightmap_file["file_path_or_artifact_uri"])
            assert heightmap_file_path.exists()
            assert sha256(heightmap_file_path.read_bytes()).hexdigest() == (
                heightmap_file["file_sha256"]
            )
            heightmap_gazebo_dem_path = Path(
                heightmap_file["gazebo_dem_file_path_or_artifact_uri"]
            )
            assert heightmap_gazebo_dem_path.exists()
            assert heightmap_gazebo_dem_path.read_bytes().startswith(
                b"P5\n64 64\n255\n"
            )
            assert sha256(heightmap_gazebo_dem_path.read_bytes()).hexdigest() == (
                heightmap_file["gazebo_dem_file_sha256"]
            )
            assert heightmap_file["gazebo_world_generated"] is False
            assert heightmap_file["coordinate_transform_generated"] is False
            assert heightmap_file["px4_mission_items_generated"] is False
            assert heightmap_file["hardware_target_allowed"] is False
            assert heightmap_file["physical_execution_invoked"] is False
            assert gazebo_world["schema_version"] == "gazebo_world_candidate.v1"
            assert gazebo_world["world_candidate_status"] == (
                "generated_for_planning_only"
            )
            assert gazebo_world["world_format"] == "gz_sim_world_candidate"
            assert gazebo_world["terrain_heightmap_file_artifact_ref"] == (
                f"terrain_heightmap_file_artifact:{heightmap_file['file_artifact_id']}"
            )
            assert gazebo_world["heightmap_uri"] == (
                heightmap_file["gazebo_dem_file_path_or_artifact_uri"]
            )
            assert gazebo_world["file_sha256"] == (
                heightmap_file["gazebo_dem_file_sha256"]
            )
            assert gazebo_world["route_plan_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert gazebo_world["weather_policy_gate_status"] == (
                "blocked_for_planning"
            )
            assert gazebo_world["execution_binding_allowed"] is False
            assert gazebo_world["gazebo_world_materialized"] is False
            assert gazebo_world["coordinate_transform_generated"] is False
            assert gazebo_world["px4_mission_items_generated"] is False
            assert gazebo_world["sitl_execution_bound"] is False
            assert gazebo_world["world_candidate_sha256"] == gazebo_world["sha256"]
            assert gazebo_world["gazebo_execution_invoked"] is False
            assert gazebo_world["hardware_target_allowed"] is False
            assert gazebo_world["physical_execution_invoked"] is False
            assert gazebo_world_artifact["schema_version"] == "gazebo_world_artifact.v1"
            assert gazebo_world_artifact["world_artifact_status"] == "materialized"
            assert gazebo_world_artifact["world_format"] == "gz_sim_sdf_world"
            assert gazebo_world_artifact["gazebo_world_candidate_ref"] == (
                f"gazebo_world_candidate:{gazebo_world['world_candidate_id']}"
            )
            assert gazebo_world_artifact["terrain_heightmap_file_artifact_ref"] == (
                f"terrain_heightmap_file_artifact:{heightmap_file['file_artifact_id']}"
            )
            assert gazebo_world_artifact["heightmap_uri"] == (
                heightmap_file["gazebo_dem_file_path_or_artifact_uri"]
            )
            assert gazebo_world_artifact["heightmap_file_sha256"] == (
                heightmap_file["gazebo_dem_file_sha256"]
            )
            assert gazebo_world_artifact["route_plan_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert gazebo_world_artifact["weather_policy_gate_status"] == (
                "blocked_for_planning"
            )
            assert gazebo_world_artifact["execution_binding_allowed"] is False
            assert gazebo_world_artifact["gazebo_world_materialized"] is True
            assert gazebo_world_artifact["coordinate_transform_generated"] is False
            assert gazebo_world_artifact["px4_mission_items_generated"] is False
            assert gazebo_world_artifact["sitl_execution_bound"] is False
            assert gazebo_world_artifact["world_file_sha256"] == (
                gazebo_world_artifact["sha256"]
            )
            world_file_path = Path(
                gazebo_world_artifact["world_file_path_or_artifact_uri"]
            )
            assert world_file_path.exists()
            assert str(world_file_path).startswith("output/digital_twin/worlds/")
            assert str(world_file_path).endswith(".world.sdf")
            assert sha256(world_file_path.read_bytes()).hexdigest() == (
                gazebo_world_artifact["world_file_sha256"]
            )
            assert gazebo_world_artifact["gazebo_execution_invoked"] is False
            assert gazebo_world_artifact["hardware_target_allowed"] is False
            assert gazebo_world_artifact["physical_execution_invoked"] is False
            assert coordinate_transform["schema_version"] == (
                "coordinate_transform_candidate.v1"
            )
            assert coordinate_transform["transform_candidate_status"] == (
                "candidate_generated"
            )
            assert coordinate_transform["gazebo_world_artifact_ref"] == (
                f"gazebo_world_artifact:{gazebo_world_artifact['world_artifact_id']}"
            )
            assert coordinate_transform["gazebo_world_candidate_ref"] == (
                f"gazebo_world_candidate:{gazebo_world['world_candidate_id']}"
            )
            assert coordinate_transform["terrain_heightmap_file_artifact_ref"] == (
                f"terrain_heightmap_file_artifact:{heightmap_file['file_artifact_id']}"
            )
            assert coordinate_transform["coordinate_frame_source"] == "wgs84"
            assert coordinate_transform["coordinate_frame_target"] == (
                "gazebo_world_local"
            )
            assert coordinate_transform["origin_latitude"] == 35.3606
            assert coordinate_transform["origin_longitude"] == 138.7274
            assert coordinate_transform["origin_altitude_m"] == 3000.0
            assert coordinate_transform["meters_per_degree_lat"] == 111320.0
            assert coordinate_transform["meters_per_degree_lon"] == 90784.349
            assert coordinate_transform["terrain_scale"] == [1890.0, 1890.0, 180.0]
            assert coordinate_transform["bbox"] == gazebo_world_artifact["bbox"]
            assert coordinate_transform["route_plan_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert coordinate_transform["gazebo_world_materialized"] is True
            assert coordinate_transform["coordinate_transform_materialized"] is False
            assert coordinate_transform["execution_binding_allowed"] is False
            assert coordinate_transform["px4_mission_items_generated"] is False
            assert coordinate_transform["sitl_execution_bound"] is False
            assert coordinate_transform["transform_hash"] == (
                coordinate_transform["sha256"]
            )
            assert coordinate_transform["gazebo_execution_invoked"] is False
            assert coordinate_transform["hardware_target_allowed"] is False
            assert coordinate_transform["physical_execution_invoked"] is False
            assert px4_mission_item_candidate["schema_version"] == (
                "digital_twin_px4_mission_item_candidate.v1"
            )
            assert px4_mission_item_candidate[
                "coordinate_transform_candidate_ref"
            ] == (
                "coordinate_transform_candidate:"
                f"{coordinate_transform['transform_candidate_id']}"
            )
            assert px4_mission_item_candidate["gazebo_world_artifact_ref"] == (
                f"gazebo_world_artifact:{gazebo_world_artifact['world_artifact_id']}"
            )
            assert px4_mission_item_candidate["gazebo_world_candidate_ref"] == (
                f"gazebo_world_candidate:{gazebo_world['world_candidate_id']}"
            )
            assert px4_mission_item_candidate[
                "terrain_heightmap_file_artifact_ref"
            ] == (
                "terrain_heightmap_file_artifact:"
                f"{heightmap_file['file_artifact_id']}"
            )
            assert px4_mission_item_candidate["candidate_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert px4_mission_item_candidate["candidate_items"] == []
            assert px4_mission_item_candidate["candidate_item_count"] == 0
            assert px4_mission_item_candidate["takeoff_anchor_ref"] == ""
            assert px4_mission_item_candidate["route_plan_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert px4_mission_item_candidate["weather_policy_gate_status"] == (
                "blocked_for_planning"
            )
            assert (
                px4_mission_item_candidate["coordinate_transform_materialized"]
                is False
            )
            assert px4_mission_item_candidate["execution_binding_allowed"] is False
            assert px4_mission_item_candidate["px4_mission_upload_allowed"] is False
            assert px4_mission_item_candidate["mavlink_dispatch_performed"] is False
            assert px4_mission_item_candidate["sitl_execution_bound"] is False
            assert px4_mission_item_candidate["gazebo_execution_invoked"] is False
            assert px4_mission_item_candidate["hardware_target_allowed"] is False
            assert px4_mission_item_candidate["physical_execution_invoked"] is False
            assert px4_mission_item_candidate["mission_item_candidate_hash"] == (
                px4_mission_item_candidate["sha256"]
            )
            assert px4_mission_item_candidate["blocked_reasons"] == [
                "weather_policy_gate_blocked",
                "takeoff_anchor_missing",
            ]
            assert sitl_binding_gate["schema_version"] == (
                "digital_twin_sitl_binding_gate.v1"
            )
            assert sitl_binding_gate["gazebo_world_artifact_ref"] == (
                f"gazebo_world_artifact:{gazebo_world_artifact['world_artifact_id']}"
            )
            assert sitl_binding_gate["coordinate_transform_candidate_ref"] == (
                "coordinate_transform_candidate:"
                f"{coordinate_transform['transform_candidate_id']}"
            )
            assert sitl_binding_gate[
                "digital_twin_px4_mission_item_candidate_ref"
            ] == (
                "digital_twin_px4_mission_item_candidate:"
                f"{px4_mission_item_candidate['candidate_id']}"
            )
            assert sitl_binding_gate["binding_gate_status"] == "blocked"
            assert sitl_binding_gate["binding_allowed"] is False
            assert sitl_binding_gate["binding_eligible"] is False
            assert sitl_binding_gate["operator_approval_required"] is True
            assert sitl_binding_gate["server_opt_in_required"] is True
            assert sitl_binding_gate["observed_facts_only"] is True
            assert sitl_binding_gate["route_plan_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert sitl_binding_gate["weather_policy_gate_status"] == (
                "blocked_for_planning"
            )
            assert sitl_binding_gate["px4_mission_item_candidate_status"] == (
                "blocked_by_weather_policy_gate"
            )
            assert sitl_binding_gate["candidate_item_count"] == 0
            assert sitl_binding_gate["coordinate_transform_materialized"] is False
            assert sitl_binding_gate["px4_mission_upload_allowed"] is False
            assert sitl_binding_gate["mavlink_dispatch_performed"] is False
            assert sitl_binding_gate["sitl_execution_bound"] is False
            assert sitl_binding_gate["gazebo_execution_invoked"] is False
            assert sitl_binding_gate["hardware_target_allowed"] is False
            assert sitl_binding_gate["physical_execution_invoked"] is False
            assert (
                sitl_binding_gate["approval_free_stronger_execution_allowed"]
                is False
            )
            assert sitl_binding_gate["binding_gate_hash"] == sitl_binding_gate["sha256"]
            assert sitl_binding_gate["blocked_reasons"] == [
                "weather_policy_gate_blocked",
                "takeoff_anchor_missing",
            ]
            non_rainy_world_artifact = GazeboWorldArtifact.model_validate(
                non_rainy_digital_twin["gazebo_world_artifact"]
            )
            non_rainy_world_candidate = GazeboWorldCandidate.model_validate(
                non_rainy_digital_twin["gazebo_world_candidate"]
            )
            non_rainy_heightmap_file = TerrainHeightmapFileArtifact.model_validate(
                non_rainy_digital_twin["terrain_heightmap_file_artifact"]
            )
            non_rainy_transform = CoordinateTransformCandidate.model_validate(
                non_rainy_digital_twin["coordinate_transform_candidate"]
            )
            non_rainy_weather_gate = WeatherEnvironmentPolicyGate.model_validate(
                non_rainy_digital_twin["weather_environment_policy_gate"]
            )
            non_rainy_route_plan = DigitalTwinRoutePlan.model_validate(
                non_rainy_digital_twin["digital_twin_route_plan"]
            )
            non_rainy_geocode = RealWorldGeocodeCandidate.model_validate(
                non_rainy_digital_twin["real_world_geocode_candidate"]
            )
            non_rainy_anchor = DigitalTwinMissionAnchorCandidate.model_validate(
                non_rainy_digital_twin["digital_twin_mission_anchor_candidate"]
            )
            eligible_mission_item_candidate = build_digital_twin_px4_mission_item_candidate(
                mission_anchor_candidate=non_rainy_anchor,
                coordinate_transform_candidate=non_rainy_transform,
                gazebo_world_artifact=non_rainy_world_artifact,
                gazebo_world_candidate=non_rainy_world_candidate,
                heightmap_file_artifact=non_rainy_heightmap_file,
                route_plan=non_rainy_route_plan,
                weather_policy_gate=non_rainy_weather_gate,
                geocode_candidate=non_rainy_geocode,
            )
            eligible_binding_gate = build_digital_twin_sitl_binding_gate(
                gazebo_world_artifact=non_rainy_world_artifact,
                coordinate_transform_candidate=non_rainy_transform,
                px4_mission_item_candidate=eligible_mission_item_candidate,
                weather_policy_gate=non_rainy_weather_gate,
                route_plan=non_rainy_route_plan,
            )
            assert eligible_mission_item_candidate.candidate_status == (
                "candidate_generated_for_planning_only"
            )
            assert eligible_mission_item_candidate.candidate_item_count > 0
            assert eligible_binding_gate.binding_gate_status == (
                "eligible_for_operator_approved_sitl_binding"
            )
            assert eligible_binding_gate.binding_eligible is True
            assert eligible_binding_gate.binding_allowed is False
            assert eligible_binding_gate.px4_mission_upload_allowed is False
            assert eligible_binding_gate.mavlink_dispatch_performed is False
            assert eligible_binding_gate.sitl_execution_bound is False
            assert eligible_binding_gate.gazebo_execution_invoked is False
            assert eligible_binding_gate.hardware_target_allowed is False
            assert eligible_binding_gate.physical_execution_invoked is False
            assert (
                eligible_binding_gate.approval_free_stronger_execution_allowed
                is False
            )
            assert digital_twin_summary["heightmap_artifact_status"] == "materialized"
            assert digital_twin_summary["heightmap_artifact_materialized"] is True
            assert (
                digital_twin_summary["heightmap_artifact_gazebo_world_generated"]
                is False
            )
            assert (
                digital_twin_summary[
                    "heightmap_artifact_coordinate_transform_generated"
                ]
                is False
            )
            assert (
                digital_twin_summary["heightmap_artifact_px4_mission_items_generated"]
                is False
            )
            assert (
                digital_twin_summary["heightmap_file_artifact_status"]
                == "materialized"
            )
            assert digital_twin_summary["heightmap_file_materialized"] is True
            assert digital_twin_summary["heightmap_file_sha256"] == (
                heightmap_file["sha256"]
            )
            assert digital_twin_summary["heightmap_file_gazebo_dem_sha256"] == (
                heightmap_file["gazebo_dem_file_sha256"]
            )
            assert (
                digital_twin_summary["heightmap_file_gazebo_world_generated"]
                is False
            )
            assert (
                digital_twin_summary[
                    "heightmap_file_coordinate_transform_generated"
                ]
                is False
            )
            assert (
                digital_twin_summary["heightmap_file_px4_mission_items_generated"]
                is False
            )
            assert (
                digital_twin_summary["gazebo_world_candidate_status"]
                == "generated_for_planning_only"
            )
            assert (
                digital_twin_summary["gazebo_world_artifact_status"]
                == "materialized"
            )
            assert (
                digital_twin_summary["gazebo_world_format"]
                == "gz_sim_sdf_world"
            )
            assert (
                digital_twin_summary["gazebo_world_file_path_or_artifact_uri"]
                == gazebo_world_artifact["world_file_path_or_artifact_uri"]
            )
            assert digital_twin_summary["gazebo_world_heightmap_file_sha256"] == (
                heightmap_file["gazebo_dem_file_sha256"]
            )
            assert digital_twin_summary["gazebo_world_file_sha256"] == (
                gazebo_world_artifact["world_file_sha256"]
            )
            assert (
                digital_twin_summary["gazebo_world_execution_binding_allowed"]
                is False
            )
            assert digital_twin_summary["gazebo_world_materialized"] is True
            assert (
                digital_twin_summary[
                    "gazebo_world_candidate_gazebo_execution_invoked"
                ]
                is False
            )
            assert (
                digital_twin_summary["gazebo_world_artifact_gazebo_execution_invoked"]
                is False
            )
            assert (
                digital_twin_summary["gazebo_world_coordinate_transform_generated"]
                is False
            )
            assert (
                digital_twin_summary["gazebo_world_px4_mission_items_generated"]
                is False
            )
            assert digital_twin_summary["gazebo_world_sitl_execution_bound"] is False
            assert (
                digital_twin_summary["coordinate_transform_candidate_status"]
                == "candidate_generated"
            )
            assert digital_twin_summary["coordinate_transform_frame_source"] == "wgs84"
            assert (
                digital_twin_summary["coordinate_transform_frame_target"]
                == "gazebo_world_local"
            )
            assert digital_twin_summary["coordinate_transform_materialized"] is False
            assert (
                digital_twin_summary[
                    "coordinate_transform_execution_binding_allowed"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "coordinate_transform_px4_mission_items_generated"
                ]
                is False
            )
            assert (
                digital_twin_summary["coordinate_transform_sitl_execution_bound"]
                is False
            )
            assert (
                digital_twin_summary[
                    "coordinate_transform_gazebo_execution_invoked"
                ]
                is False
            )
            assert (
                digital_twin_summary["mission_anchor_candidate_status"]
                == "blocked_by_weather_policy_gate"
            )
            assert digital_twin_summary["mission_anchor_candidate_takeoff_anchor_ref"] == ""
            assert (
                digital_twin_summary["mission_anchor_candidate_dropoff_anchor_ref"]
                == digital_twin_summary["real_world_geocode_candidate_ref"]
            )
            assert (
                digital_twin_summary[
                    "mission_anchor_candidate_px4_mission_upload_allowed"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "mission_anchor_candidate_mavlink_dispatch_performed"
                ]
                is False
            )
            assert (
                digital_twin_summary["mission_anchor_candidate_sitl_execution_bound"]
                is False
            )
            assert (
                digital_twin_summary["mission_anchor_candidate_gazebo_execution_invoked"]
                is False
            )
            assert (
                digital_twin_summary["px4_mission_item_candidate_status"]
                == "blocked_by_weather_policy_gate"
            )
            assert digital_twin_summary["px4_mission_item_candidate_item_count"] == 0
            assert (
                digital_twin_summary["px4_mission_item_candidate_takeoff_anchor_ref"]
                == ""
            )
            assert (
                digital_twin_summary["px4_mission_item_candidate_route_plan_status"]
                == "blocked_by_weather_policy_gate"
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_weather_policy_gate_status"
                ]
                == "blocked_for_planning"
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_coordinate_transform_materialized"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_execution_binding_allowed"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_px4_mission_upload_allowed"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_mavlink_dispatch_performed"
                ]
                is False
            )
            assert (
                digital_twin_summary["px4_mission_item_candidate_sitl_execution_bound"]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_gazebo_execution_invoked"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_hardware_target_allowed"
                ]
                is False
            )
            assert (
                digital_twin_summary[
                    "px4_mission_item_candidate_physical_execution_invoked"
                ]
                is False
            )
            assert digital_twin_summary["px4_mission_item_candidate_hash"] == (
                px4_mission_item_candidate["sha256"]
            )
            assert digital_twin_summary[
                "px4_mission_item_candidate_blocked_reasons"
            ] == [
                "weather_policy_gate_blocked",
                "takeoff_anchor_missing",
            ]
            assert digital_twin_summary["sitl_binding_gate_status"] == "blocked"
            assert digital_twin_summary["sitl_binding_gate_binding_allowed"] is False
            assert digital_twin_summary["sitl_binding_gate_binding_eligible"] is False
            assert (
                digital_twin_summary["sitl_binding_gate_operator_approval_required"]
                is True
            )
            assert digital_twin_summary["sitl_binding_gate_server_opt_in_required"] is True
            assert digital_twin_summary["sitl_binding_gate_observed_facts_only"] is True
            assert (
                digital_twin_summary[
                    "sitl_binding_gate_px4_mission_item_candidate_status"
                ]
                == "blocked_by_weather_policy_gate"
            )
            assert digital_twin_summary["sitl_binding_gate_candidate_item_count"] == 0
            assert (
                digital_twin_summary["sitl_binding_gate_px4_mission_upload_allowed"]
                is False
            )
            assert (
                digital_twin_summary["sitl_binding_gate_mavlink_dispatch_performed"]
                is False
            )
            assert digital_twin_summary["sitl_binding_gate_sitl_execution_bound"] is False
            assert (
                digital_twin_summary["sitl_binding_gate_gazebo_execution_invoked"]
                is False
            )
            assert (
                digital_twin_summary["sitl_binding_gate_hardware_target_allowed"]
                is False
            )
            assert (
                digital_twin_summary["sitl_binding_gate_physical_execution_invoked"]
                is False
            )
            assert (
                digital_twin_summary[
                    "sitl_binding_gate_approval_free_stronger_execution_allowed"
                ]
                is False
            )
            assert digital_twin_summary["sitl_binding_gate_hash"] == (
                sitl_binding_gate["sha256"]
            )
            assert digital_twin_summary["sitl_binding_gate_blocked_reasons"] == [
                "weather_policy_gate_blocked",
                "takeoff_anchor_missing",
            ]
            assert (
                digital_twin_summary["digital_twin_stage_detail"]
                == "sitl_binding_gate_blocked"
            )
            ambiguous_summary = ambiguous_digital_twin["summary"]
            assert ambiguous_digital_twin["terrain_heightmap_artifact"] is None
            assert ambiguous_digital_twin["terrain_heightmap_file_artifact"] is None
            assert ambiguous_digital_twin["gazebo_world_candidate"] is None
            assert ambiguous_digital_twin["gazebo_world_artifact"] is None
            assert ambiguous_digital_twin["coordinate_transform_candidate"] is None
            assert (
                ambiguous_digital_twin["digital_twin_px4_mission_item_candidate"]
                is None
            )
            assert ambiguous_digital_twin["digital_twin_sitl_binding_gate"] is None
            assert ambiguous_summary["terrain_heightmap_artifact_ref"] == ""
            assert ambiguous_summary["terrain_heightmap_file_artifact_ref"] == ""
            assert ambiguous_summary["gazebo_world_candidate_ref"] == ""
            assert ambiguous_summary["gazebo_world_artifact_ref"] == ""
            assert ambiguous_summary["coordinate_transform_candidate_ref"] == ""
            assert (
                ambiguous_summary["digital_twin_px4_mission_item_candidate_ref"]
                == ""
            )
            assert ambiguous_summary["digital_twin_sitl_binding_gate_ref"] == ""
            assert ambiguous_summary["heightmap_artifact_status"] == "not_generated"
            assert ambiguous_summary["heightmap_artifact_materialized"] is False
            assert ambiguous_summary["heightmap_artifact_gazebo_world_generated"] is False
            assert ambiguous_summary["heightmap_file_artifact_status"] == "not_generated"
            assert ambiguous_summary["heightmap_file_materialized"] is False
            assert ambiguous_summary["gazebo_world_candidate_status"] == "not_generated"
            assert ambiguous_summary["gazebo_world_artifact_status"] == "not_generated"
            assert (
                ambiguous_summary["coordinate_transform_candidate_status"]
                == "not_generated"
            )
            assert (
                ambiguous_summary["px4_mission_item_candidate_status"]
                == "not_generated"
            )
            assert ambiguous_summary["px4_mission_item_candidate_item_count"] == 0
            assert (
                ambiguous_summary[
                    "px4_mission_item_candidate_px4_mission_upload_allowed"
                ]
                is False
            )
            assert ambiguous_summary["sitl_binding_gate_status"] == "not_generated"
            assert ambiguous_summary["sitl_binding_gate_binding_allowed"] is False
            assert ambiguous_summary["sitl_binding_gate_px4_mission_upload_allowed"] is False
            assert ambiguous_summary["sitl_binding_gate_sitl_execution_bound"] is False
            assert approval["scenario_approval"]["operator_approved"] is True
            assert (
                approval["scenario_approval"]["approval_scope"]
                == "compile_to_bounded_simulation_request_only"
            )
            assert (
                approval["scenario_compile_result"]["scenario_profile"]
                == "mountain_summit_payload_delivery"
            )
            assert (
                approval["scenario_compile_result"]["route_profile"]
                == "staged_ascent_required"
            )
            assert (
                "High-elevation mountain delivery"
                in approval["scenario_compile_result"]["compile_reason"]
            )
            assert (
                approval["bounded_simulation_request"]["runner_kind"]
                == "deterministic_bounded_mission_runner"
            )
            assert (
                approval["bounded_simulation_request"][
                    "deterministic_bounded_runner_invoked"
                ]
                is False
            )
            assert (
                approval["bounded_simulation_request"]["gazebo_execution_invoked"]
                is False
            )
            assert blocked["validation_result"]["validation_status"] == "blocked"
            assert blocked["dry_run_result"]["dry_run_status"] == "blocked"
            assert blocked["dry_run_result"]["route_segment_count"] == 0
            assert blocked_approval_status == 400

            return {
                "base_url": base_url,
                "accepted_validation_status": accepted["validation_result"][
                    "validation_status"
                ],
                "accepted_dry_run_status": accepted["dry_run_result"]["dry_run_status"],
                "accepted_weather_hazards": accepted["summary"][
                    "weather_hazard_labels"
                ],
                "accepted_terrain_hazards": accepted["summary"][
                    "terrain_hazard_labels"
                ],
                "accepted_equipment_incidents": accepted["summary"][
                    "equipment_incident_labels"
                ],
                "accepted_altitude_target_m": accepted["summary"]["altitude_target_m"],
                "accepted_payload_weight_kg": accepted["summary"]["payload_weight_kg"],
                "accepted_feasibility_risks": accepted["summary"][
                    "feasibility_risk_labels"
                ],
                "digital_twin_heightmap_artifact_status": digital_twin_summary[
                    "heightmap_artifact_status"
                ],
                "digital_twin_heightmap_artifact_materialized": digital_twin_summary[
                    "heightmap_artifact_materialized"
                ],
                "digital_twin_heightmap_artifact_sha256": digital_twin_summary[
                    "heightmap_artifact_sha256"
                ],
                "digital_twin_heightmap_artifact_gazebo_world_generated": (
                    digital_twin_summary[
                        "heightmap_artifact_gazebo_world_generated"
                    ]
                ),
                "digital_twin_heightmap_artifact_coordinate_transform_generated": (
                    digital_twin_summary[
                        "heightmap_artifact_coordinate_transform_generated"
                    ]
                ),
                "digital_twin_heightmap_artifact_px4_mission_items_generated": (
                    digital_twin_summary[
                        "heightmap_artifact_px4_mission_items_generated"
                    ]
                ),
                "digital_twin_heightmap_file_artifact_status": digital_twin_summary[
                    "heightmap_file_artifact_status"
                ],
                "digital_twin_heightmap_file_materialized": digital_twin_summary[
                    "heightmap_file_materialized"
                ],
                "digital_twin_heightmap_file_sha256": digital_twin_summary[
                    "heightmap_file_sha256"
                ],
                "digital_twin_heightmap_file_gazebo_world_generated": (
                    digital_twin_summary["heightmap_file_gazebo_world_generated"]
                ),
                "digital_twin_heightmap_file_coordinate_transform_generated": (
                    digital_twin_summary[
                        "heightmap_file_coordinate_transform_generated"
                    ]
                ),
                "digital_twin_heightmap_file_px4_mission_items_generated": (
                    digital_twin_summary["heightmap_file_px4_mission_items_generated"]
                ),
                "digital_twin_gazebo_world_candidate_status": digital_twin_summary[
                    "gazebo_world_candidate_status"
                ],
                "digital_twin_gazebo_world_artifact_status": digital_twin_summary[
                    "gazebo_world_artifact_status"
                ],
                "digital_twin_gazebo_world_format": digital_twin_summary[
                    "gazebo_world_format"
                ],
                "digital_twin_gazebo_world_file_path_or_artifact_uri": (
                    digital_twin_summary["gazebo_world_file_path_or_artifact_uri"]
                ),
                "digital_twin_gazebo_world_heightmap_file_sha256": (
                    digital_twin_summary["gazebo_world_heightmap_file_sha256"]
                ),
                "digital_twin_gazebo_world_file_sha256": digital_twin_summary[
                    "gazebo_world_file_sha256"
                ],
                "digital_twin_gazebo_world_materialized": digital_twin_summary[
                    "gazebo_world_materialized"
                ],
                "digital_twin_gazebo_world_execution_binding_allowed": (
                    digital_twin_summary["gazebo_world_execution_binding_allowed"]
                ),
                "digital_twin_gazebo_world_artifact_gazebo_execution_invoked": (
                    digital_twin_summary[
                        "gazebo_world_artifact_gazebo_execution_invoked"
                    ]
                ),
                "digital_twin_gazebo_world_coordinate_transform_generated": (
                    digital_twin_summary[
                        "gazebo_world_coordinate_transform_generated"
                    ]
                ),
                "digital_twin_gazebo_world_px4_mission_items_generated": (
                    digital_twin_summary["gazebo_world_px4_mission_items_generated"]
                ),
                "digital_twin_gazebo_world_sitl_execution_bound": (
                    digital_twin_summary["gazebo_world_sitl_execution_bound"]
                ),
                "digital_twin_coordinate_transform_candidate_status": (
                    digital_twin_summary["coordinate_transform_candidate_status"]
                ),
                "digital_twin_coordinate_transform_frame_source": (
                    digital_twin_summary["coordinate_transform_frame_source"]
                ),
                "digital_twin_coordinate_transform_frame_target": (
                    digital_twin_summary["coordinate_transform_frame_target"]
                ),
                "digital_twin_coordinate_transform_materialized": (
                    digital_twin_summary["coordinate_transform_materialized"]
                ),
                "digital_twin_coordinate_transform_px4_mission_items_generated": (
                    digital_twin_summary[
                        "coordinate_transform_px4_mission_items_generated"
                    ]
                ),
                "digital_twin_coordinate_transform_sitl_execution_bound": (
                    digital_twin_summary["coordinate_transform_sitl_execution_bound"]
                ),
                "digital_twin_coordinate_transform_gazebo_execution_invoked": (
                    digital_twin_summary[
                        "coordinate_transform_gazebo_execution_invoked"
                    ]
                ),
                "digital_twin_px4_mission_item_candidate_status": (
                    digital_twin_summary["px4_mission_item_candidate_status"]
                ),
                "digital_twin_mission_anchor_candidate_status": (
                    digital_twin_summary["mission_anchor_candidate_status"]
                ),
                "digital_twin_mission_anchor_candidate_takeoff_anchor_ref": (
                    digital_twin_summary["mission_anchor_candidate_takeoff_anchor_ref"]
                ),
                "digital_twin_mission_anchor_candidate_upload_allowed": (
                    digital_twin_summary[
                        "mission_anchor_candidate_px4_mission_upload_allowed"
                    ]
                ),
                "digital_twin_mission_anchor_candidate_dispatch_performed": (
                    digital_twin_summary[
                        "mission_anchor_candidate_mavlink_dispatch_performed"
                    ]
                ),
                "digital_twin_mission_anchor_candidate_sitl_bound": (
                    digital_twin_summary["mission_anchor_candidate_sitl_execution_bound"]
                ),
                "digital_twin_mission_anchor_candidate_gazebo_invoked": (
                    digital_twin_summary[
                        "mission_anchor_candidate_gazebo_execution_invoked"
                    ]
                ),
                "digital_twin_px4_mission_item_candidate_item_count": (
                    digital_twin_summary["px4_mission_item_candidate_item_count"]
                ),
                "digital_twin_px4_mission_item_candidate_upload_allowed": (
                    digital_twin_summary[
                        "px4_mission_item_candidate_px4_mission_upload_allowed"
                    ]
                ),
                "digital_twin_px4_mission_item_candidate_dispatch_performed": (
                    digital_twin_summary[
                        "px4_mission_item_candidate_mavlink_dispatch_performed"
                    ]
                ),
                "digital_twin_px4_mission_item_candidate_sitl_bound": (
                    digital_twin_summary["px4_mission_item_candidate_sitl_execution_bound"]
                ),
                "digital_twin_px4_mission_item_candidate_gazebo_invoked": (
                    digital_twin_summary[
                        "px4_mission_item_candidate_gazebo_execution_invoked"
                    ]
                ),
                "digital_twin_px4_mission_item_candidate_blocked_reasons": (
                    digital_twin_summary["px4_mission_item_candidate_blocked_reasons"]
                ),
                "digital_twin_sitl_binding_gate_status": (
                    digital_twin_summary["sitl_binding_gate_status"]
                ),
                "digital_twin_sitl_binding_gate_allowed": (
                    digital_twin_summary["sitl_binding_gate_binding_allowed"]
                ),
                "digital_twin_sitl_binding_gate_eligible": (
                    digital_twin_summary["sitl_binding_gate_binding_eligible"]
                ),
                "digital_twin_sitl_binding_gate_operator_approval_required": (
                    digital_twin_summary[
                        "sitl_binding_gate_operator_approval_required"
                    ]
                ),
                "digital_twin_sitl_binding_gate_server_opt_in_required": (
                    digital_twin_summary["sitl_binding_gate_server_opt_in_required"]
                ),
                "digital_twin_sitl_binding_gate_upload_allowed": (
                    digital_twin_summary[
                        "sitl_binding_gate_px4_mission_upload_allowed"
                    ]
                ),
                "digital_twin_sitl_binding_gate_dispatch_performed": (
                    digital_twin_summary[
                        "sitl_binding_gate_mavlink_dispatch_performed"
                    ]
                ),
                "digital_twin_sitl_binding_gate_sitl_bound": (
                    digital_twin_summary["sitl_binding_gate_sitl_execution_bound"]
                ),
                "digital_twin_sitl_binding_gate_gazebo_invoked": (
                    digital_twin_summary["sitl_binding_gate_gazebo_execution_invoked"]
                ),
                "digital_twin_sitl_binding_gate_blocked_reasons": (
                    digital_twin_summary["sitl_binding_gate_blocked_reasons"]
                ),
                "eligible_sitl_binding_gate_status": (
                    eligible_binding_gate.binding_gate_status
                ),
                "eligible_sitl_binding_gate_eligible": (
                    eligible_binding_gate.binding_eligible
                ),
                "eligible_sitl_binding_gate_allowed": (
                    eligible_binding_gate.binding_allowed
                ),
                "eligible_sitl_binding_gate_upload_allowed": (
                    eligible_binding_gate.px4_mission_upload_allowed
                ),
                "eligible_sitl_binding_gate_dispatch_performed": (
                    eligible_binding_gate.mavlink_dispatch_performed
                ),
                "eligible_sitl_binding_gate_sitl_bound": (
                    eligible_binding_gate.sitl_execution_bound
                ),
                "eligible_sitl_binding_gate_gazebo_invoked": (
                    eligible_binding_gate.gazebo_execution_invoked
                ),
                "digital_twin_stage_detail": (
                    digital_twin_summary["digital_twin_stage_detail"]
                ),
                "ambiguous_heightmap_artifact_status": ambiguous_summary[
                    "heightmap_artifact_status"
                ],
                "ambiguous_heightmap_file_artifact_status": ambiguous_summary[
                    "heightmap_file_artifact_status"
                ],
                "ambiguous_gazebo_world_candidate_status": ambiguous_summary[
                    "gazebo_world_candidate_status"
                ],
                "ambiguous_gazebo_world_artifact_status": ambiguous_summary[
                    "gazebo_world_artifact_status"
                ],
                "ambiguous_coordinate_transform_candidate_status": ambiguous_summary[
                    "coordinate_transform_candidate_status"
                ],
                "ambiguous_px4_mission_item_candidate_status": ambiguous_summary[
                    "px4_mission_item_candidate_status"
                ],
                "ambiguous_sitl_binding_gate_status": ambiguous_summary[
                    "sitl_binding_gate_status"
                ],
                "approval_status": approval["summary"]["approval_status"],
                "approval_scope": approval["summary"]["approval_scope"],
                "scenario_profile": approval["summary"]["scenario_profile"],
                "route_profile": approval["summary"]["route_profile"],
                "compile_reason": approval["summary"]["compile_reason"],
                "runner_kind": approval["summary"]["runner_kind"],
                "deterministic_bounded_runner_invoked": approval["summary"][
                    "deterministic_bounded_runner_invoked"
                ],
                "approved_gazebo_execution_invoked": approval["summary"][
                    "gazebo_execution_invoked"
                ],
                "blocked_approval_status": blocked_approval_status,
                "blocked_validation_status": blocked["validation_result"][
                    "validation_status"
                ],
                "blocked_reasons": blocked["validation_result"]["blocked_reasons"],
                "gazebo_execution_invoked": accepted["dry_run_result"][
                    "gazebo_execution_invoked"
                ],
                "hardware_target_allowed": accepted["dry_run_result"][
                    "hardware_target_allowed"
                ],
                "physical_execution_invoked": accepted["dry_run_result"][
                    "physical_execution_invoked"
                ],
            }
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
