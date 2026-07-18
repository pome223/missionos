from __future__ import annotations

from hashlib import sha256
import json

import pytest
from rich.console import Console

from missionos_cli import cli as missionos_cli
from missionos_cli.route_evidence_image import (
    mission_route_evidence_svg,
    write_mission_route_evidence_artifacts,
)

pytestmark = pytest.mark.contract


def _task_payload() -> dict:
    return {
        "task_id": "task_map_weather_altitude",
        "status": "running",
        "artifacts": {
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.01,
                "dropoff_longitude": 139.02,
                "wind_speed_mps": 8.2,
                "wind_direction_deg": 270.0,
                "wind_gust_mps": 12.5,
                "temperature_c": 18.4,
                "pressure_hpa": 1008.0,
                "precipitation_mm_per_hour": 0.3,
            },
            "missionos_auto_mission_compilation": {
                "planned_route_m": 1000.0,
                "terrain_clearance_target_m": 30.0,
                "terrain_clearance_profile": [
                    {
                        "fraction": 0.0,
                        "distance_m": 0.0,
                        "terrain_elevation_m": 570.0,
                        "target_clearance_m": 30.0,
                        "mission_altitude_m": 30.0,
                    },
                    {
                        "fraction": 1.0,
                        "distance_m": 1000.0,
                        "terrain_elevation_m": 3700.0,
                        "target_clearance_m": 30.0,
                        "mission_altitude_m": 3160.0,
                    },
                ],
            },
            "missionos_auto_mission_runtime_snapshot": {
                "local_x_m": 100.0,
                "local_y_m": 20.0,
                "battery_remaining_percent": 80.0,
                "altitude_above_home_m": 30.0,
                "terrain_elevation_m": 570.0,
                "terrain_clearance_m": 30.0,
                "terrain_clearance_target_m": 30.0,
                "terrain_clearance_margin_m": 0.0,
                "terrain_clearance_status": "ok",
            },
        },
    }


def test_mission_map_model_includes_altitude_references_and_weather() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_task_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert model["telemetry"]["altitude_amsl_m"] == 600.0
    assert model["telemetry"]["home_relative_altitude_m"] == 30.0
    assert model["telemetry"]["agl_m"] == 30.0
    assert model["telemetry"]["agl_target_m"] == 30.0
    assert model["telemetry"]["destination_target_amsl_m"] == 3730.0
    assert model["telemetry"]["climb_to_destination_m"] == 3130.0
    assert model["weather"]["wind_speed_mps"] == 8.2
    assert model["weather"]["wind_direction_deg"] == 270.0
    assert model["weather"]["wind_gust_mps"] == 12.5
    assert model["weather"]["temperature_c"] == 18.4
    assert model["weather"]["pressure_hpa"] == 1008.0
    assert model["weather"]["precipitation_mm_per_hour"] == 0.3


def test_mission_map_html_renders_altitude_and_weather_panels() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_task_payload(),
        provider="osm",
        live_task_url=None,
    )
    html = missionos_cli._mission_map_html(model)

    assert "altitudeSummary" in html
    assert "weatherSummary" in html
    assert "alt(home)=" in html
    assert "AMSL" in html
    assert "AGL" in html
    assert "wind=" in html
    assert "temp=" in html


