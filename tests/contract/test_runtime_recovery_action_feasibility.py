from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from src.gateway import server as gateway_server
from src.intelligence import missionos_agent_runtime
from src.runtime.px4_gazebo_route.action_feasibility import (
    action_feasibility_hash_matches,
    SUPPORTED_FEASIBILITY_ACTIONS,
    verify_runtime_recovery_action_candidates,
    verify_runtime_recovery_action_feasibility,
)
from src.runtime.px4_gazebo_route.core_action_feasibility_adapter import (
    PX4_CORE_ADAPTER_ID,
    attach_core_hazard_state,
    verify_runtime_recovery_action_feasibility as verify_core_backed_feasibility,
)
from src.runtime.px4_gazebo_route.hazard_state import (
    build_runtime_recovery_hazard_state,
    hazard_state_hash_matches,
)
from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    build_runtime_recovery_intent,
    compile_runtime_recovery_intent,
    verify_runtime_recovery_reachability,
)


def _policy() -> dict:
    return {
        "policy_ref": "fixture_multi_hazard_policy.v1",
        "battery_return_threshold_percent": 20.0,
        "min_terrain_clearance_m": 30.0,
        "max_recovery_duration_s": 75.0,
        "max_recovery_horizontal_speed_mps": 10.0,
        "max_recovery_vertical_speed_mps": 3.0,
        "max_wind_speed_mps": 6.0,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5.0,
        "minimum_motor_thrust_factor": 0.6,
        "obstacle_minimum_clearance_m": 2.0,
        "temperature_derating_model": {
            "model_id": "fixture_temperature_derating.v1",
            "model_version": "1.0.0",
            "source_refs": ["fixture.calibrated_temperature_table"],
            "uncertainty_percent": 5.0,
            "battery_capacity_factor": 0.95,
            "motor_thrust_factor": 0.9,
        },
        "battery_action_energy_model": {
            "model_id": "fixture_action_energy.v1",
            "model_version": "1.0.0",
            "source_refs": ["fixture.calibrated_action_energy_table"],
            "uncertainty_percent": 5.0,
            "percent_per_meter_horizontal": 0.02,
            "percent_per_meter_climb": 0.08,
            "headwind_multiplier_per_mps": 0.01,
            "payload_energy_multiplier_per_kg": 0.05,
        },
    }


def _telemetry() -> dict:
    return {
        "source": "fixture_multi_hazard_telemetry",
        "sample_index": 200,
        "elapsed_seconds": 48.2,
        "telemetry": {"stale": False, "dropout": False},
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "distance_to_home_m": 25.0,
            "frame_id": "local_ned_xy_altitude_up",
            "source_refs": ["fixture.position"],
        },
        "battery": {
            "remaining_percent": 80.0,
            "source_refs": ["fixture.battery"],
        },
        "wind": {
            "speed_mps": 2.0,
            "gust_mps": 3.0,
            "source_refs": ["fixture.wind"],
        },
        "terrain": {
            "terrain_clearance_m": 50.0,
            "terrain_clearance_target_m": 30.0,
            "frame_id": "amsl",
            "source_refs": ["fixture.terrain"],
        },
        "obstacle": {
            "frame_id": "local_ned_xy_altitude_up",
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "fixture_obstacle",
                        "bounds_local_xyz_m": {
                            "min_x_m": 28.0,
                            "max_x_m": 32.0,
                            "min_y_m": -2.0,
                            "max_y_m": 2.0,
                            "min_z_m": 0.0,
                            "max_z_m": 20.0,
                        },
                        "source_refs": ["fixture.obstacle_manifest"],
                    }
                ]
            },
            "conflict_assessment": {
                "local_avoidance_required": True,
                "source_refs": ["fixture.obstacle_conflict"],
                "nearest_obstacle": {
                    "obstacle_name": "fixture_obstacle",
                    "time_to_conflict_s": 60.0,
                    "source_refs": ["fixture.obstacle_manifest"],
                },
            },
        },
        "temperature": {
            "temperature_c": 35.0,
            "battery_capacity_factor": 0.95,
            "motor_thrust_factor": 0.9,
            "source_refs": ["fixture.temperature"],
        },
        "payload": {
            "mass_kg": 0.5,
            "source_refs": ["fixture.payload_manifest"],
        },
        "landing_zone": {
            "safe": True,
            "source_refs": ["fixture.landing_zone_verifier"],
        },
        "recovery": {
            "performance_observation": {
                "action": "avoid_obstacle",
                "sample_count": 12,
                "duration_seconds": 20.0,
                "horizontal_distance_m": 60.0,
                "observed_horizontal_speed_mps": 10.0,
                "source_refs": ["fixture.prior_bounded_offboard_maneuver"],
            }
        },
    }


def _candidates() -> list[dict]:
    return [
        {
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": {
                "target_x_m": 80.0,
                "target_y_m": 200.0,
                "target_altitude_m": 35.0,
                "source_obstacle_name": "fixture_obstacle",
            },
            "source_refs": ["fixture.avoidance_compiler"],
            "recovery_path": {
                "frame_id": "local_ned_xy_altitude_up",
                "waypoints": [
                    {"x_m": 80.0, "y_m": 200.0, "z_m": 35.0}
                ],
                "source_refs": ["fixture.avoidance_path"],
            },
        },
        {
            "selected_bounded_action": "reroute",
            "proposed_parameters": {
                "target_x_m": 20.0,
                "target_y_m": 10.0,
                "target_altitude_m": 35.0,
            },
            "source_refs": ["fixture.reroute_compiler"],
        },
        {
            "selected_bounded_action": "adjust_altitude",
            "proposed_parameters": {"target_altitude_m": 40.0},
            "source_refs": ["fixture.altitude_compiler"],
        },
        {
            "selected_bounded_action": "return_to_launch",
            "proposed_parameters": {},
            "source_refs": ["fixture.rtl_compiler"],
        },
        {
            "selected_bounded_action": "land",
            "proposed_parameters": {},
            "source_refs": ["fixture.land_compiler"],
        },
    ]


def _state(telemetry: dict | None = None, policy: dict | None = None) -> dict:
    return build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry or _telemetry(),
        recovery_policy=policy or _policy(),
        observed_at="2026-07-23T12:00:00+00:00",
    )


def test_hazard_state_separates_observed_and_derived_facts() -> None:
    state = _state()

    assert state["hazard_state_status"] == "verified"
    assert state["telemetry_cursor"] == {
        "cursor_status": "complete",
        "sample_index": 200,
        "elapsed_seconds": 48.2,
    }
    assert state["policy_ref"] == "fixture_multi_hazard_policy.v1"
    assert len(state["policy_sha256"]) == 64
    assert state["observed_facts"]["temperature_c"]["unit"] == "degC"
    assert (
        state["observed_facts"]["nearest_obstacle_name"]["frame"]
        == "local_ned_xy_altitude_up"
    )
    assert (
        state["derived_facts"]["battery_return_margin_percent"]["fact_status"]
        == "derived"
    )
    assert state["temperature_model"]["model_status"] == "verified"
    assert state["approval_created"] is False
    assert state["dispatch_authority_created"] is False
    assert state["physical_execution_invoked"] is False
    assert state["completion_claimed"] is False


