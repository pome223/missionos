"""Executor authorization, observed motion and MAVLink boundary regressions."""

from copy import deepcopy
import struct

import pytest

from test_ship_vla_adapter import context, px4_row
from src.runtime.ship_vla_adapter import bind_proposal, make_envelope, snapshot_from_px4
from src.runtime.ship_vla_execution import (
    authorize_candidate,
    at_target,
    check_execution_sample,
    executor_contract,
)
from scripts.ship_vla_mavlink import decode, frame, mode_command, setpoint, POSITION_YAW_MASK


def execution_context():
    config, _, _ = context()
    config["execution_scope"] = "sim"
    config["urban"].update(
        vla_executor_contract=executor_contract(), buildings=[], obstacle_size_m=[16, 8, 60]
    )
    row = px4_row()
    row.update(
        poses_fresh=True,
        vehicle={"id": 1, "xyz": [0, 70, 30]},
        urban_contact_monitors_connected=True,
        urban_contact_observed=False,
        urban_buildings=[],
        urban_obstacle={"id": 2, "xyz": [0, 170, 30]},
    )
    proposal = bind_proposal(
        executor_contract()["fixture_text"], snapshot_from_px4(row, config), make_envelope(config)
    )
    return config, row, proposal


def test_permission_is_separate_from_assessment_and_never_claims_dispatch():
    config, row, proposal = execution_context()
    permit = authorize_candidate(config, proposal, row, row, now_s=50)
    assert permit["dispatch_allowed"] and not permit["dispatch_invoked"]
    assert not permit["assessment"]["dispatch_allowed"]
    assert permit["clearance"]["source"] == "fresh_gazebo_geometry_not_onboard_perception"
    assert not permit["vla_inference_invoked"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("approval", "executor_not_explicitly_authorized"),
        ("scope", "executor_not_explicitly_authorized"),
        ("limits", "executor_not_explicitly_authorized"),
        ("collision", "candidate_obstacle_clearance_rejected"),
        ("stale_world", "fresh_simulator_clearance_unavailable"),
        ("stale_image", "candidate_rejected"),
        ("plan", "candidate_rejected"),
        ("wrong_proposal", "qualification_requires_explicit_fixture"),
    ],
)
def test_rejects_before_any_transport(mutation, reason):
    config, row, proposal = execution_context()
    current = deepcopy(row)
    if mutation == "approval":
        config["operator_approval_ref"] = None
    elif mutation == "scope":
        config["execution_scope"] = "physical"
    elif mutation == "limits":
        config["urban"]["vla_executor_contract"]["speed_mps"] = 10
    elif mutation == "collision":
        current["urban_obstacle"]["xyz"] = [0, 72, 30]
    elif mutation == "stale_world":
        current["poses_fresh"] = False
    elif mutation == "stale_image":
        current["onboard_frame"]["received_age_s"] = 3
    elif mutation == "plan":
        proposal["binding"]["plan_sha256"] = "other"
    elif mutation == "wrong_proposal":
        proposal["generated_text"] = "LAND"
    with pytest.raises(ValueError, match=reason):
        authorize_candidate(config, proposal, row, current, now_s=50)


def test_monitor_catches_reset_after_execution_starts():
    config, row, proposal = execution_context()
    permit = authorize_candidate(config, proposal, row, row, now_s=50)
    initial = snapshot_from_px4(row, config)
    row["raw_px4"]["vehicle_local_position"] = row["raw_px4"]["vehicle_local_position"].replace(
        "xy_reset_counter: 0", "xy_reset_counter: 1"
    )
    with pytest.raises(ValueError, match="reset"):
        check_execution_sample(row, initial, permit, config, modes={4})


def test_nominal_send_cannot_replace_observed_position_and_stop():
    config, row, proposal = execution_context()
    permit = authorize_candidate(config, proposal, row, row, now_s=50)
    state = snapshot_from_px4(row, config)
    assert not at_target(state, permit["candidate"], executor_contract())
    state["position_ned_m"] = permit["candidate"]["target_ned_m"]
    state["heading_ned_rad"] = permit["candidate"]["target_heading_ned_rad"]
    assert at_target(state, permit["candidate"], executor_contract())
    state["velocity_ned_mps"] = [0.4, 0, 0]
    assert not at_target(state, permit["candidate"], executor_contract())
    state["velocity_ned_mps"] = [0, 0, 0]
    state["position_ned_m"] = [*state["position_ned_m"][:2], -29]
    assert not at_target(state, permit["candidate"], executor_contract())


def test_setpoint_units_mask_yaw_and_target_ids():
    raw = setpoint([72, 1, -30.2], 0.13, 250)
    value = decode(raw)[0]
    data = struct.unpack("<IfffffffffffHBBB", value["payload"])
    assert data[1:4] == pytest.approx([72, 1, -30.2])
    assert data[10] == pytest.approx(0.13)
    assert data[-4:] == (POSITION_YAW_MASK, 1, 1, 1)
    assert data[4:10] == (0,) * 6
    damaged = bytearray(raw)
    damaged[15] ^= 1
    assert decode(bytes(damaged)) == []