def _segmented_route_payload() -> dict:
    return {
        "task_id": "task_segmented_route_map",
        "status": "completed",
        "artifacts": {
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.001,
                "dropoff_longitude": 139.0,
            },
            "missionos_auto_mission_runtime_replay": {
                "flight_path_profile": [
                    {
                        "sample_index": 1,
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "battery_remaining_percent": 90.0,
                    },
                    {
                        "sample_index": 2,
                        "local_x_m": 111.32,
                        "local_y_m": 0.0,
                        "battery_remaining_percent": 82.0,
                    },
                ]
            },
            "missionos_auto_mission_live_trajectory": {
                "schema_version": "missionos_auto_mission_live_trajectory.v1",
                "samples": [
                    {
                        "sample_index": 1,
                        "segment_index": 0,
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "battery_remaining_percent": 90.0,
                        "battery_state_source": "px4_battery_status",
                        "battery_sample_accepted": True,
                    },
                    {
                        "sample_index": 100000,
                        "segment_index": 1,
                        "segment_break_reason": "sample_index_gap",
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "battery_remaining_percent": 81.0,
                        "battery_state_source": "px4_battery_status",
                        "battery_sample_accepted": True,
                        "observed_at": "2026-07-17T05:56:00+00:00",
                    },
                ],
            },
            "missionos_auto_mission_runtime_snapshot": {
                "sample_index": 100001,
                "local_x_m": 0.0,
                "local_y_m": 0.0,
                "battery_remaining_percent": 97.6,
                "battery_state_source": None,
                "battery_sample_accepted": True,
                "observed_at": "2026-07-17T05:56:58+00:00",
            },
            "obstacle_manifest": {
                "gazebo_obstacle_model_spawned": True,
                "obstacles": [
                    {
                        "name": "dropoff_blocker",
                        "x_m": 111.32,
                        "y_m": 0.0,
                        "size_x_m": 18.0,
                        "size_y_m": 18.0,
                        "gazebo_obstacle_model_spawned": True,
                    }
                ],
            },
        },
    }


def test_map_does_not_join_replay_to_later_return_observation() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_segmented_route_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert [len(segment) for segment in model["observed_segments"]] == [2, 1]
    assert len(model["observed_points"]) == 3
    assert model["observed_segments"][0][-1]["lat"] == pytest.approx(35.001)
    assert model["observed_segments"][1][0]["lat"] == pytest.approx(35.0)
    assert model["observed_segment_details"][1]["role"] == "return_to_home"
    assert model["observed_gaps"][0]["evidence_status"] == ("not_observed_between_endpoints")
    assert model["latest"]["lat"] == pytest.approx(35.0)


def _recovery_gap_route_payload() -> dict:
    payload = _segmented_route_payload()
    payload["task_id"] = "task_recovery_gap_route_map"
    artifacts = payload["artifacts"]
    artifacts["mission_designer_coordinate_pair_route"]["dropoff_latitude"] = 35.00134722
    artifacts["missionos_auto_mission_runtime_replay"]["flight_path_profile"] = [
        {"sample_index": 0, "elapsed_s": 0.0, "local_x_m": 0.0, "local_y_m": 0.0},
        {"sample_index": 1, "elapsed_s": 1.0, "local_x_m": 50.0, "local_y_m": 0.0},
        {"sample_index": 2, "elapsed_s": 2.0, "local_x_m": 100.0, "local_y_m": 0.0},
        {"sample_index": 100002, "elapsed_s": 14.0, "local_x_m": 0.0, "local_y_m": 0.0},
    ]
    artifacts["missionos_auto_mission_live_trajectory"]["samples"] = [
        {
            "sample_index": 0,
            "segment_index": 0,
            "elapsed_seconds": 0.0,
            "local_x_m": 0.0,
            "local_y_m": 0.0,
        },
        {
            "sample_index": 1,
            "segment_index": 0,
            "elapsed_seconds": 1.0,
            "local_x_m": 50.0,
            "local_y_m": 0.0,
        },
        {
            "sample_index": 2,
            "segment_index": 0,
            "elapsed_seconds": 2.0,
            "local_x_m": 100.0,
            "local_y_m": 0.0,
        },
        {
            "sample_index": 3,
            "segment_index": 0,
            "elapsed_seconds": 10.5,
            "local_x_m": 125.0,
            "local_y_m": 31.225,
        },
        {
            "sample_index": 4,
            "segment_index": 0,
            "elapsed_seconds": 11.5,
            "local_x_m": 140.0,
            "local_y_m": 10.0,
        },
        {
            "sample_index": 5,
            "segment_index": 0,
            "elapsed_seconds": 12.5,
            "local_x_m": 150.0,
            "local_y_m": 0.0,
        },
        {
            "sample_index": 100001,
            "segment_index": 1,
            "segment_break_reason": "sample_index_gap",
            "elapsed_seconds": 13.0,
            "local_x_m": 150.0,
            "local_y_m": 0.0,
        },
        {
            "sample_index": 100002,
            "segment_index": 1,
            "elapsed_seconds": 14.0,
            "local_x_m": 0.0,
            "local_y_m": 0.0,
        },
    ]
    artifacts["missionos_auto_mission_probe_observed"] = {
        "monitor": {
            "operator_recovery": {
                "command": {
                    "action": "avoid_obstacle",
                    "status": "target_reached",
                    "target": {"target_x_m": 125.0, "target_y_m": 31.225},
                    "target_reached": True,
                    "resume_auto_status": "resumed_auto_mission",
                    "maneuver_observation_samples": [
                        {"x_m": 108.0, "y_m": 10.0},
                        {"x_m": 117.0, "y_m": 22.0},
                        {"x_m": 125.0, "y_m": 31.225},
                    ],
                }
            }
        }
    }
    artifacts["missionos_runtime_recovery_proposal_revalidation"] = {
        "current_position": {
            "local_x_m": 100.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
        }
    }
    artifacts["missionos_runtime_recovery_last_proposal"] = {
        "proposal_id": "runtime_recovery_proposal_fixture",
        "proposal_origin_sha256": "a" * 64,
        "proposal_origin": {
            "origin_kind": "hosted_llm",
            "provider": "google_adk_gemini",
            "model_id": "gemini-3.1-flash-lite",
            "invocation_kind": "google_adk_function_tool_call",
        },
    }
    artifacts["missionos_runtime_recovery_dispatch_request"] = {
        "proposal_id": "runtime_recovery_proposal_fixture",
        "operator_approved": True,
        "explicit_recovery_dispatch_approval": True,
        "approval_ref": "runtime_recovery_maneuver_approval_fixture",
        "recovery_action": "avoid_obstacle",
    }
    artifacts["missionos_runtime_recovery_last_attempt"] = {
        "recovery_action": "avoid_obstacle",
        "target_reached": True,
        "resume_status": "resumed_auto_mission",
        "simulator_execution_observed": True,
        "resume_safety_verification": {
            "resume_mission_current_seq": 12,
            "resume_mission_seq_after_obstacle": 12,
            "resume_mission_current_seq_observed": True,
        },
    }
    return payload