def test_core_adapter_preserves_verified_result_and_authority_boundary() -> None:
    result = verify_core_backed_feasibility(
        candidate=_candidates()[0],
        hazard_state=attach_core_hazard_state(_state()),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == "verified_feasible"
    assert result["core_contract"]["adapter_id"] == PX4_CORE_ADAPTER_ID
    assert result["core_contract"]["status"] == "verified_feasible"
    assert result["core_contract"]["verification_basis"] == "deterministic"
    assert result["core_contract"]["verification_items"][0][
        "item_id"
    ] == "px4_bounded_recovery_feasibility"
    assert result["core_contract"]["approval_created"] is False
    assert result["core_contract"]["dispatch_authority_created"] is False
    assert result["core_contract"]["execution_invoked"] is False
    assert result["core_contract"]["progress_claimed"] is False
    assert result["core_contract"]["completion_claimed"] is False
    assert action_feasibility_hash_matches(result)


def test_pending_legacy_hazard_cannot_bypass_core_adapter() -> None:
    result = verify_core_backed_feasibility(
        candidate=_candidates()[0],
        hazard_state=_state(),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == "unverified"
    assert "action_feasibility_core_hazard_state_missing" in result[
        "unverified_reasons"
    ]
    assert result["core_contract"]["approval_created"] is False
    assert result["core_contract"]["dispatch_authority_created"] is False
    assert action_feasibility_hash_matches(result)


def test_core_hazard_projection_is_hash_bound() -> None:
    state = attach_core_hazard_state(_state())
    assert hazard_state_hash_matches(state)

    state["core_hazard_state"]["assumptions"] = ("tampered",)

    assert not hazard_state_hash_matches(state)


def test_all_required_actions_share_verified_feasibility_contract() -> None:
    result = verify_runtime_recovery_action_candidates(
        candidates=_candidates(),
        hazard_state=_state(),
        recovery_policy=_policy(),
    )

    assert set(result["verified_feasible_actions"]) == SUPPORTED_FEASIBILITY_ACTIONS
    assert result["feasible_candidate_count"] == 5
    assert {
        item["feasibility_status"] for item in result["evaluations"]
    } == {"verified_feasible"}


def test_horizontal_offboard_action_is_unverified_without_observed_performance() -> None:
    telemetry = _telemetry()
    telemetry.pop("recovery")
    policy = {
        **_policy(),
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
    }
    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "unverified"
    assert (
        "action_feasibility_offboard_performance_envelope_unverified"
        in result["unverified_reasons"]
    )
    assert result["maneuver_completion_upper_bound_s"] is None
    assert (
        result["maneuver_duration_model"]["performance_envelope_status"]
        == "unverified"
    )


def test_observed_performance_sets_conservative_duration_bound() -> None:
    telemetry = _telemetry()
    telemetry["recovery"]["performance_observation"].update(
        {
            "duration_seconds": 15.0,
            "horizontal_distance_m": 60.0,
            "observed_horizontal_speed_mps": 4.0,
        }
    )
    policy = {
        **_policy(),
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
    }
    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "verified_feasible"
    model = result["maneuver_duration_model"]
    assert model["observed_horizontal_speed_mps"] == 4.0
    assert model["conservative_horizontal_speed_mps"] == 3.0
    assert model["effective_horizontal_speed_mps"] == 3.0
    assert result["maneuver_completion_upper_bound_s"] == pytest.approx(
        14.317,
        abs=0.001,
    )


def test_separated_mode_wait_still_counts_against_duration_bound() -> None:
    telemetry = _telemetry()
    telemetry["recovery"]["performance_observation"].update({
        "duration_seconds": 15.0, "horizontal_distance_m": 60.0,
        "observed_horizontal_speed_mps": 4.0,
        "measurement_basis": "matched_offboard_pose_samples.v1",
        "observation_status": "measured", "non_movement_duration_seconds": 12.0,
    })
    policy = {**_policy(), "offboard_performance_envelope_required": True,
              "offboard_performance_min_samples": 5,
              "offboard_performance_uncertainty_fraction": 0.25}
    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1], hazard_state=_state(telemetry, policy), recovery_policy=policy,
    )
    assert result["maneuver_duration_model"]["effective_horizontal_speed_mps"] == 3.0
    assert result["maneuver_duration_model"]["effective_setup_seconds"] == 15.0
    assert result["maneuver_completion_upper_bound_s"] == pytest.approx(24.317, abs=0.001)
    # The same motion remains infeasible when non-motion time exceeds the cap.
    telemetry["recovery"]["performance_observation"]["non_movement_duration_seconds"] = 300.0
    blocked = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1], hazard_state=_state(telemetry, policy), recovery_policy=policy,
    )
    assert blocked["feasibility_status"] == "blocked"
    assert "action_feasibility_duration_bound_exceeded" in blocked["blocking_reasons"]


def test_obstacle_avoidance_can_use_longer_hold_bound_only() -> None:
    telemetry = _telemetry()
    telemetry["recovery"].update(
        {
            "action": "safety_hold",
            "request_observed": True,
            "command_ack_observed": True,
            "assist_status": "safety_hold_observed",
            "resume_status": "held_awaiting_operator_recovery_approval",
        }
    )
    telemetry["recovery"]["performance_observation"].update(
        {
            "action": "calibrate_offboard",
            "duration_seconds": 5.4,
            "horizontal_distance_m": 13.8,
            "observed_horizontal_speed_mps": 2.56,
        }
    )
    policy = {
        **_policy(),
        "max_obstacle_avoidance_duration_s": 240.0,
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
    }
    avoid = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[0],
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )
    long_reroute_candidate = deepcopy(_candidates()[0])
    long_reroute_candidate["selected_bounded_action"] = "reroute"
    long_reroute_candidate["proposed_parameters"].pop(
        "source_obstacle_name"
    )
    reroute = verify_runtime_recovery_action_feasibility(
        candidate=long_reroute_candidate,
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert avoid["maneuver_completion_upper_bound_s"] > 75.0
    assert avoid["maneuver_completion_upper_bound_s"] < 150.0
    assert avoid["feasibility_status"] == "verified_feasible"
    assert reroute["feasibility_status"] == "blocked"
    assert (
        "action_feasibility_duration_bound_exceeded"
        in reroute["blocking_reasons"]
    )


def test_insufficient_performance_samples_remain_unverified() -> None:
    telemetry = _telemetry()
    telemetry["recovery"]["performance_observation"]["sample_count"] = 2
    policy = {
        **_policy(),
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
    }
    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "unverified"
    assert (
        "performance_envelope_sample_count_insufficient"
        in result["unverified_reasons"]
    )
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False
    assert result["completion_claimed"] is False


def _calibration_case() -> tuple[dict, dict, dict]:
    telemetry = _telemetry()
    telemetry.pop("recovery")
    telemetry["obstacle"]["conflict_assessment"].update(
        local_avoidance_required=False,
        nearest_obstacle={},
    )
    policy = {
        **_policy(),
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
        "offboard_performance_calibration_enabled": True,
        "offboard_performance_calibration_max_distance_m": 15.0,
        "offboard_performance_calibration_max_altitude_delta_m": 2.0,
        "offboard_performance_calibration_speed_mps": 2.0,
    }
    candidate = {
        "selected_bounded_action": "calibrate_offboard",
        "proposed_parameters": {
            "target_x_m": 10.0,
            "target_y_m": 0.0,
            "target_altitude_m": 30.0,
            "calibration_only": True,
            "resume_original_route": True,
        },
        "source_refs": ["fixture.explicit_operator_calibration"],
    }
    return telemetry, policy, candidate


def test_short_sitl_offboard_calibration_is_verified_without_prior_envelope() -> None:
    telemetry, policy, candidate = _calibration_case()

    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "verified_feasible"
    assert result["horizontal_distance_m"] == 10.0
    assert (
        result["maneuver_duration_model"]["model_id"]
        == "bounded_sitl_offboard_calibration.v1"
    )
    assert result["maneuver_duration_model"]["calibration_speed_mps"] == 2.0
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda telemetry, policy, candidate: candidate[
                "proposed_parameters"
            ].update(target_x_m=20.0),
            "action_feasibility_offboard_calibration_distance_exceeded",
        ),
        (
            lambda telemetry, policy, candidate: candidate[
                "proposed_parameters"
            ].update(target_altitude_m=35.0),
            "action_feasibility_offboard_calibration_altitude_delta_exceeded",
        ),
        (
            lambda telemetry, policy, candidate: telemetry["obstacle"][
                "conflict_assessment"
            ].update(local_avoidance_required=True),
            "action_feasibility_offboard_calibration_conflict_active",
        ),
        (
            lambda telemetry, policy, candidate: telemetry.update(
                recovery=_telemetry()["recovery"]
            ),
            "action_feasibility_offboard_calibration_already_observed",
        ),
    ],
)
def test_offboard_calibration_fails_closed(
    mutation,
    expected_reason: str,
) -> None:
    telemetry, policy, candidate = _calibration_case()
    mutation(telemetry, policy, candidate)

    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "blocked"
    assert expected_reason in result["blocking_reasons"]
    assert result["dispatch_authority_created"] is False