def test_batched_crc_checked_ack_and_no_unsupported_commands():
    ack = frame(77, struct.pack("<HBBiBB", 176, 0, 0, 0, 255, 191), 1, system=1, component=1)
    assert len(decode(ack + ack)) == 2
    with pytest.raises(ValueError, match="unsupported_mode"):
        mode_command("land", 0)
    with pytest.raises(ValueError, match="nonfinite"):
        setpoint([0, 0, float("nan")], 0, 0)


def test_rejected_execution_does_not_open_transport_or_request_mode(monkeypatch, tmp_path):
    from scripts.ship_vla_executor import execute_fixture

    config, row, _ = execution_context()
    row["onboard_frame"]["received_age_s"] = 3
    monkeypatch.setattr(
        "scripts.ship_vla_executor.PositionStream", lambda *a, **kw: pytest.fail("Transport opened")
    )
    with pytest.raises(ValueError, match="candidate_rejected"):
        execute_fixture(
            tmp_path,
            config,
            lambda: row,
            lambda *a, **k: None,
            lambda *a, **k: pytest.fail("PX4 command sent"),
            "/unused/",
            lambda: 50.5,
        )


def test_executor_opt_in_requires_onboard_and_approval_before_docker(tmp_path, monkeypatch):
    from src.runtime.ship_delivery import ShipDeliveryScenario
    from src.runtime.ship_delivery_sitl import run_ship_delivery_sitl

    monkeypatch.setattr(
        "src.runtime.ship_delivery_sitl._run", lambda *a, **kw: pytest.fail("Docker invoked")
    )
    with pytest.raises(PermissionError):
        run_ship_delivery_sitl(
            ShipDeliveryScenario(), output_dir=tmp_path / "run", vla_executor_smoke=True
        )
    with pytest.raises(ValueError, match="requires an onboard"):
        run_ship_delivery_sitl(
            ShipDeliveryScenario(),
            output_dir=tmp_path / "run",
            operator_approved=True,
            vla_executor_smoke=True,
        )


def test_executor_uses_shared_worker_clock_after_observation_collection(monkeypatch, tmp_path):
    import json
    from scripts.ship_vla_executor import execute_fixture

    config, row, _ = execution_context()
    current_time = [50.0]

    def sample():
        current_time[0] += 0.05
        result = deepcopy(row)
        result["elapsed_s"] = current_time[0]
        current_time[0] += 0.01  # capture/write work after the row's timestamp
        return result

    calls = []

    def transport(*args):
        raise RuntimeError("reached_transport_with_fresh_permit")

    monkeypatch.setattr("scripts.ship_vla_executor.PositionStream", transport)
    with pytest.raises(RuntimeError, match="reached_transport"):
        execute_fixture(
            tmp_path,
            config,
            sample,
            lambda *a, **k: None,
            lambda argv: calls.append(argv),
            "/sim/",
            lambda: current_time[0],
        )
    receipt = json.loads((tmp_path / "vla-execution.json").read_text())
    permit = receipt["prestream"]["permit"]
    assert permit["issued_at_s"] > receipt["prestream"]["current_elapsed_s"]
    assert len(calls) == 1 and calls[0][0].endswith("mavlink")
    assert receipt["dispatch_invoked"] is False


def test_replicated_mode_acks_still_require_two_distinct_command_windows():
    from src.runtime.ship_vla_execution_verifier import verify_mode_ack_windows

    commands = [(10, 6), (20, 4)]
    assert verify_mode_ack_windows(commands, [10.1, 10.1001, 20.1, 20.1001]) == [
        [10.1, 10.1001],
        [20.1, 20.1001],
    ]
    for acks in ([10.1, 10.1001], [20.1, 20.1001], [9.9, 20.1], [10.1, 24]):
        with pytest.raises(ValueError):
            verify_mode_ack_windows(commands, acks)


def test_reused_mission_uploader_keeps_px4_publishers_alive(monkeypatch, capsys):
    import json
    import socket
    import subprocess
    from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script

    program = _inner_upload_script(reuse_mavlink_session=True)
    namespace = {}

    class Socket:
        def __init__(self, *args):
            self.index = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def settimeout(self, *args):
            pass

        def bind(self, *args):
            pass

        def sendto(self, *args):
            pass

        def recvfrom(self, *args):
            if self.index in (0, 1, 6):
                mid, payload = 47, bytes([255, 190, 0])
            else:
                mid, payload = 51, struct.pack("<HBB", self.index - 2, 255, 190)
            self.index += 1
            return namespace["frame"](mid, payload, self.index), ("127.0.0.1", 14604)

    monkeypatch.setattr(socket, "socket", Socket)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("Existing PX4 MAVLink session restarted")
    )
    exec(compile(program, "reused_mission_uploader", "exec"), namespace)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["mavlink_session_reused"] is True
    assert receipt["mavlink_start_returncode"] is None
    assert receipt["mission_request_sequences"] == [0, 1, 2, 3]
    assert receipt["mission_ack_type"] == 0
