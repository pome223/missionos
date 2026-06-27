#!/usr/bin/env python3
"""Runtime smoke for prepared Mission Designer SITL execution requests (#483)."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from pydantic import ValidationError

from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboMissionDesignerSITLExecutionRequest,
    PX4GazeboMissionScenarioDesignerError,
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    build_px4_gazebo_mission_designer_sitl_execution_request,
    run_px4_gazebo_mission_scenario_designer,
)


def main() -> int:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    proposed = run_px4_gazebo_mission_scenario_designer(
        prompt=(
            "３０００メートルの山の山頂に重さ５キロの水を届ける" "ミッションを作成して"
        ),
        now=now,
    )
    approval_result = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=proposed["scenario_proposal"],
        validation=proposed["validation_result"],
        now=now,
    )
    execution_request = build_px4_gazebo_mission_designer_sitl_execution_request(
        proposal=proposed["scenario_proposal"],
        validation=proposed["validation_result"],
        approval=approval_result["scenario_approval"],
        compile_result=approval_result["scenario_compile_result"],
        bounded_simulation_request=approval_result["bounded_simulation_request"],
        now=now,
    )

    safety_override_failed_closed = False
    payload = execution_request.model_dump(mode="json")
    payload["external_dispatch_performed"] = True
    try:
        PX4GazeboMissionDesignerSITLExecutionRequest.model_validate(payload)
    except ValidationError:
        safety_override_failed_closed = True

    command_like_metadata_failed_closed = False
    try:
        build_px4_gazebo_mission_designer_sitl_execution_request(
            proposal=proposed["scenario_proposal"],
            validation=proposed["validation_result"],
            approval=approval_result["scenario_approval"],
            compile_result=approval_result["scenario_compile_result"],
            bounded_simulation_request=approval_result["bounded_simulation_request"],
            now=now,
            metadata={"mavlink_command": "not allowed"},
        )
    except PX4GazeboMissionScenarioDesignerError:
        command_like_metadata_failed_closed = True

    summary = {
        "mission_designer_sitl_execution_request_smoke_passed": True,
        "schema_version": execution_request.schema_version,
        "execution_request_id": execution_request.execution_request_id,
        "scenario_proposal_ref": execution_request.scenario_proposal_ref,
        "validation_ref": execution_request.validation_ref,
        "approval_ref": execution_request.approval_ref,
        "compile_result_ref": execution_request.compile_result_ref,
        "bounded_simulation_request_ref": (
            execution_request.bounded_simulation_request_ref
        ),
        "request_status": execution_request.request_status,
        "preparation_scope": execution_request.preparation_scope,
        "execution_mode": execution_request.execution_mode,
        "target_endpoint": execution_request.target_endpoint,
        "target_endpoint_whitelisted": execution_request.target_endpoint_whitelisted,
        "requires_explicit_execution_approval": (
            execution_request.requires_explicit_execution_approval
        ),
        "execution_invoked": execution_request.execution_invoked,
        "gazebo_execution_invoked": execution_request.gazebo_execution_invoked,
        "external_dispatch_performed": execution_request.external_dispatch_performed,
        "mavlink_dispatch_performed": execution_request.mavlink_dispatch_performed,
        "px4_mission_upload_performed": (
            execution_request.px4_mission_upload_performed
        ),
        "px4_mission_upload_allowed": execution_request.px4_mission_upload_allowed,
        "hardware_target_allowed": execution_request.hardware_target_allowed,
        "real_hardware_target": execution_request.real_hardware_target,
        "physical_execution_invoked": execution_request.physical_execution_invoked,
        "ros_dispatch_performed": execution_request.ros_dispatch_performed,
        "actuator_execution_performed": execution_request.actuator_execution_performed,
        "approval_free_dispatch_allowed": (
            execution_request.approval_free_dispatch_allowed
        ),
        "safety_override_failed_closed": safety_override_failed_closed,
        "command_like_metadata_failed_closed": command_like_metadata_failed_closed,
        "environment_limitations": [
            "prepared SITL execution request only; no Gazebo/PX4 container was started",
            "MAVLink mission upload remains unavailable until a later explicit opt-in execution path",
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert (
        summary["schema_version"]
        == PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION
    )
    assert summary["target_endpoint"] == PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT
    assert summary["target_endpoint_whitelisted"] is True
    assert summary["requires_explicit_execution_approval"] is True
    assert summary["execution_invoked"] is False
    assert summary["gazebo_execution_invoked"] is False
    assert summary["external_dispatch_performed"] is False
    assert summary["mavlink_dispatch_performed"] is False
    assert summary["px4_mission_upload_performed"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["real_hardware_target"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["ros_dispatch_performed"] is False
    assert summary["actuator_execution_performed"] is False
    assert summary["approval_free_dispatch_allowed"] is False
    assert summary["safety_override_failed_closed"] is True
    assert summary["command_like_metadata_failed_closed"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
