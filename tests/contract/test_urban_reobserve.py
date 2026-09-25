"""Freshness/authority and real candidate-divergence checks; no aircraft I/O."""

import math

import pytest

from scripts.px4_aerial_flight_session import sign_command
from scripts.select_urban_depth_route import rank_routes
from scripts.urban_navigation_contract import digest, route_check
from scripts.urban_reobserve_contract import (
    CHECKPOINT,
    BARRIER,
    planner,
    routes,
    truth_scene,
    validate,
)


def test_checkpoint_capture_is_narrow_and_origin_probe_unchanged():
    from scripts.probe_px4_aerial_camera import CAPTURE
    from scripts.urban_checkpoint_capture import capture_program, CHECKPOINT_GUARD

    assert capture_program("approach") == CAPTURE
    assert CHECKPOINT_GUARD in capture_program("resume")
    compile(capture_program("resume"), "checkpoint-capture", "exec")
    for xyz, expected in (
        ([3, 0, 3], True),
        ([0, 0, 3], False),
        ([3.3, 0, 3], False),
        ([3, 0, 4], False),
    ):
        assert eval(CHECKPOINT_GUARD, {"abs": abs, "xyz": xyz}) is expected
    with pytest.raises(ValueError):
        capture_program("arbitrary_location")


def test_camera_only_change_diverges_from_frozen_route_without_truth_input():
    p = planner("resume", CHECKPOINT)
    assert BARRIER["lower_enu_m"] not in [b["lower_enu_m"] for b in p["map_boxes"]]
    assert rank_routes(p, [])["route_id"] == "forward"
    sensed = [[7.5, y, z] for y in (-1, 0, 1) for z in (2.5, 3, 3.5)]
    choice = rank_routes(p, sensed)
    assert choice["route_id"] == "left_detour"
    assert choice["scores"]["forward"]["rejected_by_observed_surface"]
    assert not route_check(truth_scene(), routes("resume")["forward"], start=CHECKPOINT)[
        "admissible"
    ]
    assert route_check(truth_scene(), routes("resume")["left_detour"], start=CHECKPOINT)[
        "admissible"
    ]


def fixture():
    config = {
        "session_id": "session",
        "scene_sha256": "scene",
        "approved_instruction_ref": "user-instruction",
        "reobserve_policy_sha256": "policy",
        "mode": "reobserve",
    }
    selection = {
        "schema_version": "urban_reobserve_selection.v1",
        "stage": "resume",
        **{k: config[k] for k in ("session_id", "scene_sha256", "mode")},
        "model_invoked": False,
        "route_id": "left_detour",
        "route_sha256": digest(routes("resume")["left_detour"]),
        "observation_unix_s": 100,
        "observation_enu_m": CHECKPOINT,
        "observation_yaw_ned_rad": math.pi / 2,
    }
    current = {
        "gazebo_pose_enu_m": list(CHECKPOINT),
        "yaw_ned_rad": math.pi / 2,
        "local_ned_velocity_mps": [0, 0, 0],
        "telemetry_age_seconds": 0.1,
    }
    command = {
        k: config[k]
        for k in (
            "session_id",
            "scene_sha256",
            "approved_instruction_ref",
            "reobserve_policy_sha256",
        )
    }
    command.update(
        stage="resume",
        selection_sha256=digest(selection),
        issued_at_unix_s=100,
        expires_at_unix_s=104,
    )
    return config, selection, current, command


def test_fresh_scoped_decision_admitted():
    config, selection, current, command = fixture()
    assert (
        validate(sign_command(command, b"key"), config, b"key", selection, current, 101, "resume")
        == 1
    )


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "expired",
        "stage",
        "task",
        "policy",
        "selection",
        "moving",
        "translated",
        "turned",
        "telemetry",
    ],
)
def test_changed_authority_or_observation_rejected(change):
    config, selection, current, command = fixture()
    now = 101
    if change == "expired":
        now = 104
    elif change == "stage":
        command["stage"] = "approach"
    elif change == "task":
        command["session_id"] = "other"
    elif change == "policy":
        command["reobserve_policy_sha256"] = "other"
    elif change == "selection":
        selection["route_id"] = "forward"
    elif change == "moving":
        current["local_ned_velocity_mps"] = [0.2, 0, 0]
    elif change == "translated":
        current["gazebo_pose_enu_m"] = [3.3, 0, 3]
    elif change == "turned":
        current["yaw_ned_rad"] += 0.3
    elif change == "telemetry":
        current["telemetry_age_seconds"] = 2
    envelope = sign_command(command, b"key")
    if change == "signature":
        envelope["hmac_sha256"] = "bad"
    with pytest.raises(ValueError):
        validate(envelope, config, b"key", selection, current, now, "resume")


def test_resigned_stale_input_and_route_tamper_rejected():
    for field, value in (("observation_unix_s", 90), ("route_sha256", "different")):
        config, selection, current, command = fixture()
        selection[field] = value
        command["selection_sha256"] = digest(selection)
        with pytest.raises(ValueError):
            validate(
                sign_command(command, b"key"), config, b"key", selection, current, 101, "resume"
            )