def test_gateway_calibration_revalidation_uses_latest_source_backed_telemetry() -> None:
    telemetry, _policy_snapshot, candidate = _calibration_case()
    artifacts = {
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/fixture-offboard-calibration.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "heartbeat_observed": True,
            "landed": False,
        },
    }

    result = gateway_server._runtime_recovery_calibration_revalidation(
        artifacts=artifacts,
        recovery_parameters=candidate["proposed_parameters"],
        now=datetime.now(timezone.utc),
    )

    assert result["validation_status"] == "valid"
    assert result["reasons"] == []
    assert (
        result["dispatch_action_feasibility"]["feasibility_status"]
        == "verified_feasible"
    )
    assert result["dispatch_authority_created"] is False


@pytest.mark.parametrize(
    ("mutation", "action", "expected_status", "expected_reason"),
    [
        (
            lambda value: value["wind"].update(speed_mps=11.0, gust_mps=12.0),
            "reroute",
            "blocked",
            "action_feasibility_wind_control_margin_not_positive",
        ),
        (
            lambda value: value["battery"].update(remaining_percent=20.1),
            "adjust_altitude",
            "blocked",
            "action_feasibility_projected_battery_reserve_negative",
        ),
        (
            lambda value: value["terrain"].update(terrain_clearance_m=20.0),
            "return_to_launch",
            "blocked",
            "action_feasibility_terrain_clearance_below_policy",
        ),
        (
            lambda value: value["obstacle"]["conflict_assessment"][
                "nearest_obstacle"
            ].update(obstacle_name="changed_obstacle"),
            "avoid_obstacle",
            "blocked",
            "action_feasibility_obstacle_source_mismatch",
        ),
        (
            lambda value: value["obstacle"]["conflict_assessment"][
                "nearest_obstacle"
            ].update(time_to_conflict_s=4.0),
            "avoid_obstacle",
            "blocked",
            "action_feasibility_maneuver_not_complete_before_conflict",
        ),
        (
            lambda value: value["landing_zone"].update(safe=False),
            "land",
            "blocked",
            "action_feasibility_landing_zone_not_safe",
        ),
    ],
)
def test_known_multi_hazard_constraints_block_actions(
    mutation,
    action: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    telemetry = _telemetry()
    mutation(telemetry)
    candidate = next(
        item
        for item in _candidates()
        if item["selected_bounded_action"] == action
    )

    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == expected_status
    assert expected_reason in result["blocking_reasons"]


@pytest.mark.parametrize("action", ["adjust_altitude", "reroute"])
def test_descent_below_terrain_clearance_is_blocked(action: str) -> None:
    telemetry = _telemetry()
    telemetry["terrain"].update(terrain_clearance_m=40.0)
    candidate = next(
        deepcopy(item)
        for item in _candidates()
        if item["selected_bounded_action"] == action
    )
    candidate["proposed_parameters"].update(target_altitude_m=10.0)
    if action == "reroute":
        candidate["proposed_parameters"].update(target_x_m=20.0, target_y_m=10.0)

    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == "blocked"
    assert result["projected_terrain_clearance_margin_m"] == -10.0
    assert "action_feasibility_terrain_clearance_below_policy" in result[
        "blocking_reasons"
    ]


def test_configured_but_unverified_payload_fails_closed_and_payload_changes_cost() -> None:
    telemetry = _telemetry()
    telemetry["payload"].update(observation_status="configured_unverified")
    unverified = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )
    assert unverified["feasibility_status"] == "unverified"
    assert "action_feasibility_payload_mass_unverified" in unverified["unverified_reasons"]

    light = _telemetry()
    light["payload"].update(mass_kg=0.1, observation_status="configured_applied")
    heavy = _telemetry()
    heavy["payload"].update(mass_kg=2.0, observation_status="configured_applied")
    light_result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1], hazard_state=_state(light), recovery_policy=_policy()
    )
    heavy_result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1], hazard_state=_state(heavy), recovery_policy=_policy()
    )
    assert (
        heavy_result["projected_battery_after_action_percent"]
        < light_result["projected_battery_after_action_percent"]
    )


@pytest.mark.parametrize(
    ("applied_mass_kg", "observation_status", "expected_reason"),
    [
        (
            None,
            "configured_unverified",
            "action_feasibility_payload_applied_mass_missing",
        ),
        (
            0.05,
            "configured_applied",
            "action_feasibility_payload_mass_mismatch",
        ),
    ],
)
def test_requested_payload_without_matching_applied_mass_fails_closed(
    applied_mass_kg: float | None,
    observation_status: str,
    expected_reason: str,
) -> None:
    telemetry = _telemetry()
    telemetry["payload"].update(
        requested_mass_kg=1.5,
        mass_kg=applied_mass_kg,
        observation_status=observation_status,
    )
    hazard_state = _state(telemetry)

    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=hazard_state,
        recovery_policy=_policy(),
    )

    assert (
        hazard_state["observed_facts"]["payload_requested_mass_kg"]["value"]
        == 1.5
    )
    assert result["feasibility_status"] == "unverified"
    assert expected_reason in result["unverified_reasons"]
    assert (
        result["battery_action_energy_model"]["payload_requested_mass_kg"]
        == 1.5
    )


def test_requested_payload_with_matching_applied_mass_is_costed() -> None:
    telemetry = _telemetry()
    telemetry["payload"].update(
        requested_mass_kg=1.5,
        mass_kg=1.5,
        observation_status="configured_applied",
    )

    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == "verified_feasible"
    assert (
        result["battery_action_energy_model"]["payload_requested_mass_kg"]
        == 1.5
    )
    assert result["battery_action_energy_model"]["payload_mass_kg"] == 1.5


@pytest.mark.parametrize(
    ("mutations", "expected_reasons"),
    [
        (
            (
                lambda value: value["battery"].update(
                    remaining_percent=20.1
                ),
                lambda value: value["wind"].update(
                    speed_mps=11.0,
                    gust_mps=12.0,
                ),
            ),
            {
                "action_feasibility_wind_control_margin_not_positive",
                "action_feasibility_projected_battery_reserve_negative",
            },
        ),
        (
            (
                lambda value: value["terrain"].update(
                    terrain_clearance_m=20.0
                ),
                lambda value: value["temperature"].update(
                    motor_thrust_factor=0.5
                ),
            ),
            {
                "action_feasibility_terrain_clearance_below_policy",
                "action_feasibility_motor_thrust_derating_exceeded",
            },
        ),
        (
            (
                lambda value: value["obstacle"]["conflict_assessment"][
                    "nearest_obstacle"
                ].update(time_to_conflict_s=4.0),
                lambda value: value["wind"].update(
                    speed_mps=7.0,
                    gust_mps=8.0,
                ),
            ),
            {
                "action_feasibility_maneuver_not_complete_before_conflict",
            },
        ),
    ],
)
def test_compound_constraints_are_evaluated_together(
    mutations,
    expected_reasons: set[str],
) -> None:
    telemetry = _telemetry()
    for mutation in mutations:
        mutation(telemetry)

    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[0],
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == "blocked"
    assert expected_reasons <= set(result["blocking_reasons"])