def test_map_prefers_complete_live_trace_and_marks_recovery_telemetry_gap() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_recovery_gap_route_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert model["observed_trace_source"] == "missionos_auto_mission_live_trajectory"
    assert [len(segment) for segment in model["observed_segments"]] == [3, 3, 2]
    assert [detail["role"] for detail in model["observed_segment_details"]] == [
        "outbound",
        "outbound_after_observation_gap",
        "return_to_home",
    ]
    assert model["observed_gaps"] == [
        {
            "from": model["observed_segments"][0][-1],
            "to": model["observed_segments"][1][0],
            "reason": "telemetry_time_and_distance_gap",
            "distance_m": 40.0,
            "elapsed_gap_s": 8.5,
            "from_sample_index": 2,
            "to_sample_index": 3,
            "evidence_status": "not_observed_between_endpoints",
        }
    ]
    assert model["avoidance"]["start"]["x_m"] == 100.0
    assert len(model["avoidance"]["samples"]) == 3
    assert model["avoidance"]["target_beyond_obstacle"] is True
    assert model["avoidance"]["geometry_status"] == ("lateral_bypass_target_beyond_obstacle")
    assert model["avoidance"]["route_rejoin"]["cross_track_m"] == 10.0
    assert len(model["obstacles"][0]["footprint"]) == 4
    assert model["recovery_provenance"]["model_id"] == "gemini-3.1-flash-lite"
    assert model["recovery_provenance"]["operator_approved"] is True
    assert model["recovery_provenance"]["target_reached"] is True
    assert model["recovery_provenance"]["resume_mission_current_seq"] == 12

    html = missionos_cli._mission_map_html(model)
    assert "gaps are never drawn as movement" in html
    assert "outbound →" in html
    assert "return home →" in html
    assert "Recovery bypass →" in html
    assert "3 Bypass" in html
    assert "4 Rejoin" in html
    assert 'class="obstacle-footprint"' in html
    assert 'id="terminalEvidence"' in html
    assert "source task artifacts—not this image—remain authoritative" in html


