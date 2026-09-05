from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from src.runtime import (
    missionos_sitl_dispatch_runtime,
    px4_gazebo_mission_designer_sitl_live_flight_run,
)

RUNTIME_MODULE = "src.runtime.px4_gazebo_route.entrypoint"
LEGACY_SMOKE_MODULE = "scripts.smoke_px4_gazebo_horizontal_route_delivery"


def test_legacy_smoke_module_is_runtime_entrypoint_alias() -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    legacy = importlib.import_module(LEGACY_SMOKE_MODULE)

    assert legacy is runtime
    assert legacy.main is runtime.main
    assert legacy.OPT_IN_ENV == "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"


def test_missionos_dispatch_defaults_to_formal_runtime_module() -> None:
    assert missionos_sitl_dispatch_runtime._default_command() == [
        sys.executable,
        "-m",
        RUNTIME_MODULE,
    ]


def test_live_flight_defaults_to_formal_runtime_module() -> None:
    assert (
        px4_gazebo_mission_designer_sitl_live_flight_run._horizontal_route_runtime_command()
        == [sys.executable, "-m", RUNTIME_MODULE]
    )


def test_live_flight_mission_assurance_uses_bounded_rtl_adapter_flags() -> None:
    assert (
        px4_gazebo_mission_designer_sitl_live_flight_run._horizontal_route_runtime_command(
            mission_assurance_on_deviation=True
        )
        == [
            sys.executable,
            "-m",
            RUNTIME_MODULE,
            "--on-deviation-action",
            "rtl",
            "--max-pose-deviation-xy-m",
            "0.85",
            "--mission-assurance-on-deviation",
        ]
    )


def test_mission_assurance_telemetry_uses_wind_vector_magnitude(monkeypatch) -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    monkeypatch.setattr(
        runtime,
        "WIND_REALISM_SUMMARY",
        {
            "observed_environment_evidence": {
                "evidence_id": "fixture-wind-readback",
                "observation_status": "observed",
                "observed": {
                    "readback_wind_vector_x_mps": 0.0,
                    "readback_wind_vector_y_mps": 4.0,
                    "wind_direction_deg": 90.0,
                },
            }
        },
    )
    monkeypatch.setattr(
        runtime,
        "_battery_status_sample",
        lambda: {
            "battery_status_observed": True,
            "battery_remaining_percent": 80.0,
        },
    )

    bundle = runtime._live_mission_assurance_telemetry_bundle(
        phase="original",
        deviation={"deviation_xy_m": 1.0},
        pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
        current_pose={"x": 1.0, "y": 0.0, "z": 2.0},
        sample_index=10,
        elapsed_seconds=10.0,
        invocation_started_at=datetime.now(UTC),
    )

    assert bundle["telemetry_snapshot"]["wind"]["speed_mps"] == 4.0
    assert bundle["telemetry_snapshot"]["wind"]["gust_mps"] == 4.0


