"""Flight authorization, isolation and real wire-format boundary tests."""

import copy
import json
import math
import struct

import pytest

from scripts.px4_aerial_flight_session import (
    COMMAND_SCHEMA,
    CRC_EXTRA,
    FlightSession,
    assert_container,
    crc,
    decode_datagram,
    frame,
    main,
    sign_command,
    validate_command,
)


def command_fixture():
    config = {
        "session_id": "session-001",
        "scene_sha256": "a" * 64,
        "approved_instruction_ref": "user-authorized-isolated-simulation",
    }
    hover = {"local_ned_pose_m": [0.0, 0.0, -3.0], "yaw_ned_rad": math.pi / 2}
    current = {
        "phase": "holding",
        "hardware_target": False,
        "observed_at_unix_s": 100.0,
        "local_ned_pose_m": [0.0, 0.0, -3.0],
        "local_ned_velocity_mps": [0.0, 0.0, 0.0],
        "yaw_ned_rad": math.pi / 2,
        "scene_static_verified": True,
    }
    command = {
        "schema_version": COMMAND_SCHEMA,
        "session_id": config["session_id"],
        "command_id": "command-001",
        "action": "fly_candidate",
        "candidate_id": "left_5m",
        "candidate_sha256": "b" * 64,
        "scene_sha256": "a" * 64,
        "model_receipt_sha256": "c" * 64,
        "jev_receipt_sha256": "d" * 64,
        "approval_receipt_sha256": "e" * 64,
        "approved_instruction_ref": config["approved_instruction_ref"],
        "target_local_ned_m": [5.0, 0.0, -3.0],
        "issued_at_unix_s": 100.0,
        "expires_at_unix_s": 105.0,
    }
    return config, hover, current, command


def test_signed_bound_left_plan_is_accepted():
    config, hover, current, command = command_fixture()
    assert (
        validate_command(
            sign_command(command, b"key"), config, b"key", now=101.0, hover=hover, current=current
        )
        == command
    )


def test_mutated_candidate_after_signing_is_rejected():
    config, hover, current, command = command_fixture()
    envelope = sign_command(command, b"key")
    command["candidate_id"] = "right_5m"
    with pytest.raises(ValueError, match="signature"):
        validate_command(envelope, config, b"key", now=101.0, hover=hover, current=current)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "another-session"),
        ("scene_sha256", "f" * 64),
        ("approved_instruction_ref", "model-proposal"),
        ("model_receipt_sha256", ""),
        ("jev_receipt_sha256", ""),
        ("approval_receipt_sha256", ""),
        ("candidate_id", "forward_5m"),
        ("expires_at_unix_s", 101.0),
        ("issued_at_unix_s", 102.0),
        ("expires_at_unix_s", 106.0),
        ("target_local_ned_m", [-5.0, 0.0, -3.0]),
        ("target_local_ned_m", [5.0, 0.0, -4.0]),
        ("target_local_ned_m", [True, 0.0, -3.0]),
        ("target_local_ned_m", [math.nan, 0.0, -3.0]),
    ],
)
def test_invalid_bound_command_fails_before_motion(field, value):
    config, hover, current, command = command_fixture()
    command[field] = value
    with pytest.raises(ValueError):
        validate_command(
            sign_command(command, b"key"), config, b"key", now=101.0, hover=hover, current=current
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "taking_off"),
        ("hardware_target", True),
        ("observed_at_unix_s", 98.0),
        ("local_ned_pose_m", [1.0, 0.0, -3.0]),
        ("local_ned_velocity_mps", [0.0, 0.3, 0.0]),
        ("yaw_ned_rad", 0.0),
        ("scene_static_verified", False),
    ],
)
def test_stale_or_changed_execution_state_rejects_fly(field, value):
    config, hover, current, command = command_fixture()
    current[field] = value
    with pytest.raises(ValueError, match="stationary hover"):
        validate_command(
            sign_command(command, b"key"), config, b"key", now=101.0, hover=hover, current=current
        )


def test_land_remains_available_when_hover_is_not_valid():
    config, hover, current, command = command_fixture()
    command["action"] = "land"
    current["phase"] = "flying_candidate"
    validate_command(
        sign_command(command, b"key"), config, b"key", now=101.0, hover=hover, current=current
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("local_ned_pose_m", [math.nan, 0.0, -3.0]),
        ("local_ned_velocity_mps", [0.0, math.nan, 0.0]),
        ("yaw_ned_rad", math.nan),
        ("observed_at_unix_s", math.inf),
    ],
)
def test_nonfinite_execution_telemetry_fails_closed(field, value):
    config, hover, current, command = command_fixture()
    current[field] = value
    with pytest.raises(ValueError, match="finite"):
        validate_command(
            sign_command(command, b"key"), config, b"key", now=101.0, hover=hover, current=current
        )


def test_raw_mavlink_crc_and_truncated_ack():
    ack = frame(77, struct.pack("<H", 400), 1, system=1, component=1)
    # Accepted=0 is truncated on MAVLink2 and must decode as zero.
    assert list(decode_datagram(ack)) == [(77, struct.pack("<HB", 400, 0))]
    bad = ack[:-1] + bytes([ack[-1] ^ 1])
    assert list(decode_datagram(bad)) == []
    assert list(decode_datagram(frame(77, b"\x90\x01\x00", 1, system=2, component=1))) == []


