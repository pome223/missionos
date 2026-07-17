from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import environmental_realism, scenario, world


FIXED_TIME = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _world_root(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "model"
    world_dir = root / "worlds"
    world_dir.mkdir(parents=True)
    (world_dir / "default.sdf").write_text(
        '<sdf version="1.9"><world name="default"><scene></scene>' + body + "</world></sdf>"
    )
    return root


def _assert_no_completion_or_physical_claims(
    *artifacts: dict[str, Any],
) -> None:
    for artifact in artifacts:
        if "delivery_completion_claimed" in artifact:
            assert artifact["delivery_completion_claimed"] is False
        if "physical_execution_invoked" in artifact:
            assert artifact["physical_execution_invoked"] is False


def test_thermal_not_requested_invokes_no_runtime_callbacks() -> None:
    calls: list[str] = []
    profile = scenario.build_thermal_weather_requested_profile(
        temperature_c=None,
        pressure_hpa=None,
        thermal_battery_drain_factor=None,
        thermal_motor_derate_factor=None,
    )

    result = environmental_realism.run_thermal_weather_realism(
        profile=profile,
        param_show=lambda _name: calls.append("show") or {},
        param_set=lambda _name, _value: calls.append("set") or {},
        reset_battery_status_cache=lambda: calls.append("reset"),
        battery_status_sample=lambda: calls.append("battery") or {},
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )

    assert calls == []
    application = result["thermal_weather_simulator_condition_application"]
    evidence = result["observed_thermal_weather_evidence"]
    assert application["application_status"] == "not_requested"
    assert evidence["observation_status"] == "not_requested"
    _assert_no_completion_or_physical_claims(application, evidence)


def test_thermal_runtime_requires_param_readback_before_observation() -> None:
    values = {
        "SIM_BAT_MIN_PCT": 0.0,
        "SIM_BAT_DRAIN": 1800.0,
        "MPC_THR_MAX": 1.0,
    }
    reset_count = 0

    def param_show(name: str) -> dict[str, Any]:
        return {"param": name, "returncode": 0, "value": values[name]}

    def param_set(name: str, value: float) -> dict[str, Any]:
        values[name] = value
        return {
            "param": name,
            "requested_value": value,
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    def reset() -> None:
        nonlocal reset_count
        reset_count += 1

    profile = scenario.build_thermal_weather_requested_profile(
        temperature_c=45.0,
        pressure_hpa=1013.0,
        thermal_battery_drain_factor=None,
        thermal_motor_derate_factor=None,
    )
    result = environmental_realism.run_thermal_weather_realism(
        profile=profile,
        param_show=param_show,
        param_set=param_set,
        reset_battery_status_cache=reset,
        battery_status_sample=lambda: {
            "battery_status_observed": True,
            "battery_remaining_percent": 80.0,
        },
        sleep=lambda _seconds: None,
        observed_at=FIXED_TIME,
    )

    application = result["thermal_weather_simulator_condition_application"]
    evidence = result["observed_thermal_weather_evidence"]
    assert application["application_status"] == "applied_with_approximations"
    assert evidence["observation_status"] == ("thermal_condition_param_readback_observed")
    assert evidence["observed"]["param_readback_matches_requested"] == {
        "MPC_THR_MAX": True,
        "SIM_BAT_DRAIN": True,
        "SIM_BAT_MIN_PCT": True,
    }
    assert reset_count == 1
    _assert_no_completion_or_physical_claims(application, evidence)


def test_sensor_failure_observation_requires_loss_and_cleanup() -> None:
    samples = iter(
        [
            {"sensor_gps_observed": True},
            {"sensor_gps_observed": False},
        ]
    )
    set_values: list[float] = []

    def param_set(_name: str, value: float) -> dict[str, Any]:
        set_values.append(value)
        return {
            "returncode": 0,
            "requested_value": value,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    profile = scenario.build_sensor_failure_requested_profile(
        sensor_component="gps",
        failure_type="off",
    )
    result = environmental_realism.run_sensor_failure_realism(
        profile=profile,
        param_set=param_set,
        sensor_gps_sample=lambda: next(samples),
        sleep=lambda _seconds: None,
        observed_at=FIXED_TIME,
    )

    application = result["sensor_failure_injection_application"]
    evidence = result["observed_sensor_condition_evidence"]
    assert set_values == [0.0, 1.0]
    assert application["application_status"] == "applied"
    assert application["reset"]["reset_observed"] is True
    assert evidence["observed"]["sensor_failure_effect_observed"] is True
    assert evidence["observed"]["cleanup_reset_observed"] is True
    _assert_no_completion_or_physical_claims(application, evidence)


def test_landing_zone_marker_is_visual_only(tmp_path: Path) -> None:
    root = _world_root(tmp_path, world.landing_zone_blocked_world_sdf_patch())

    result = environmental_realism.project_landing_zone_blocked_realism(
        requested=True,
        payload_model_root=root,
        observed_at=FIXED_TIME,
    )

    capability = result["gazebo_world_capability_matrix"]
    application = result["gazebo_world_application"]
    evidence = result["observed_world_condition_evidence"]
    assert capability["landing_zone_blocked_marker"] == ("supported_visual_only")
    assert capability["collision_enabled"] is False
    assert application["application_status"] == "applied"
    assert evidence["observed"]["landing_zone_blocked_does_not_verify_dropoff"] is True
    _assert_no_completion_or_physical_claims(application, evidence)


def test_visibility_fog_marker_is_not_sensor_or_route_evidence(
    tmp_path: Path,
) -> None:
    base = '<sdf version="1.9"><world name="default"><scene></scene></world></sdf>'
    root = _world_root(
        tmp_path,
        "",
    )
    (root / "worlds" / "default.sdf").write_text(world.inject_visibility_fog_render_marker(base))

    result = environmental_realism.project_visibility_realism(
        mode="fog",
        payload_model_root=root,
        observed_at=FIXED_TIME,
    )

    application = result["visibility_application"]
    evidence = result["observed_visibility_condition_evidence"]
    assert application["application_status"] == "applied_with_approximations"
    assert application["auto_gate"] is False
    assert evidence["observed"]["observed_fog_render_matches_requested"] is True
    assert evidence["observed"]["traffic_conflict_verified"] is False
    assert evidence["observed"]["route_blocking_verified"] is False
    _assert_no_completion_or_physical_claims(application, evidence)


def test_operational_marker_does_not_become_geofence_enforcement(
    tmp_path: Path,
) -> None:
    root = _world_root(tmp_path, world.no_fly_zone_world_sdf_patch())
    collision_motion = scenario.build_collision_obstacle_motion_spec(
        start_x_m=2.1,
        start_y_m=2.1,
        end_x_m=3.7,
        end_y_m=3.7,
    )

    result = environmental_realism.project_operational_markers_realism(
        payload_model_root=root,
        no_fly_zone_requested=True,
        traffic_conflict_requested=False,
        alternate_landing_requested=False,
        rth_behavior_requested=False,
        moving_actor_requested=False,
        collision_obstacle_requested=False,
        collision_contact_topic_requested=False,
        multi_drone_conflict_probe_requested=False,
        moving_actor_motion_spec=world.moving_actor_waypoint_motion_spec(),
        moving_actor_trajectory_definition_sha256=(
            world.moving_actor_waypoint_trajectory_definition_sha256()
        ),
        collision_obstacle_motion_spec=collision_motion,
        collision_obstacle_contact_topic="/fixture/contact",
        observed_at=FIXED_TIME,
    )

    profile = result["operational_condition_profile"]
    capability = result["operational_capability_matrix"]
    application = result["operational_application"]
    evidence = result["observed_operational_condition_evidence"]
    assert profile["requested"]["visual_only"] is True
    assert profile["requested"]["enforcement_enabled"] is False
    assert capability["no_fly_zone_marker"] == "supported_visual_only"
    assert capability["geofence_enforcement"] == "unsupported"
    assert evidence["observed"]["geofence_enforcement_observed"] is False
    _assert_no_completion_or_physical_claims(application, evidence)


def test_multi_drone_probe_without_world_stays_unsupported() -> None:
    collision_motion = scenario.build_collision_obstacle_motion_spec(
        start_x_m=2.1,
        start_y_m=2.1,
        end_x_m=3.7,
        end_y_m=3.7,
    )
    result = environmental_realism.project_operational_markers_realism(
        payload_model_root=None,
        no_fly_zone_requested=False,
        traffic_conflict_requested=False,
        alternate_landing_requested=False,
        rth_behavior_requested=False,
        moving_actor_requested=False,
        collision_obstacle_requested=False,
        collision_contact_topic_requested=False,
        multi_drone_conflict_probe_requested=True,
        moving_actor_motion_spec=world.moving_actor_waypoint_motion_spec(),
        moving_actor_trajectory_definition_sha256=(
            world.moving_actor_waypoint_trajectory_definition_sha256()
        ),
        collision_obstacle_motion_spec=collision_motion,
        collision_obstacle_contact_topic="/fixture/contact",
        observed_at=FIXED_TIME,
    )

    capability = result["operational_capability_matrix"]
    contract = result["multi_vehicle_frame_contract"]
    evidence = result["observed_operational_condition_evidence"]
    assert capability["multi_drone_conflict_probe"] == "unsupported"
    assert contract["multi_vehicle_enabled"] is False
    assert contract["traffic_conflict_verified"] is False
    assert evidence["observed"]["multi_drone_conflict_verified"] is False
    _assert_no_completion_or_physical_claims(contract, evidence)


def test_entrypoint_passes_explicit_operational_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def project(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"projected": True}

    monkeypatch.setattr(
        route_entrypoint,
        "_project_operational_markers_realism",
        project,
    )
    monkeypatch.setattr(
        route_entrypoint,
        "_no_fly_zone_marker_requested",
        lambda: True,
    )
    monkeypatch.setattr(
        route_entrypoint,
        "_traffic_conflict_marker_requested",
        lambda: False,
    )

    result = route_entrypoint._operational_no_fly_zone_realism(payload_model_root=None)

    assert result == {"projected": True}
    assert captured["no_fly_zone_requested"] is True
    assert captured["traffic_conflict_requested"] is False
    assert captured["payload_model_root"] is None