def test_bounded_mission_assurance_continue_resends_route_and_observes_effect(
    monkeypatch,
) -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    calls: list[dict[str, object]] = []
    pose_rows: list[tuple[str, dict[str, float]]] = []
    monkeypatch.setattr(
        runtime,
        "_assert_planned_route_stream_budget",
        lambda **values: calls.append({"budget": values}),
    )
    monkeypatch.setattr(
        runtime,
        "_send_route_with_monitor",
        lambda **values: (
            calls.append(values)
            or {
                "pose_deviation_aborted": False,
                "offboard_mode_switch_allowed": True,
                "offboard_mode_switch_command_id": 176,
                "offboard_mode_switch_frame_sent": True,
                "offboard_mode_switch_ack_required": True,
                "offboard_mode_switch_ack_command_id": 176,
                "offboard_mode_switch_ack_timeout_seconds": 5.0,
                "offboard_mode_switch_ack_observed": True,
                "offboard_mode_switch_ack_result_code": 0,
                "setpoint_frames_sent": 50,
                "route_stream_stop_reason": "duration_completed",
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_pose_sample",
        lambda: {"x": 2.0, "y": 0.0, "z": -2.0},
    )
    monkeypatch.setattr(
        runtime,
        "_append_live_pose_row",
        lambda phase, pose: pose_rows.append((phase, dict(pose))),
    )
    target = SimpleNamespace(
        sent_target_x_m=5.0,
        sent_target_y_m=0.0,
        target_z_m=-2.0,
        uncompensated_target_x_m=5.0,
        uncompensated_target_y_m=0.0,
        feed_forward_velocity_x_mps=0.0,
        feed_forward_velocity_y_mps=0.0,
        wind_compensation={
            "feed_forward_ramp_start_fraction": 0.65,
            "feed_forward_ramp_end_fraction": 0.9,
        },
    )

    evidence = runtime._execute_bounded_mission_assurance_continue(
        deviation={"sample": {"x": 1.0, "y": 0.0, "z": -2.0}},
        target=target,
        route=SimpleNamespace(
            altitude_max_m=10.0,
            max_pose_deviation_xy_m=0.85,
            max_pose_deviation_z_m=1.5,
        ),
        route_approval=SimpleNamespace(
            operator_approval_performed=True,
            approval_id="approval-1",
        ),
    )

    assert calls[1]["duration_seconds"] == 5.0
    assert calls[1]["on_deviation"] is None
    assert pose_rows == [
        ("mission_assurance_continue_resume", {"x": 2.0, "y": 0.0, "z": -2.0})
    ]
    assert evidence["existing_route_approval_consumed"] is True
    assert evidence["offboard_mode_switch_ack_observed"] is True
    assert evidence["setpoint_frames_sent"] == 50
    assert evidence["resume_displacement_m"] == 1.0
    assert evidence["target_distance_before_m"] == 4.0
    assert evidence["target_distance_after_m"] == 3.0
    assert evidence["route_resume_effect_observed"] is True
    assert evidence["route_completion_claimed"] is False
    assert evidence["physical_execution_invoked"] is False


def test_payload_release_records_event_pose_before_post_release_motion(
    monkeypatch,
) -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    monkeypatch.setenv(runtime.PAYLOAD_RELEASE_MODEL_ENV, "1")
    poses = iter(
        [
            {"x": 5.2, "y": 5.1, "z": 0.1},
            {"x": 5.9, "y": 5.2, "z": 0.0},
        ]
    )
    monkeypatch.setattr(runtime, "_payload_pose_sample", lambda: next(poses))
    monkeypatch.setattr(runtime, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    evidence = runtime._trigger_payload_release()

    assert evidence is not None
    assert evidence["payload_release_position_x_m"] == 5.2
    assert evidence["payload_release_position_y_m"] == 5.1
    assert evidence["payload_release_position_z_m"] == 0.1
    assert evidence["payload_pose_after_release_observation"] == {
        "x": 5.9,
        "y": 5.2,
        "z": 0.0,
    }


def test_mission_assurance_dropoff_approach_centers_before_release(
    monkeypatch,
) -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    poses = iter(
        [
            {"x": 5.2, "y": 5.1, "z": 2.4},
            {"x": 5.02, "y": 4.99, "z": 0.3},
        ]
    )
    monkeypatch.setattr(runtime, "_pose_sample", lambda: next(poses))
    monkeypatch.setattr(runtime, "_assert_planned_route_stream_budget", lambda **_: None)
    monkeypatch.setattr(
        runtime,
        "_send_route_with_monitor",
        lambda **_: {
            "pose_deviation_aborted": False,
            "offboard_mode_switch_ack_observed": True,
            "offboard_mode_switch_ack_result_code": 0,
            "setpoint_frames_sent": 50,
        },
    )
    pose_rows: list[tuple[str, dict[str, float]]] = []
    monkeypatch.setattr(
        runtime,
        "_append_live_pose_row",
        lambda phase, pose: pose_rows.append((phase, dict(pose))),
    )

    evidence = runtime._execute_mission_assurance_dropoff_approach(
        target=SimpleNamespace(
            uncompensated_target_x_m=5.0,
            uncompensated_target_y_m=5.0,
            target_z_m=-2.5,
        )
    )

    assert evidence["dropoff_approach_effect_observed"] is True
    assert evidence["horizontal_target_error_m"] < 0.03
    assert evidence["approach_observed_pose"]["z"] == 0.3
    assert evidence["dropoff_region_observed_at"]
    assert pose_rows[0][0] == "mission_assurance_dropoff_approach_cycle_1"
    assert evidence["dropoff_approach_cycle_count"] == 1


def test_mission_assurance_dropoff_approach_corrects_observed_wind_drift(
    monkeypatch,
) -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    poses = iter(
        [
            {"x": 5.1, "y": 5.0, "z": 2.4},
            {"x": 6.3, "y": 5.4, "z": 0.5},
            {"x": 5.1, "y": 5.05, "z": 0.3},
        ]
    )
    monkeypatch.setattr(runtime, "_pose_sample", lambda: next(poses))
    monkeypatch.setattr(runtime, "_assert_planned_route_stream_budget", lambda **_: None)
    route_calls: list[dict[str, float]] = []

    def _send_route(**kwargs):
        route_calls.append(kwargs)
        return {
            "pose_deviation_aborted": False,
            "offboard_mode_switch_ack_observed": True,
            "offboard_mode_switch_ack_result_code": 0,
            "setpoint_frames_sent": 50,
        }

    monkeypatch.setattr(runtime, "_send_route_with_monitor", _send_route)
    monkeypatch.setattr(runtime, "_append_live_pose_row", lambda *_args, **_kwargs: None)

    evidence = runtime._execute_mission_assurance_dropoff_approach(
        target=SimpleNamespace(
            uncompensated_target_x_m=5.0,
            uncompensated_target_y_m=5.0,
            target_z_m=-2.5,
        )
    )

    assert evidence["dropoff_approach_effect_observed"] is True
    assert evidence["dropoff_approach_cycle_count"] == 2
    assert route_calls[0]["target_x"] == 5.0
    assert route_calls[0]["target_y"] == 5.0
    assert route_calls[1]["target_x"] == 4.35
    assert route_calls[1]["target_y"] == 4.8
    assert route_calls[1]["expected_target_x"] == 4.35
    assert route_calls[1]["expected_target_y"] == 4.8
    assert route_calls[0]["duration_seconds"] == 5.0
    assert route_calls[1]["duration_seconds"] == 2.0