@pytest.mark.parametrize(
    ("mutate_telemetry", "mutate_policy", "expected_reason"),
    [
        (
            lambda value: value["telemetry"].update(stale=True),
            lambda value: None,
            "hazard_state_telemetry_stale",
        ),
        (
            lambda value: value.pop("sample_index"),
            lambda value: None,
            "hazard_state_telemetry_cursor_incomplete",
        ),
        (
            lambda value: value["temperature"].pop("motor_thrust_factor"),
            lambda value: value["temperature_derating_model"].pop(
                "motor_thrust_factor"
            ),
            "temperature_motor_thrust_factor_missing",
        ),
        (
            lambda value: value["temperature"].update(
                model={
                    "model_id": "unknown_temperature_model",
                    "model_version": "0",
                    "source_refs": ["fixture.unknown_model"],
                    "uncertainty_percent": 1.0,
                    "battery_capacity_factor": 1.0,
                    "motor_thrust_factor": 1.0,
                }
            ),
            lambda value: None,
            "temperature_model_policy_mismatch",
        ),
        (
            lambda value: None,
            lambda value: value["battery_action_energy_model"].pop("model_id"),
            "battery_action_energy_model_id_missing",
        ),
    ],
)
def test_missing_or_unknown_evidence_is_unverified(
    mutate_telemetry,
    mutate_policy,
    expected_reason: str,
) -> None:
    telemetry = _telemetry()
    policy = _policy()
    mutate_telemetry(telemetry)
    mutate_policy(policy)

    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(telemetry, policy),
        recovery_policy=policy,
    )

    assert result["feasibility_status"] == "unverified"
    assert expected_reason in result["unverified_reasons"]


def test_cursor_regression_and_policy_drift_fail_closed() -> None:
    state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=_telemetry(),
        recovery_policy=_policy(),
        prior_telemetry_cursor={
            "cursor_status": "complete",
            "sample_index": 201,
            "elapsed_seconds": 49.2,
        },
    )
    assert state["hazard_state_status"] == "unverified"
    assert "hazard_state_telemetry_cursor_regression" in state["freshness"][
        "blocking_reasons"
    ]

    current_policy = deepcopy(_policy())
    current_policy["max_recovery_horizontal_speed_mps"] = 8.0
    result = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[1],
        hazard_state=_state(),
        recovery_policy=current_policy,
    )
    assert result["feasibility_status"] == "unverified"
    assert "action_feasibility_policy_drift" in result["unverified_reasons"]


def test_observed_safety_hold_suspends_original_route_ttc_deadline() -> None:
    telemetry = _telemetry()
    telemetry["obstacle"]["conflict_assessment"]["nearest_obstacle"][
        "time_to_conflict_s"
    ] = None
    without_hold = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[0],
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )
    telemetry["recovery"] = {
        "request_observed": True,
        "action": "safety_hold",
        "command_ack_observed": True,
        "assist_status": "safety_hold_observed",
        "resume_status": "held_awaiting_operator_recovery_approval",
        "performance_observation": _telemetry()["recovery"][
            "performance_observation"
        ],
        "source_refs": ["fixture.active_runner_hold_observation"],
    }
    with_hold = verify_runtime_recovery_action_feasibility(
        candidate=_candidates()[0],
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert without_hold["feasibility_status"] == "unverified"
    assert (
        "action_feasibility_time_to_conflict_missing"
        in without_hold["unverified_reasons"]
    )
    assert with_hold["feasibility_status"] == "verified_feasible"
    assert (
        with_hold["time_to_conflict_constraint_status"]
        == "suspended_by_observed_safety_hold"
    )


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_reason"),
    [
        (
            "collision",
            "blocked",
            "action_feasibility_obstacle_clearance_not_met",
        ),
        (
            "boundary",
            "blocked",
            "action_feasibility_obstacle_clearance_not_met",
        ),
        (
            "frame_mismatch",
            "unverified",
            "action_feasibility_recovery_path_frame_mismatch",
        ),
        (
            "geometry_missing",
            "unverified",
            "action_feasibility_obstacle_geometry_unverified",
        ),
        (
            "no_motion",
            "blocked",
            "action_feasibility_recovery_path_not_beyond_obstacle",
        ),
        (
            "endpoint_mismatch",
            "blocked",
            "action_feasibility_recovery_path_endpoint_mismatch",
        ),
    ],
)
def test_obstacle_path_clearance_fails_closed(
    case: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    telemetry = _telemetry()
    candidate = deepcopy(_candidates()[0])
    if case == "collision":
        candidate["proposed_parameters"].update(
            target_x_m=80.0,
            target_y_m=0.0,
            target_altitude_m=0.0,
        )
        candidate["recovery_path"]["waypoints"] = [
            {"x_m": 80.0, "y_m": 0.0, "z_m": 0.0}
        ]
    elif case == "boundary":
        telemetry["position"]["altitude_above_home_m"] = 22.0
        candidate["proposed_parameters"].update(
            target_x_m=80.0,
            target_y_m=0.0,
            target_altitude_m=22.0,
        )
        candidate["recovery_path"]["waypoints"] = [
            {"x_m": 80.0, "y_m": 0.0, "z_m": 22.0}
        ]
    elif case == "frame_mismatch":
        candidate["recovery_path"]["frame_id"] = "map"
    elif case == "geometry_missing":
        telemetry["obstacle"]["obstacle_manifest"]["obstacles"][0].pop(
            "bounds_local_xyz_m"
        )
    elif case == "no_motion":
        candidate["proposed_parameters"].update(
            target_x_m=0.0,
            target_y_m=0.0,
            target_altitude_m=30.0,
        )
        candidate["recovery_path"]["waypoints"] = [
            {"x_m": 0.0, "y_m": 0.0, "z_m": 30.0}
        ]
    elif case == "endpoint_mismatch":
        candidate["recovery_path"]["waypoints"] = [
            {"x_m": 80.0, "y_m": 200.0, "z_m": 36.0}
        ]

    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=_state(telemetry),
        recovery_policy=_policy(),
    )

    assert result["feasibility_status"] == expected_status
    reasons = (
        result["blocking_reasons"]
        if expected_status == "blocked"
        else result["unverified_reasons"]
    )
    assert expected_reason in reasons
    assert result["dispatch_authority_created"] is False


@pytest.mark.parametrize(
    ("selected_action", "telemetry_mutation", "expected_reason"),
    [
        (
            "return_to_launch",
            lambda value: value["wind"].update(speed_mps=11.0, gust_mps=12.0),
            "action_feasibility_wind_control_margin_not_positive",
        ),
        (
            "land",
            lambda value: value["landing_zone"].update(safe=False),
            "action_feasibility_landing_zone_not_safe",
        ),
    ],
)
def test_llm_cannot_select_blocked_or_unverified_action(
    selected_action: str,
    telemetry_mutation,
    expected_reason: str,
) -> None:
    telemetry = _telemetry()
    telemetry_mutation(telemetry)

    assessment = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": selected_action,
            "proposed_parameters": {},
            "trigger_level": "advisory",
            "trigger_reasons": ["fixture_multi_hazard_risk"],
            "requires_human_approval": True,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=_policy()
        | {
            "action_feasibility_required": True,
            "preauthorized_actions": [selected_action],
        },
    )

    assert assessment["assessment_status"] == "blocked"
    assert assessment["selected_bounded_action"] == "operator_review"
    assert expected_reason in assessment["blocking_reasons"]
    assert (
        assessment["action_feasibility"]["feasibility_status"] == "blocked"
    )
    assert assessment["dispatch_authority_created"] is False
    assert assessment["physical_execution_invoked"] is False