def test_terminal_route_evidence_image_is_hash_bound_to_saved_observations(
    tmp_path,
) -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_recovery_gap_route_payload(),
        provider="osm",
        live_task_url=None,
    )

    svg = mission_route_evidence_svg(model)
    assert "MissionOS E2E Route Evidence" in svg
    assert "1 Start" in svg
    assert "2 Recovery" in svg
    assert "3 Bypass" in svg
    assert "4 Rejoin" in svg
    assert "5 Dropoff" in svg
    assert "6 Home" in svg
    assert "gemini-3.1-flash-lite" in svg
    assert "waypoint 12" in svg
    assert "display-only summary, not verifier authority" in svg
    assert "telemetry missing" not in svg

    generated = write_mission_route_evidence_artifacts(
        model=model,
        output_dir=tmp_path,
    )
    svg_bytes = generated["svg_path"].read_bytes()
    manifest = json.loads(generated["manifest_path"].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "missionos_route_evidence_manifest.v1"
    assert manifest["task_id"] == "task_recovery_gap_route_map"
    assert manifest["task_status"] == "completed"
    assert manifest["svg_sha256"] == sha256(svg_bytes).hexdigest()
    assert manifest["source_payload_sha256"] == generated["source_payload_sha256"]
    assert manifest["display_only"] is True
    assert manifest["verifier_input"] is False
    assert manifest["dispatch_authority_created"] is False
    assert manifest["delivery_completion_claimed"] is False
    assert manifest["physical_execution_claimed"] is False


def test_terminal_route_evidence_is_generated_only_after_terminal_status(
    tmp_path,
) -> None:
    payload = _recovery_gap_route_payload()
    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )
    generated = missionos_cli._write_terminal_route_evidence(
        model=model,
        output_dir=tmp_path,
        stem="terminal",
    )
    assert generated is not None
    assert generated["svg_path"].exists()
    assert generated["manifest_path"].exists()

    payload["status"] = "running"
    running_model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )
    assert (
        missionos_cli._write_terminal_route_evidence(
            model=running_model,
            output_dir=tmp_path,
            stem="running",
        )
        is None
    )


def test_map_surfaces_blocked_dropoff_and_rejects_source_less_battery_reset() -> None:
    payload = _segmented_route_payload()
    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )

    assert model["obstacles"][0]["coincident_with_dropoff"] is True
    assert model["battery"]["display_percent"] == 81.0
    assert model["battery"]["reported_percent"] == 97.6
    assert model["battery"]["source"] == "px4_battery_status"
    assert model["battery"]["status"] == "suspect_reset"
    assert model["battery"]["reset_detected"] is True

    html = missionos_cli._mission_map_html(model)
    assert "5 Blocked" in html
    assert "batterySummary" in html
    assert "rejected_reset" in html

    artifacts = payload["artifacts"]
    console = Console(record=True, color_system=None, width=150)
    console.print(
        missionos_cli._render_flight_map(
            trail=[(0.0, 0.0), (50.0, 0.0)],
            snapshot=artifacts["missionos_auto_mission_runtime_snapshot"],
            artifacts=artifacts,
            status="completed",
            task_id="task_segmented_route_map",
        )
    )
    rendered = console.export_text()
    assert "battery=81.0%" in rendered
    assert "status=suspect_reset" in rendered
    assert "reported=97.6%" in rendered
    assert "rejected_reset=+16.6pp" in rendered
    assert "X/!=blocked dropoff" in rendered

    job_text = "\n".join(missionos_cli._job_operator_summary(payload))
    assert "Battery: 81.0% trusted (reported 97.6%; reset rejected)" in job_text

    operate = missionos_cli._render_operate_status_line(
        artifacts["missionos_auto_mission_runtime_snapshot"],
        artifacts=artifacts,
        status="completed",
        task_id="task_segmented_route_map",
    ).plain
    assert "battery=81.0% trusted (reported 97.6%; reset rejected)" in operate
