"""Planning-only contracts migrated from standalone smoke wrappers."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.runtime.digital_twin_mission_environment import (
    DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION,
    build_digital_twin_stage1_epic_exit_result,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboMissionDesignerSITLExecutionRequest,
    PX4GazeboMissionScenarioDesignerError,
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    build_px4_gazebo_mission_designer_sitl_execution_request,
    run_px4_gazebo_mission_scenario_designer,
)


NOW = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def prepared_scenario() -> tuple[dict, dict]:
    proposed = run_px4_gazebo_mission_scenario_designer(
        prompt="3000メートルの山頂に5キロの水を届けるミッションを作成して",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=proposed["scenario_proposal"],
        validation=proposed["validation_result"],
        now=NOW,
    )
    return proposed, approved


def test_stage1_epic_exit_is_planning_only_and_weather_blocked() -> None:
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="10km先の3000mの山小屋に水3kgを届けて、天候は雨",
        now=NOW,
    )
    result = build_digital_twin_stage1_epic_exit_result(
        mission_designer_result=designed,
        completed_at=NOW,
    )

    assert result.schema_version == DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION
    assert result.stage1_epic_exit_complete is True
    assert result.requested_distance_km == 10.0
    assert result.requested_altitude_m == 3000.0
    assert result.payload_weight_kg == 3.0
    assert result.rain_or_precipitation is True
    assert result.route_plan_status == "blocked_by_weather_policy_gate"
    assert result.weather_policy_gate_status == "blocked_for_planning"
    assert result.operator_escalation_required is True
    assert result.external_weather_required is True
    assert result.external_weather_observed is False
    assert result.digital_twin_world_generated is False
    assert result.sitl_world_binding_status == "not_generated"
    assert result.coordinate_transform_status == "not_generated"
    assert result.px4_mission_items_generated is False
    assert result.gazebo_execution_invoked is False
    assert result.px4_mission_upload_allowed is False
    assert result.mavlink_dispatch_allowed is False
    assert result.hardware_target_allowed is False
    assert result.physical_execution_invoked is False
    assert result.epic_exit_hash == result.sha256


def test_prompt_altitude_does_not_misread_wind_speed_and_binds_coordinate_route() -> None:
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="東京駅から神田駅まで、高度45mで飛行し、風速3m/sにしてください。",
        coordinate_route={
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6944731,
            "dropoff_longitude": 139.7706981,
            "dropoff_roof_height_agl_m": 30.0,
            "terrain_clearance_agl_m": 30.0,
        },
        now=NOW,
    )

    proposal = designed["scenario_proposal"]
    route = designed["mission_designer_coordinate_pair_route"]
    assert proposal["altitude_target_m"] == 45
    assert proposal["altitude_target_m"] != 3
    assert route["altitude_target_m"] == 45.0
    assert route["altitude_source"] == "operator_instruction"
    assert route["dropoff_roof_height_agl_m"] == 45.0
    assert route["terrain_clearance_agl_m"] == 45.0


def test_sitl_execution_request_prepares_but_does_not_execute(
    prepared_scenario: tuple[dict, dict],
) -> None:
    proposed, approved = prepared_scenario
    request = build_px4_gazebo_mission_designer_sitl_execution_request(
        proposal=proposed["scenario_proposal"],
        validation=proposed["validation_result"],
        approval=approved["scenario_approval"],
        compile_result=approved["scenario_compile_result"],
        bounded_simulation_request=approved["bounded_simulation_request"],
        now=NOW,
    )

    assert (
        request.schema_version
        == PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION
    )
    assert request.target_endpoint == PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT
    assert request.target_endpoint_whitelisted is True
    assert request.requires_explicit_execution_approval is True
    for field in (
        "execution_invoked",
        "gazebo_execution_invoked",
        "external_dispatch_performed",
        "mavlink_dispatch_performed",
        "px4_mission_upload_performed",
        "px4_mission_upload_allowed",
        "hardware_target_allowed",
        "real_hardware_target",
        "physical_execution_invoked",
        "ros_dispatch_performed",
        "actuator_execution_performed",
        "approval_free_dispatch_allowed",
    ):
        assert getattr(request, field) is False, field


def test_sitl_execution_request_rejects_authority_override(
    prepared_scenario: tuple[dict, dict],
) -> None:
    proposed, approved = prepared_scenario
    request = build_px4_gazebo_mission_designer_sitl_execution_request(
        proposal=proposed["scenario_proposal"],
        validation=proposed["validation_result"],
        approval=approved["scenario_approval"],
        compile_result=approved["scenario_compile_result"],
        bounded_simulation_request=approved["bounded_simulation_request"],
        now=NOW,
    )
    payload = request.model_dump(mode="json")
    payload["external_dispatch_performed"] = True

    with pytest.raises(ValidationError):
        PX4GazeboMissionDesignerSITLExecutionRequest.model_validate(payload)

    with pytest.raises(PX4GazeboMissionScenarioDesignerError):
        build_px4_gazebo_mission_designer_sitl_execution_request(
            proposal=proposed["scenario_proposal"],
            validation=proposed["validation_result"],
            approval=approved["scenario_approval"],
            compile_result=approved["scenario_compile_result"],
            bounded_simulation_request=approved["bounded_simulation_request"],
            now=NOW,
            metadata={"mavlink_command": "not allowed"},
        )