def _v3_proposal(
    *,
    telemetry: dict | None = None,
    policy: dict | None = None,
    action: str = "avoid_obstacle",
) -> tuple[dict, dict]:
    telemetry = telemetry or _telemetry()
    policy = policy or gateway_server._operator_recovery_proposal_policy()
    candidate = next(
        item
        for item in _candidates()
        if item["selected_bounded_action"] == action
    )
    agent_output = {
        "strategy": (
            "local_avoidance"
            if action == "avoid_obstacle"
            else "rtl_or_land"
            if action in {"return_to_launch", "land"}
            else action
        ),
        "selected_bounded_action": action,
        "proposed_parameters": candidate["proposed_parameters"],
        "intent_constraints": {"maximum_duration_s": 75.0},
        "requires_human_approval": True,
    }
    intent = build_runtime_recovery_intent(agent_output=agent_output)
    compilation = compile_runtime_recovery_intent(
        intent=intent,
        candidate=candidate,
        recovery_policy=policy,
    )
    reachability = verify_runtime_recovery_reachability(
        compilation=compilation,
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
    )
    hazard_state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
    )
    feasibility = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=hazard_state,
        recovery_policy=policy,
    )
    now = datetime.now(timezone.utc)
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v3",
        "proposal_id": "fixture_v3_action_feasibility_proposal",
        "proposal_status": "awaiting_operator_approval",
        "source_obstacle_name": (
            "fixture_obstacle" if action == "avoid_obstacle" else None
        ),
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "recovery_intent": intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "hazard_state": hazard_state,
        "hazard_state_id": hazard_state["hazard_state_id"],
        "hazard_state_sha256": hazard_state["hazard_state_sha256"],
        "action_feasibility": feasibility,
        "action_feasibility_id": feasibility["action_feasibility_id"],
        "action_feasibility_sha256": feasibility[
            "action_feasibility_sha256"
        ],
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": candidate,
                "hazard_state": hazard_state,
                "action_feasibility": feasibility,
            }
        },
        "dispatch_authority_created": False,
    }
    return proposal, policy


def _v4_proposal(
    *,
    telemetry: dict | None = None,
    policy: dict | None = None,
    assurance_observed: bool = True,
) -> tuple[dict, dict]:
    source_telemetry = telemetry or _telemetry()
    proposal, resolved_policy = _v3_proposal(
        telemetry=source_telemetry,
        policy=policy,
    )
    proposal["schema_version"] = "missionos_runtime_recovery_proposal_evidence.v4"
    graph = {
        "schema_version": "missionos_adk_v2_mission_incident_graph_result.v1",
        "workflow_name": "missionos_mission_incident_v2",
        "graph_runtime_status": "proposal_guardrail_passed",
        "decision_status": "awaiting_operator_approval",
        "alignment_status": "accepted",
        "graph_node_sequence": [
            "observe_mission_incident",
            "invoke_runtime_recovery_agent",
            "materialize_source_action_feasibility",
            "invoke_mission_assurance_agent",
            "resolve_mission_incident_checkpoint",
            "finalize_mission_incident",
        ],
        "mission_assurance_agent_invoked": assurance_observed,
        "recovery_agent_invoked": True,
        "recovery_agent_invoked_before_mission_assurance": True,
        "recovery_judgment_available_before_mission_assurance": True,
        "recovery_proposed_action": "avoid_obstacle",
        "mission_situation": {
            "observations": {
                "runtime_telemetry": {
                    "sample_index": source_telemetry["sample_index"],
                    "elapsed_seconds": source_telemetry["elapsed_seconds"],
                }
            }
        },
        "operator_approval_required": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "executor_invoked": False,
        "command_ack_observed": False,
        "effect_observed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "delivery_completion_claimed": False,
    }
    graph_sha256 = hashlib.sha256(
        json.dumps(
            graph,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    proposal["missionos_mission_incident_graph"] = {
        **graph,
        "mission_incident_graph_sha256": graph_sha256,
        "mission_incident_graph_id": (
            f"mission_incident_graph_{graph_sha256[:12]}"
        ),
    }
    return proposal, resolved_policy


def _operator_chat_agent_result(
    *, telemetry: dict | None = None
) -> dict:
    proposal, _ = _v3_proposal(telemetry=telemetry)
    stored = proposal["runtime_recovery_agent_result"]
    assessment = dict(stored["assessment"])
    candidate = dict(assessment["recovery_planner_tool_candidate"])
    assessment.update(
        {
            "assessment_status": "proposal_guardrail_passed",
            "selected_bounded_action": candidate["selected_bounded_action"],
            "proposed_parameters": dict(candidate["proposed_parameters"]),
            "recovery_intent": proposal["recovery_intent"],
            "intent_compilation": proposal["intent_compilation"],
            "reachability_verification": proposal[
                "reachability_verification"
            ],
        }
    )
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": assessment,
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "provider": "google_adk_litellm_deepseek",
                "model_id": "fixture-recovery-model",
                "invocation_kind": "adk_llm",
                "prompt_sha256": "a" * 64,
                "response_sha256": "b" * 64,
            }
        ],
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _operator_chat_incident_graph(
    *,
    agent_result: dict,
    decision_status: str = "awaiting_operator_approval",
) -> dict:
    accepted = decision_status == "awaiting_operator_approval"
    graph = {
        "schema_version": "missionos_adk_v2_mission_incident_graph_result.v1",
        "workflow_name": "missionos_mission_incident_v2",
        "graph_runtime_status": "proposal_guardrail_passed",
        "decision_status": decision_status,
        "alignment_status": (
            "accepted" if accepted else "suppressed_by_mission_assurance"
        ),
        "blocking_reasons": [],
        "graph_node_sequence": [
            "observe_mission_incident",
            "invoke_runtime_recovery_agent",
            "materialize_source_action_feasibility",
            "invoke_mission_assurance_agent",
            "resolve_mission_incident_checkpoint",
            "finalize_mission_incident",
        ],
        "recovery_result": agent_result,
        "recovery_proposed_action": "avoid_obstacle",
        "mission_situation": {"situation_id": "fixture-situation"},
        "mission_assurance_proposal": {
            "judgment_status": "proposal_guardrail_passed",
            "proposed_response_kind": "replan" if accepted else "hold",
            "model_inference_invoked": True,
        },
        "mission_assurance_response_kind": "replan" if accepted else "hold",
        "recovery_agent_invoked": True,
        "recovery_judgment_available_before_mission_assurance": True,
        "mission_assurance_agent_invoked": True,
        "operator_approval_required": accepted,
        "dispatch_prevented_by_mission_assurance": not accepted,
        "approval_created": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    digest = hashlib.sha256(
        json.dumps(
            graph,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **graph,
        "mission_incident_graph_sha256": digest,
        "mission_incident_graph_id": f"mission_incident_graph_{digest[:12]}",
    }


def _dispatch_artifacts(proposal: dict, telemetry: dict) -> dict:
    position = telemetry["position"]
    wind = telemetry["wind"]
    return {
        "missionos_runtime_recovery_last_proposal": proposal,
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": position["local_x_m"],
            "local_y_m": position["local_y_m"],
            "altitude_above_home_m": position["altitude_above_home_m"],
            "wind_speed_mps": wind["speed_mps"],
            "heartbeat_observed": True,
            "landed": False,
        },
    }


@pytest.mark.parametrize(
    ("decision_status", "expected_durable"),
    [
        ("awaiting_operator_approval", True),
        ("no_dispatch", False),
    ],
)
def test_operator_chat_recovery_uses_graph_before_durable_proposal(
    monkeypatch,
    tmp_path,
    decision_status: str,
    expected_durable: bool,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    telemetry = _telemetry()
    agent_result = _operator_chat_agent_result(telemetry=telemetry)
    graph = _operator_chat_incident_graph(
        agent_result=agent_result,
        decision_status=decision_status,
    )
    graph_calls: list[dict] = []

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_runtime_recovery_agent",
        lambda **_kwargs: agent_result,
    )

    def run_graph(**kwargs):
        graph_calls.append(kwargs)
        assert kwargs["recovery_runner"]() == agent_result
        return graph

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_mission_incident_graph",
        run_graph,
    )
    gateway = create_missionos_gateway()
    task_id = f"task_operator_chat_graph_{decision_status}"
    initial_artifacts = {
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        }
    }
    if not expected_durable:
        initial_artifacts["missionos_runtime_recovery_last_proposal"] = {
            "schema_version": (
                "missionos_runtime_recovery_proposal_evidence.v4"
            ),
            "proposal_id": "prior_pending_operator_chat_proposal",
            "proposal_status": "awaiting_operator_approval",
            "dispatch_authority_created": False,
        }
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Operator chat graph fixture",
        status="running",
        artifacts=initial_artifacts,
    )

    response = TestClient(gateway.app).post(
        "/missionos/runtime-recovery-agent/propose-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "Propose a bounded obstacle recovery",
            "requested_action": "avoid_obstacle",
        },
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(graph_calls) == 1
    assert body["missionos_mission_incident_graph"][
        "mission_incident_graph_id"
    ] == graph["mission_incident_graph_id"]
    assert body["durable_proposal_created"] is expected_durable
    assert body["dispatch_authority_created"] is False
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    artifacts = stored["artifacts"]
    assert artifacts["missionos_mission_incident_graph"][
        "mission_incident_graph_id"
    ] == graph["mission_incident_graph_id"]
    if expected_durable:
        proposal = artifacts["missionos_runtime_recovery_last_proposal"]
        assert proposal["schema_version"] == (
            "missionos_runtime_recovery_proposal_evidence.v4"
        )
        assert proposal["missionos_mission_incident_graph"][
            "mission_incident_graph_id"
        ] == graph["mission_incident_graph_id"]
        assert proposal["dispatch_authority_created"] is False
    else:
        invalidated = artifacts["missionos_runtime_recovery_last_proposal"]
        assert invalidated["proposal_status"] == "invalidated"
        assert invalidated["invalidation_reasons"] == [
            "new_mission_incident_graph_did_not_create_approval_candidate"
        ]
        assert graph["mission_assurance_response_kind"] == "hold"
        assert graph["dispatch_prevented_by_mission_assurance"] is True


