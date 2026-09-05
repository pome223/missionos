"""Exercise the emitted simulator code, not a second measurement implementation."""

import ast
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import smoke_missionos_auto_mission_full_runtime_probe as probe
from src.runtime.px4_gazebo_route.hazard_state import _performance_envelope


@pytest.fixture
def emitted():
    source = probe._inner_runtime_probe_script(
        dropoff_dwell_mission_seq=2, land_mission_seq=3,
        release_altitude_target_m=30.0, release_altitude_tolerance_m=2.0,
        required_dwell_seconds=2.0, monitor_seconds=60.0, min_progress_m=1.0,
        no_progress_grace_seconds=10.0, min_route_altitude_m=20.0,
        altitude_grace_seconds=10.0, min_battery_remaining_percent=20.0,
        post_abort_wait_seconds=10.0, land_post_abort_wait_seconds=10.0,
        rtl_post_abort_wait_seconds=10.0, rtl_recovery_min_progress_m=5.0,
        sim_battery_min_remaining_percent=15.0, sim_battery_drain_seconds=600.0,
        thermal_motor_derate_factor=None, wind_mean_mps=None,
        wind_direction_deg=None, wind_gust_mps=None, wind_variance=None,
        gz_physical_battery_enabled=False, resume_mission_seq_after_obstacle=1,
    )
    tree = ast.parse(source)
    names = {"operator_performance_observation", "observe_operator_maneuver",
             "operator_maneuver_target", "run_operator_maneuver"}
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    env = {"math": math, "NAV_OFFBOARD": 14, "NAV_AUTO_MISSION": 3,
           "NAV_AUTO_LOITER": 4, "OPERATOR_RECOVERY_CALIBRATION_MIN_SAMPLE_COUNT": 5,
           "OPERATOR_RECOVERY_ASSIST_PRESTREAM_FRAMES": 20,
           "OPERATOR_RECOVERY_ASSIST_SETPOINT_INTERVAL_SECONDS": 0.05,
           "OPERATOR_RECOVERY_ASSIST_MAX_SECONDS": 75.0,
           "OPERATOR_RECOVERY_ASSIST_OBSTACLE_AVOIDANCE_MAX_SECONDS": 240.0,
           "OPERATOR_RECOVERY_LATERAL_OBSTACLE_MARGIN_M": 20.0,
           "RESUME_MISSION_SEQ_AFTER_OBSTACLE": 1,
           "MAVLINK_MSG_ID_MISSION_SET_CURRENT": 41}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "<emitted-probe>", "exec"), env)
    return env


def _samples():
    return [dict(elapsed_seconds=10.0 + i, position_timestamp_us=1_000_000 * (i+1),
                 x_m=x, y_m=0.0, nav_state=14)
            for i, x in enumerate([0.0, 1.0, 3.0, 6.0, 9.0, 9.8, 10.0])]


def test_complete_leg_speed_excludes_resume_wait_but_records_it(emitted):
    build = emitted["operator_performance_observation"]
    short = build("calibrate_offboard", _samples(), True, 10.0, 1.0, True)
    long = build("calibrate_offboard", _samples(), True, 10.0, 31.0, True)
    assert short["observation_status"] == long["observation_status"] == "measured"
    assert short["duration_seconds"] == long["duration_seconds"] == 6.0
    assert short["observed_horizontal_speed_mps"] == long["observed_horizontal_speed_mps"] == 1.666667
    assert long["non_movement_duration_seconds"] == 41.0
    assert long["measurement_start_elapsed_seconds"] == 10.0
    assert long["measurement_end_elapsed_seconds"] == 16.0