def test_datagram_can_contain_multiple_verified_packets():
    heartbeat = struct.pack("<IBBBBB", 0, 2, 12, 128, 4, 3)
    data = frame(0, heartbeat, 3, system=1, component=1) + frame(
        245, b"\x00\x01", 4, system=1, component=1
    )
    assert list(decode_datagram(data)) == [(0, heartbeat), (245, b"\x00\x01")]


def test_mavlink1_requires_crc_and_full_mandatory_payload():
    payload = struct.pack("<HB", 21, 0)
    header = bytes([len(payload), 9, 1, 1, 77])
    message = b"\xfe" + header + payload + struct.pack("<H", crc(header + payload, CRC_EXTRA[77]))
    assert list(decode_datagram(message)) == [(77, payload)]
    assert list(decode_datagram(message[:-1])) == []


def container_fixture(directory):
    return {
        "Name": "/missionos-aerial-flight-e2e",
        "State": {"Running": True},
        "Config": {
            "Labels": {"missionos.scope": "aerial-wam-jev-sitl"},
            "Env": ["PX4_SIM_MODEL=gz_x500_depth"],
        },
        "HostConfig": {"NetworkMode": "none", "Privileged": False},
        "Mounts": [{"Source": str(directory), "Destination": "/session", "RW": True}],
    }


def test_container_guard_accepts_only_isolated_simulator(tmp_path):
    metadata = container_fixture(tmp_path)
    assert_container(metadata, tmp_path)
    for key, value in (
        ("NetworkMode", "host"),
        ("Privileged", True),
        ("Devices", ["/dev/ttyUSB0"]),
        ("PortBindings", {"14550": [{}]}),
    ):
        bad = copy.deepcopy(metadata)
        bad["HostConfig"][key] = value
        with pytest.raises(ValueError, match="isolated"):
            assert_container(bad, tmp_path)


def test_initialize_preserves_scene_assets_and_never_replaces_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_PX4_AERIAL_FLIGHT_SESSION", "1")
    (tmp_path / "scene.json").write_text('{"retained":true}')
    args = [
        "initialize",
        "--session-dir",
        str(tmp_path),
        "--scene-sha256",
        "a" * 64,
        "--approved-instruction-ref",
        "user-authorized-isolated-simulation",
    ]
    assert main(args) == 0
    assert json.loads((tmp_path / "scene.json").read_text()) == {"retained": True}
    key = (tmp_path / ".dispatch-key").read_bytes()
    with pytest.raises(ValueError, match="not be overwritten"):
        main(args)
    assert (tmp_path / ".dispatch-key").read_bytes() == key
    assert (tmp_path / ".dispatch-key").stat().st_mode & 0o777 == 0o600


def test_missing_opt_in_does_not_create_session(tmp_path, monkeypatch):
    monkeypatch.delenv("RUN_PX4_AERIAL_FLIGHT_SESSION", raising=False)
    with pytest.raises(ValueError, match="RUN_PX4_AERIAL"):
        main(
            [
                "initialize",
                "--session-dir",
                str(tmp_path),
                "--scene-sha256",
                "a" * 64,
                "--approved-instruction-ref",
                "ref",
            ]
        )
    assert not (tmp_path / "config.json").exists()


def test_strict_hover_requires_full_simulation_second_and_settled_pose():
    controller = object.__new__(FlightSession)
    samples = [
        ([0.0, 0.0, -2.84], [0.0, 0.0, 0.01], 0),
        ([0.0, 0.0, -2.98], [0.0, 0.0, 0.10], 1_000_000_000),
        ([0.0, 0.0, -2.99], [0.0, 0.0, 0.01], 2_000_000_000),
        ([0.0, 0.0, -2.99], [0.0, 0.0, 0.01], 2_000_000_000),
        ([0.0, 0.0, -2.99], [0.0, 0.0, 0.01], 2_900_000_000),
        ([0.0, 0.0, -2.99], [0.0, 0.0, 0.01], 3_000_000_000),
    ]
    results = []

    def fake_wait(predicate, timeout, description):
        for position, velocity, stamp in samples:
            state = {
                "local_ned_pose_m": position,
                "local_ned_velocity_mps": velocity,
                "pose_simulation_time_ns": stamp,
            }
            results.append(predicate(state))
        return state

    controller.wait = fake_wait
    controller.stationary_at(
        [0.0, 0.0, -3.0], 180, position_tolerance=0.05, speed_limit=0.08, stable_sim_seconds=1.0
    )
    assert results == [False, False, False, False, False, True]


def test_hover_observation_rejects_reset_simulation_clock():
    controller = object.__new__(FlightSession)

    def fake_wait(predicate, *_):
        for stamp in (2_000_000_000, 1_000_000_000):
            predicate(
                {
                    "local_ned_pose_m": [0.0, 0.0, -3.0],
                    "local_ned_velocity_mps": [0.0, 0.0, 0.0],
                    "pose_simulation_time_ns": stamp,
                }
            )

    controller.wait = fake_wait
    with pytest.raises(RuntimeError, match="clock regressed"):
        controller.stationary_at([0.0, 0.0, -3.0], 180, stable_sim_seconds=1.0)