def test_operator_chat_recovery_graph_failure_creates_no_proposal(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    telemetry = _telemetry()
    agent_result = _operator_chat_agent_result(telemetry=telemetry)
    monkeypatch.setattr(
        gateway_server,
        "run_missionos_runtime_recovery_agent",
        lambda **_kwargs: agent_result,
    )

    def fail_graph(**_kwargs):
        raise RuntimeError("fixture graph unavailable")

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_mission_incident_graph",
        fail_graph,
    )
    gateway = create_missionos_gateway()
    task_id = "task_operator_chat_graph_failure"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Operator chat graph failure fixture",
        status="running",
        artifacts={
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": telemetry,
            },
            "missionos_runtime_recovery_last_proposal": {
                "schema_version": (
                    "missionos_runtime_recovery_proposal_evidence.v4"
                ),
                "proposal_id": "prior_pending_graph_failure_proposal",
                "proposal_status": "awaiting_operator_approval",
                "dispatch_authority_created": False,
            },
        },
    )

    response = TestClient(gateway.app).post(
        "/missionos/runtime-recovery-agent/propose-for-task",
        json={
            "task_id": task_id,
            "operator_instruction": "Propose a bounded obstacle recovery",
            "requested_action": "avoid_obstacle",
        },
    )

    assert response.status_code == 409, response.json()
    assert response.json()["durable_proposal_created"] is False
    assert "fixture graph unavailable" in response.json()[
        "mission_incident_graph_error"
    ]
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    artifacts = stored["artifacts"]
    invalidated = artifacts["missionos_runtime_recovery_last_proposal"]
    assert invalidated["proposal_status"] == "invalidated"
    assert invalidated["invalidation_reasons"] == [
        "mission_incident_graph_failed_closed"
    ]
    assert artifacts["missionos_mission_incident_graph_failure"][
        "failure_status"
    ] == "operator_escalation"


def test_valid_v3_proposal_requires_graph_as_its_only_missing_condition() -> None:
    telemetry = _telemetry()
    proposal, _ = _v3_proposal(telemetry=telemetry)
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": proposal["intent_compilation"][
                "compiled_parameters"
            ]
        },
    )

    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, telemetry),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=datetime.now(timezone.utc),
    )

    assert result["reasons"] == [
        "mission_incident_graph_required_for_recovery_dispatch"
    ]
    assert result["validation_status"] == "blocked"


@pytest.mark.parametrize(
    ("recovery_action", "runner_path"),
    [
        ("avoid_obstacle", "/tmp/missionos_graphless_avoidance.json"),
        ("return_to_launch", ""),
    ],
)
def test_graphless_agent_proposal_reaches_neither_dispatch_side_effect(
    monkeypatch,
    tmp_path,
    recovery_action: str,
    runner_path: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    telemetry = _telemetry()
    proposal, _ = _v3_proposal(
        telemetry=telemetry,
        action=recovery_action,
    )
    artifacts = _dispatch_artifacts(proposal, telemetry)
    artifacts["missionos_auto_mission_gui_dispatch_running_receipt"] = {
        "operator_recovery_request_container_path": runner_path,
    }
    gateway = create_missionos_gateway()
    task_id = f"task_graphless_{recovery_action}"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Graphless proposal side-effect guard",
        status="running",
        artifacts=artifacts,
    )
    side_effects: list[str] = []

    def _unexpected_runner_write(**_kwargs):
        side_effects.append("active_runner_write")
        raise AssertionError("graphless proposal reached active runner write")

    def _unexpected_emergency_dispatch(**_kwargs):
        side_effects.append("emergency_mavlink_dispatch")
        raise AssertionError("graphless proposal reached emergency dispatch")

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        _unexpected_runner_write,
    )
    monkeypatch.setattr(
        gateway_server,
        "run_px4_gazebo_emergency_command_dispatch",
        _unexpected_emergency_dispatch,
    )
    requested = {
        key: value
        for key, value in proposal["intent_compilation"][
            "compiled_parameters"
        ].items()
        if key != "source_obstacle_name"
    }

    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": recovery_action,
            "recovery_parameters": requested,
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409, response.json()
    summary = response.json()["summary"]
    reasons = summary["proposal_revalidation"]["reasons"]
    assert "mission_incident_graph_required_for_recovery_dispatch" in reasons
    if recovery_action == "avoid_obstacle":
        assert reasons == [
            "mission_incident_graph_required_for_recovery_dispatch"
        ]
    assert summary["dispatch_authority_created"] is False
    assert summary["active_runner_request_queued"] is False
    continuation = response.json()[
        "missionos_mission_incident_continuation_graph"
    ]
    assert continuation["human_approval_observed"] is False
    assert continuation["dispatch_authority_created"] is False
    assert continuation["dispatch_request_sent"] is False
    assert continuation["executor_invoked"] is False
    assert continuation["verifier_status"] == "not_started"
    assert side_effects == []