@pytest.mark.parametrize("case", ["short", "repeated_pose", "clock", "missing", "nan", "wrong_mode", "unfinished", "unsettled"])
def test_inadequate_measurement_cannot_verify_an_envelope(emitted, case):
    samples = deepcopy(_samples())
    if case == "short":
        for sample in samples:
            sample["x_m"] *= 0.25
    elif case == "repeated_pose":
        samples[3]["position_timestamp_us"] = samples[2]["position_timestamp_us"]
    elif case == "clock":
        samples[3]["elapsed_seconds"] = samples[2]["elapsed_seconds"]
    elif case == "missing":
        samples[3]["x_m"] = None
    elif case == "nan":
        samples[3]["x_m"] = float("nan")
    elif case == "wrong_mode":
        samples[3]["nav_state"] = 3
    observation = emitted["operator_performance_observation"](
        "calibrate_offboard", samples, case != "unfinished", 10.0, 1.0, case != "unsettled"
    )
    envelope = _performance_envelope(
        telemetry_snapshot={"recovery": {"performance_observation": observation}},
        recovery_policy={"offboard_performance_min_samples": 5, "offboard_performance_uncertainty_fraction": 0.25},
    )
    assert observation["observation_status"] == "unverified"
    assert envelope["envelope_status"] == "unverified"
    assert "performance_envelope_measurement_unverified" in envelope["blocking_reasons"]


def test_emitted_executor_freezes_samples_before_slow_auto_resume(emitted):
    # Fake physics/transport only; execute the actual generated control loop.
    state = dict(t=1.0, x=0.0, target=0.0, speed=0.0, nav=3)

    def sleep(dt):
        distance = state["target"] - state["x"]
        state["speed"] = min(2.0, abs(distance) / dt) if dt else 0.0
        state["x"] += math.copysign(min(abs(distance), 2.0 * dt), distance)
        state["t"] += dt

    def sendto(packet, _remote):
        if isinstance(packet, tuple):
            state["target"] = packet[0]

    def listener(topic, _count):
        if topic == "vehicle_status":
            return {"nav_state": state["nav"]}
        return {"x": state["x"], "y": 0.0, "z": -30.5, "vx": state["speed"],
                "vy": 0.0, "timestamp": int(state["t"] * 1_000_000)}

    def command(*_args):
        # Resume wait must not dilute movement speed.
        state["nav"] = 3
        state["t"] += 31.0
        return {"ack_result": 0, "ack_observed": True}

    def offboard(*_args):
        state["nav"] = 14
        return {"ack_result": 0, "ack_observed": True}

    emitted.update(
        time=SimpleNamespace(monotonic=lambda: state["t"], sleep=sleep),
        listener=listener, parse_float=lambda text, key: text.get(key),
        parse_int=lambda text, key: text.get(key) if isinstance(text, dict) else None, heartbeat=lambda _seq: None,
        setpoint_local_ned=lambda x, *_args: (x,),
        send_command_with_recovery_setpoints=offboard, send_command=command,
        wait_nav_state=lambda *_args: {"observed": True, "status_text": {"nav_state": 3}},
        recovery_parameter=lambda req, *keys: next((req["recovery_parameters"][k] for k in keys if k in req["recovery_parameters"]), None),
        recovery_flag=lambda req, key, default: req["recovery_parameters"].get(key, default),
        verify_recovery_resume=lambda *_args: {"resume_auto_authorized": True, "verification_status": "verified"},
    )
    result = emitted["run_operator_maneuver"](
        SimpleNamespace(sendto=sendto), None, 0,
        {"recovery_action": "calibrate_offboard", "recovery_parameters": {
            "target_x_m": 10.0, "target_y_m": 0.0, "target_altitude_m": 30.5,
            "calibration_only": True, "resume_original_route": True}},
        0.0, 0.0, -30.5, 30.5,
    )
    obs = result["performance_observation"]
    assert result["target_reached"] is True
    assert obs["observation_status"] == "measured"
    assert obs["duration_seconds"] < 10.0
    assert obs["post_maneuver_duration_seconds"] >= 31.0
    assert obs["horizontal_distance_m"] == 10.0
    assert len(result["calibration_setup_samples"]) >= 2
    assert all(s["horizontal_speed_mps"] <= 0.5 for s in result["maneuver_observation_samples"][-2:])
