from __future__ import annotations

import hashlib

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import verification


def test_legacy_route_entrypoint_uses_packaged_verifiers() -> None:
    bindings = {
        "_application_status_is_materialized": (
            verification.application_status_is_materialized
        ),
        "_px4_param_set_applied": verification.px4_param_set_applied,
        "_px4_param_value_matches": verification.px4_param_value_matches,
        "_route_corridor_obstacle_application_source_check": (
            verification.route_corridor_obstacle_application_source_check
        ),
        "_wind_readback_status": verification.wind_readback_status,
    }
    for legacy_name, packaged_function in bindings.items():
        assert getattr(route_entrypoint, legacy_name) is packaged_function


def test_materialized_application_status_is_an_explicit_allowlist() -> None:
    assert verification.application_status_is_materialized("applied") is True
    assert (
        verification.application_status_is_materialized("applied_with_approximations")
        is True
    )
    assert verification.application_status_is_materialized("proposed") is False
    assert verification.application_status_is_materialized(None) is False


def test_wind_readback_requires_observed_vector_match() -> None:
    wind_message = "linear_velocity {\n  x: 3.25\n  y: -1.5\n  z: 0\n}"
    output = (
        f"{wind_message}\n"
        "__BC_WIND_PUBLISH_STATUS=0\n"
        "__BC_WIND_READBACK_STATUS=0\n"
    )
    result = verification.wind_readback_status(
        output,
        expected_x=3.25,
        expected_y=-1.5,
    )

    assert result == {
        "readback_observed": True,
        "readback_source": "gz_topic_echo",
        "readback_publish_status": 0,
        "readback_status": 0,
        "readback_wind_vector_x_mps": 3.25,
        "readback_wind_vector_y_mps": -1.5,
        "readback_message_sha256": hashlib.sha256(
            wind_message.encode("utf-8")
        ).hexdigest(),
    }
    mismatch = verification.wind_readback_status(
        output,
        expected_x=9.0,
        expected_y=-1.5,
    )
    assert mismatch["readback_observed"] is False


def test_px4_parameter_verification_rejects_failed_or_missing_values() -> None:
    assert verification.px4_param_set_applied(
        {"returncode": 0, "stdout_tail": "set succeeded", "stderr_tail": ""}
    )
    assert not verification.px4_param_set_applied(
        {"returncode": 0, "stdout_tail": "parameter NOT FOUND", "stderr_tail": ""}
    )
    assert not verification.px4_param_set_applied(
        {"returncode": 1, "stdout_tail": "", "stderr_tail": "failed"}
    )

    assert verification.px4_param_value_matches(
        {"returncode": 0, "value": 1.00001},
        1.0,
    )
    assert not verification.px4_param_value_matches(
        {"returncode": 0, "value": None},
        1.0,
    )
    assert not verification.px4_param_value_matches(
        {"returncode": 1, "value": 1.0},
        1.0,
    )


def test_obstacle_application_verifier_reports_every_missing_fact() -> None:
    valid = {
        "schema_version": "gazebo_route_corridor_obstacle_spawn_application.v1",
        "application_id": (
            "gazebo_route_corridor_obstacle_spawn_application:"
            "mission_designer_collision_obstacle"
        ),
        "application_status": "applied",
        "observed": {
            "observed": True,
            "world_sdf_hash_match": True,
            "model_materialized": True,
            "collision_geometry_materialized": True,
            "trajectory_follower_materialized": True,
        },
    }
    assert verification.route_corridor_obstacle_application_source_check(valid) == (
        True,
        [],
    )

    accepted, reasons = verification.route_corridor_obstacle_application_source_check(
        {}
    )
    assert accepted is False
    assert reasons == [
        "gazebo_route_corridor_obstacle_spawn_schema_missing",
        "gazebo_route_corridor_obstacle_spawn_ref_missing",
        "gazebo_route_corridor_obstacle_spawn_not_applied",
        "gazebo_route_corridor_obstacle_spawn_not_observed",
        "gazebo_route_corridor_obstacle_world_sdf_hash_not_verified",
        "gazebo_route_corridor_obstacle_model_not_materialized",
        "gazebo_route_corridor_obstacle_collision_not_materialized",
        "gazebo_route_corridor_obstacle_motion_not_materialized",
    ]