def test_preflight_calibration_uses_rules_and_human_approval_without_incident_graph(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    telemetry, _policy_snapshot, candidate = _calibration_case()
    artifacts = {
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/fixture-offboard-calibration.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "heartbeat_observed": True,
            "landed": False,
        },
    }
    gateway = create_missionos_gateway()
    task_id = "task_preflight_calibration_without_incident"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Preflight calibration boundary",
        status="running",
        artifacts=artifacts,
    )
    queued: list[dict] = []

    def _queue(**kwargs):
        queued.append(kwargs["request_payload"])
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo",
            "container_path": kwargs["container_path"],
            "bytes_written": 123,
        }

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        _queue,
    )

    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "calibrate_offboard",
            "recovery_parameters": candidate["proposed_parameters"],
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    summary = payload["summary"]
    assert summary["proposal_revalidation"]["validation_status"] == "valid"
    assert summary["dispatch_status"] == "queued_for_active_runner"
    assert summary["dispatch_authority_created"] is True
    assert payload["missionos_mission_incident_continuation_graph"] == {}
    boundary = payload["missionos_preflight_calibration_dispatch_boundary"]
    assert boundary["mission_incident_graph_required"] is False
    assert boundary["recovery_agent_invoked"] is False
    assert boundary["mission_assurance_agent_invoked"] is False
    assert boundary["human_approval_observed"] is True
    assert boundary["action_revalidation"]["validation_status"] == "valid"
    assert boundary["executor_invoked"] is True
    assert boundary["dispatch_request_sent"] is True
    assert boundary["effect_observed"] is False
    assert len(queued) == 1
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    stored_artifacts = stored["artifacts"]
    assert "missionos_mission_incident_continuation_graph" not in stored_artifacts
    assert stored_artifacts[
        "missionos_preflight_calibration_dispatch_boundary"
    ]["dispatch_request_sent"] is True


@pytest.mark.parametrize("review_binding", ["legacy_client", "exact", "changed_id", "changed_hash"])
def test_valid_v4_graph_proposal_queues_only_after_operator_approval(
    monkeypatch,
    tmp_path,
    review_binding,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    telemetry = _telemetry()
    proposal, _ = _v4_proposal(telemetry=telemetry)
    gateway = create_missionos_gateway()
    task_id = "task_valid_v4_graph_dispatch"
    proposal["runtime_recovery_agent_result"]["assessment"].update({
        "intent_compilation": proposal["intent_compilation"],
        "reachability_verification": proposal["reachability_verification"],
        "selected_bounded_action": "avoid_obstacle",
    })
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Valid v4 graph dispatch",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_valid_v4_graph_dispatch.json"
                ),
            },
            **_dispatch_artifacts(proposal, telemetry),
        },
    )
    queued: list[dict] = []

    def _queue(**kwargs):
        request_payload = kwargs["request_payload"]
        queued.append(request_payload)
        gateway.task_store.update(
            task_id,
            replace_artifacts={
                "missionos_auto_mission_runtime_snapshot": {
                    "sample_index": telemetry["sample_index"] + 2,
                    "elapsed_seconds": telemetry["elapsed_seconds"] + 1.0,
                    "local_x_m": request_payload["recovery_parameters"][
                        "target_x_m"
                    ],
                    "local_y_m": request_payload["recovery_parameters"][
                        "target_y_m"
                    ],
                    "altitude_above_home_m": request_payload[
                        "recovery_parameters"
                    ]["target_altitude_m"],
                    "heartbeat_observed": True,
                    "landed": False,
                    "operator_recovery_request_observed": True,
                    "operator_recovery_action": "avoid_obstacle",
                    "operator_recovery_parameters": request_payload[
                        "recovery_parameters"
                    ],
                    "operator_recovery_command_ack_observed": True,
                    "operator_recovery_command_ack_result": 0,
                    "operator_recovery_assist_status": "target_reached",
                    "operator_recovery_target_reached": True,
                    "operator_recovery_resume_auto_status": (
                        "resumed_auto_mission"
                    ),
                }
            },
        )
        return {
            "request_status": "queued",
            "container_name": "missionos-px4-gazebo",
            "container_path": kwargs["container_path"],
            "bytes_written": 123,
        }

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        _queue,
    )
    requested = {
        key: value
        for key, value in proposal["intent_compilation"][
            "compiled_parameters"
        ].items()
        if key != "source_obstacle_name"
    }

    from missionos_cli import cli
    pending = cli._pending_recovery_approval_from_task({"task": gateway.task_store.get(task_id)})
    assert pending is not None
    assert pending["checkpoint_id"] == proposal["proposal_id"]
    assert pending["checkpoint_hash"] == proposal["missionos_mission_incident_graph"]["mission_incident_graph_sha256"]
    assert pending["mission_assurance"]["decision_status"] == "awaiting_operator_approval"
    binding = {} if review_binding == "legacy_client" else {
        "expected_recovery_checkpoint_id": pending["checkpoint_id"],
        "expected_recovery_checkpoint_hash": pending["checkpoint_hash"],
    }
    if review_binding == "changed_id":
        binding["expected_recovery_checkpoint_id"] = "reviewed_old_proposal"
    elif review_binding == "changed_hash":
        binding["expected_recovery_checkpoint_hash"] = "reviewed_old_graph_hash"
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": requested,
            "explicit_recovery_dispatch_approval": True,
            "wait_for_effect_observation": True,
            **binding,
        },
    )

    if review_binding.startswith("changed_"):
        assert queued == []
        summary = response.json()["summary"]
        assert summary["dispatch_authority_created"] is False
        assert summary["proposal_revalidation"]["validation_status"] == "blocked"
        field = "id" if review_binding == "changed_id" else "hash"
        assert summary["proposal_revalidation"]["reasons"] == [
            f"reviewed_px4_recovery_checkpoint_{field}_mismatch"
        ]
        return
    assert response.status_code == 200, response.json()
    summary = response.json()["summary"]
    assert summary["proposal_revalidation"]["validation_status"] == "valid"
    assert summary["dispatch_status"] == "queued_for_active_runner"
    assert summary["dispatch_authority_created"] is True
    assert summary["command_ack_observed"] is True
    assert summary["command_ack_result_name"] == 0
    continuation = response.json()[
        "missionos_mission_incident_continuation_graph"
    ]
    assert continuation["frozen_mission_incident_graph_id"] == proposal[
        "missionos_mission_incident_graph"
    ]["mission_incident_graph_id"]
    assert continuation["recovery_agent_rerun"] is False
    assert continuation["mission_assurance_agent_rerun"] is False
    assert continuation["human_approval_observed"] is True
    assert continuation["action_revalidation"]["validation_status"] == "valid"
    assert continuation["executor_invoked"] is True
    assert continuation["dispatch_request_sent"] is True
    assert continuation["command_ack_observed"] is True
    assert continuation["effect_observed"] is True
    assert continuation["verifier_status"] == "verified"
    assert continuation["next_mission_situation_created"] is True
    assert continuation["next_mission_situation"]["observation_status"] == (
        "fresh_post_dispatch_observation"
    )
    assert len(queued) == 1
    assert queued[0]["operator_approved"] is True
    stored = gateway.task_store.get(task_id)
    assert stored is not None
    bound = stored["artifacts"]["missionos_runtime_recovery_last_proposal"]
    assert bound["proposal_status"] == "dispatch_authority_bound"
    assert bound["dispatch_authority_created"] is True
    assert stored["artifacts"][
        "missionos_mission_incident_continuation_graph"
    ]["continuation_graph_id"] == continuation["continuation_graph_id"]


def test_v4_graph_reports_missing_assurance_observation_separately() -> None:
    telemetry = _telemetry()
    proposal, _ = _v4_proposal(
        telemetry=telemetry,
        assurance_observed=False,
    )
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": proposal["intent_compilation"][
                "compiled_parameters"
            ]
        },
    )

    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, telemetry),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=datetime.now(timezone.utc),
    )

    assert result["reasons"] == ["mission_assurance_agent_not_observed"]
    assert result["validation_status"] == "blocked"


