#!/usr/bin/env python3
"""Runtime smoke for Digital Twin Stage 1 epic-exit evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from src.runtime.digital_twin_mission_environment import (
    DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION,
    build_digital_twin_stage1_epic_exit_result,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    run_px4_gazebo_mission_scenario_designer,
)


PROMPT = "10km先の3000mの山小屋に水3kgを届けて、天候は雨"
NOW = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


def run_smoke() -> dict:
    designer_result = run_px4_gazebo_mission_scenario_designer(
        prompt=PROMPT,
        now=NOW,
    )
    epic_exit = build_digital_twin_stage1_epic_exit_result(
        mission_designer_result=designer_result,
        completed_at=NOW,
    )
    route_plan = designer_result["digital_twin_route_plan"]
    weather_gate = designer_result["weather_environment_policy_gate"]
    summary = {
        "digital_twin_stage1_epic_exit_smoke_passed": True,
        "schema_version": epic_exit.schema_version,
        "result_id": epic_exit.result_id,
        "prompt": epic_exit.prompt,
        "stage1_epic_exit_complete": epic_exit.stage1_epic_exit_complete,
        "requested_distance_km": epic_exit.requested_distance_km,
        "requested_altitude_m": epic_exit.requested_altitude_m,
        "payload_weight_kg": epic_exit.payload_weight_kg,
        "rain_or_precipitation": epic_exit.rain_or_precipitation,
        "route_plan_status": epic_exit.route_plan_status,
        "weather_policy_gate_status": epic_exit.weather_policy_gate_status,
        "operator_escalation_required": epic_exit.operator_escalation_required,
        "external_weather_required": epic_exit.external_weather_required,
        "external_weather_observed": epic_exit.external_weather_observed,
        "blocked_reasons": list(epic_exit.blocked_reasons),
        "digital_twin_world_generated": epic_exit.digital_twin_world_generated,
        "sitl_world_binding_status": epic_exit.sitl_world_binding_status,
        "coordinate_transform_status": epic_exit.coordinate_transform_status,
        "px4_mission_items_generated": epic_exit.px4_mission_items_generated,
        "gazebo_execution_invoked": epic_exit.gazebo_execution_invoked,
        "px4_mission_upload_allowed": epic_exit.px4_mission_upload_allowed,
        "mavlink_dispatch_allowed": epic_exit.mavlink_dispatch_allowed,
        "hardware_target_allowed": epic_exit.hardware_target_allowed,
        "physical_execution_invoked": epic_exit.physical_execution_invoked,
        "epic_exit_hash_equals_sha256": epic_exit.epic_exit_hash == epic_exit.sha256,
        "digital_twin_route_plan_ref": epic_exit.digital_twin_route_plan_ref,
        "weather_environment_policy_gate_ref": (
            epic_exit.weather_environment_policy_gate_ref
        ),
        "route_plan_hash_equals_sha256": (
            route_plan["route_plan_hash"] == route_plan["sha256"]
        ),
        "weather_gate_hash_equals_sha256": (
            weather_gate["gate_hash"] == weather_gate["sha256"]
        ),
        "environment_limitations": [
            "Stage 1 planning-only smoke; no geocode, live DEM, or live weather fetch",
            "No Gazebo world generation, PX4 mission item generation, PX4 upload, hardware, or physical execution",
        ],
    }
    assert epic_exit.schema_version == DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION
    assert summary["stage1_epic_exit_complete"] is True
    assert summary["requested_distance_km"] == 10.0
    assert summary["requested_altitude_m"] == 3000.0
    assert summary["payload_weight_kg"] == 3.0
    assert summary["rain_or_precipitation"] is True
    assert summary["route_plan_status"] == "blocked_by_weather_policy_gate"
    assert summary["weather_policy_gate_status"] == "blocked_for_planning"
    assert summary["operator_escalation_required"] is True
    assert summary["external_weather_required"] is True
    assert summary["external_weather_observed"] is False
    assert summary["digital_twin_world_generated"] is False
    assert summary["sitl_world_binding_status"] == "not_generated"
    assert summary["coordinate_transform_status"] == "not_generated"
    assert summary["px4_mission_items_generated"] is False
    assert summary["gazebo_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["epic_exit_hash_equals_sha256"] is True
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(
        "SMOKE_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