def test_v4_graph_hash_detects_post_judgment_mutation() -> None:
    telemetry = _telemetry()
    proposal, _ = _v4_proposal(telemetry=telemetry)
    proposal["missionos_mission_incident_graph"][
        "mission_assurance_response_kind"
    ] = "hold"
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": proposal["intent_compilation"][
                "compiled_parameters"
            ]
        },
    )

    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, telemetry),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=datetime.now(timezone.utc),
    )

    assert result["validation_status"] == "blocked"
    assert "mission_incident_graph_hash_mismatch" in result["reasons"]


def test_dispatch_time_revalidates_all_hazards_from_latest_telemetry() -> None:
    telemetry = _telemetry()
    proposal, _ = _v4_proposal(telemetry=telemetry)
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": proposal["intent_compilation"][
                "compiled_parameters"
            ]
        },
    )

    valid = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, telemetry),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=datetime.now(timezone.utc),
    )
    changed = deepcopy(telemetry)
    changed["sample_index"] = 201
    changed["elapsed_seconds"] = 49.2
    changed["temperature"].pop("motor_thrust_factor")
    current_model = gateway_server._operator_recovery_proposal_policy()[
        "temperature_derating_model"
    ]
    changed["temperature"]["model"] = {
        key: value
        for key, value in current_model.items()
        if key != "motor_thrust_factor"
    }
    changed_proposal, _ = _v4_proposal(telemetry=telemetry)

    unverified = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(changed_proposal, changed),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=datetime.now(timezone.utc),
    )

    assert valid["validation_status"] == "valid", valid["reasons"]
    assert (
        valid["dispatch_action_feasibility"]["feasibility_status"]
        == "verified_feasible"
    )
    assert unverified["validation_status"] == "blocked"
    assert "temperature_motor_thrust_factor_missing" in unverified["reasons"]
    assert unverified["dispatch_authority_created"] is False


def test_gateway_dispatch_does_not_queue_when_latest_battery_blocks_feasibility(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    gateway = create_missionos_gateway()
    proposal_telemetry = _telemetry()
    proposal, _ = _v4_proposal(telemetry=proposal_telemetry)
    latest_telemetry = deepcopy(proposal_telemetry)
    latest_telemetry["sample_index"] = 201
    latest_telemetry["elapsed_seconds"] = 49.2
    latest_telemetry["battery"]["remaining_percent"] = 20.1
    task_id = "task_fixture_v3_multi_hazard_dispatch"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title="Fixture v3 multi-hazard dispatch",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": (
                    "/tmp/missionos_fixture_v3_recovery.json"
                ),
            },
            **_dispatch_artifacts(proposal, latest_telemetry),
        },
    )
    queued: list[dict] = []

    def should_not_queue(**kwargs):
        queued.append(kwargs)
        raise AssertionError("blocked feasibility must not reach the runner")

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        should_not_queue,
    )
    requested = {
        key: value
        for key, value in proposal["intent_compilation"][
            "compiled_parameters"
        ].items()
        if key != "source_obstacle_name"
    }
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": requested,
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409, response.json()
    revalidation = response.json()["summary"]["proposal_revalidation"]
    assert revalidation["validation_status"] == "blocked"
    assert (
        "action_feasibility_projected_battery_reserve_negative"
        in revalidation["reasons"]
    )
    assert (
        revalidation["dispatch_action_feasibility"]["feasibility_status"]
        == "blocked"
    )
    assert revalidation["dispatch_authority_created"] is False
    assert queued == []


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        (
            "policy_drift",
            "runtime_recovery_active_policy_drift",
        ),
        (
            "pending_v2",
            "runtime_recovery_v2_proposal_invalidated_by_action_feasibility_policy",
        ),
    ],
)
def test_gateway_dispatch_rejects_policy_drift_and_pending_v2(
    monkeypatch,
    tmp_path,
    case: str,
    expected_reason: str,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    telemetry = _telemetry()
    proposal, _ = _v4_proposal(telemetry=telemetry)
    if case == "policy_drift":
        current_policy = gateway_server._operator_recovery_proposal_policy()
        current_policy["battery_return_threshold_percent"] = 30.0
        monkeypatch.setattr(
            gateway_server,
            "_operator_recovery_proposal_policy",
            lambda: deepcopy(current_policy),
        )
    else:
        proposal["schema_version"] = (
            "missionos_runtime_recovery_proposal_evidence.v2"
        )
        proposal.pop("hazard_state", None)
        proposal.pop("action_feasibility", None)

    gateway = create_missionos_gateway()
    task_id = f"task_fixture_{case}_dispatch"
    gateway.task_store.create(
        task_id=task_id,
        kind="mission_designer_sitl_execution",
        title=f"Fixture {case} dispatch",
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": (
                    f"/tmp/missionos_fixture_{case}.json"
                ),
            },
            **_dispatch_artifacts(proposal, telemetry),
        },
    )
    queued: list[dict] = []

    def should_not_queue(**kwargs):
        queued.append(kwargs)
        raise AssertionError("fail-closed dispatch must not reach the runner")

    monkeypatch.setattr(
        gateway_server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        should_not_queue,
    )
    requested = {
        key: value
        for key, value in proposal["intent_compilation"][
            "compiled_parameters"
        ].items()
        if key != "source_obstacle_name"
    }
    response = TestClient(gateway.app).post(
        "/px4-gazebo/mission-scenarios/recovery-dispatch",
        json={
            "task_id": task_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": requested,
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert response.status_code == 409, response.json()
    revalidation = response.json()["summary"]["proposal_revalidation"]
    assert revalidation["validation_status"] == "blocked"
    assert expected_reason in revalidation["reasons"]
    assert revalidation["dispatch_authority_created"] is False
    assert queued == []
def test_gateway_calibration_waits_for_requested_wind_activation() -> None:
    telemetry, _policy_snapshot, candidate = _calibration_case()
    artifacts = {
        "mission_designer_coordinate_pair_route": {"wind_speed_mps": 4.0},
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/fixture-offboard-calibration.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "wind_speed_mps": 0.0,
            "wind_mean_started": False,
            "heartbeat_observed": True,
            "landed": False,
        },
    }

    result = gateway_server._runtime_recovery_calibration_revalidation(
        artifacts=artifacts,
        recovery_parameters=candidate["proposed_parameters"],
        now=datetime.now(timezone.utc),
    )

    assert result["validation_status"] == "blocked"
    assert "runtime_recovery_calibration_requested_wind_not_started" in result[
        "reasons"
    ]
    assert "runtime_recovery_calibration_requested_wind_not_observed" in result[
        "reasons"
    ]


def test_gateway_calibration_rejects_wind_above_recovery_limit() -> None:
    telemetry, _policy_snapshot, candidate = _calibration_case()
    telemetry["wind"].update(speed_mps=9.0, gust_mps=9.0)
    artifacts = {
        "mission_designer_coordinate_pair_route": {"wind_speed_mps": 9.0},
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/fixture-offboard-calibration.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "wind_speed_mps": 9.0,
            "wind_mean_started": True,
            "heartbeat_observed": True,
            "landed": False,
        },
    }

    result = gateway_server._runtime_recovery_calibration_revalidation(
        artifacts=artifacts,
        recovery_parameters=candidate["proposed_parameters"],
        now=datetime.now(timezone.utc),
    )

    assert result["validation_status"] == "blocked"
    assert (
        result["dispatch_action_feasibility"]["feasibility_status"]
        == "verified_feasible"
    )
    assert "runtime_recovery_calibration_wind_above_policy_limit" in result["reasons"]
