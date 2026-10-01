"""Independent axis, authority, freshness and recorded PX4 boundary checks."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

import pytest

from src.runtime.ship_vla_adapter import (
    assess_proposal,
    bind_proposal,
    entry_limits,
    inspect_live_smoke,
    make_envelope,
    snapshot_from_px4,
    verify_guard_artifacts,
)


def context():
    config = {
        "run_id": "run",
        "world_sha256": "world",
        "plan_sha256": "plan",
        "operator_approval_ref": "operator:explicit-sitl-opt-in",
        "timeout_s": 900,
        "urban": {"entry_north_m": 70, "cruise_altitude_m": 30},
    }
    config["urban"]["vla_guard_limits"] = entry_limits(config["urban"])
    envelope = make_envelope(config)
    observation = {
        **{
            k: envelope[k]
            for k in ("run_id", "world_sha256", "plan_sha256", "clock_id", "frame_id")
        },
        "phase": "urban_entry",
        "observed_at_s": 50.0,
        "pose_age_s": 0.01,
        "image_age_s": 0.1,
        "image_sha256": "a" * 64,
        "position_ned_m": [70.0, 0.0, -30.0],
        "heading_ned_rad": 0.0,
        "velocity_ned_mps": [0.0, 0.0, 0.0],
        "position_valid": True,
        "nav_state": 4,
        "arming_state": 2,
        "reset_counters": [0, 0, 0],
    }
    return config, envelope, observation


def check(text="55 49 49", *, change=None, archived=False, now=50.5):
    _, envelope, observation = context()
    current = deepcopy(observation)
    if change:
        change(envelope, observation, current)
    return assess_proposal(
        bind_proposal(text, observation, envelope),
        observation,
        current,
        envelope,
        now_s=now,
        archived=archived,
    )


def test_ned_axes_yaw_first_and_large_yaw_suppression():
    result = check(
        "98 49 49",
        change=lambda e, o, c: (
            o.update(heading_ned_rad=math.pi / 2) or c.update(heading_ned_rad=math.pi / 2)
        ),
    )
    assert result["candidate"]["target_ned_m"] == pytest.approx([70, 5, -30])
    assert result["candidate_eligible"] is True
    result = check("98 49 55")  # 0.1347 rad: rotate first, then horizontal motion
    target = result["candidate"]["target_ned_m"]
    assert target[0] == pytest.approx(74.95471392597495)
    assert target[1] == pytest.approx(0.6714348378179359)
    result = check("98 49 98")  # 1.1 rad: native controller suppresses horizontal
    assert result["candidate"]["target_ned_m"] == [70, 0, -30]
    assert result["candidate"]["horizontal_translation_suppressed"] is True


def test_positive_down_is_descent_and_never_clamped():
    result = check("55 85 49</s>")
    assert result["candidate"]["target_ned_m"][2] == pytest.approx(-26.3265306122449)
    assert "altitude_envelope_violation" in result["reasons"]


def test_derived_candidate_is_exact_under_one_ulp_math_library_variation(monkeypatch):
    # Worker Linux libm and host macOS libm differed by 5.55e-17 m in a real
    # permit. Canonicalize derived values at production, not verifier tolerance.
    baseline = check("55 47 55")
    original_sin, original_cos = math.sin, math.cos
    monkeypatch.setattr(math, "sin", lambda x: math.nextafter(original_sin(x), math.inf))
    monkeypatch.setattr(math, "cos", lambda x: math.nextafter(original_cos(x), -math.inf))
    assert check("55 47 55") == baseline


def test_derived_heading_is_exact_under_one_ulp_atan2_variation(monkeypatch):
    config, _, _ = context()
    row = px4_row()
    baseline = snapshot_from_px4(row, config)
    original_atan2 = math.atan2
    monkeypatch.setattr(math, "atan2", lambda a, b: math.nextafter(original_atan2(a, b), math.inf))
    assert snapshot_from_px4(row, config) == baseline


@pytest.mark.parametrize(
    "text",
    [
        "LAND",
        "<LAND>",
        "0 49 49",
        "55 49 49 LAND",
        "[55 49 49",
        "55 49 49]",
        "99 49 49",
        "55 49 49 extra",
        "55 -1 49",
        "55 NaN 49",
    ],
)
def test_invalid_or_terminal_outputs_fail_closed(text):
    assert not check(text)["candidate_eligible"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("observed_at_s", 40, "input_observation_stale_or_future"),
        ("observed_at_s", 51, "input_observation_stale_or_future"),
        ("image_age_s", 5, "input_observation_stale_or_future"),
        ("position_valid", False, "input_hold_not_verified"),
        ("nav_state", 3, "input_hold_not_verified"),
        ("arming_state", 1, "input_hold_not_verified"),
        ("velocity_ned_mps", [1, 0, 0], "input_hold_not_verified"),
        ("phase", "delivery", "input_phase_mismatch"),
        ("frame_id", "gazebo", "observation_binding_mismatch:frame_id"),
        ("clock_id", "unix", "observation_binding_mismatch:clock_id"),
    ],
)
def test_untrusted_or_stale_input_rejected(field, value, reason):
    result = check(change=lambda e, o, c: o.update({field: value}))
    assert reason in result["reasons"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("position_ned_m", [71, 0, -30], "vehicle_moved_since_input"),
        ("heading_ned_rad", 0.2, "vehicle_rotated_since_input"),
        ("reset_counters", [0, 1, 0], "estimator_reset_since_input"),
        ("observed_at_s", 49, "current_observation_precedes_input"),
    ],
)
def test_inference_interval_changes_rejected(field, value, reason):
    assert reason in check(change=lambda e, o, c: c.update({field: value}))["reasons"]


def test_inflated_segment_and_expiry_not_only_end_position():
    result = check(
        change=lambda e, o, c: (
            o.update(position_ned_m=[60.1, 0, -30]) or c.update(position_ned_m=[60.1, 0, -30])
        )
    )
    assert "horizontal_envelope_violation" in result["reasons"]
    assert (
        "motion_outlives_envelope"
        in check(change=lambda e, o, c: e.update(expires_at_s=51))["reasons"]
    )
    assert "envelope_expired" in check(change=lambda e, o, c: e.update(expires_at_s=40))["reasons"]
    assert (
        "inspection_not_authorized"
        in check(change=lambda e, o, c: e.update(inspection_authorized=False))["reasons"]
    )
    assert "archived_observation_not_live_authority" in check(archived=True)["reasons"]


def test_model_cannot_set_authority_and_receipt_never_grants_dispatch():
    _, envelope, observation = context()
    proposal = bind_proposal("55 49 49", observation, envelope)
    proposal["dispatch_allowed"] = True
    result = assess_proposal(proposal, observation, observation, envelope, now_s=50)
    assert result["reasons"] == ["model_must_not_supply_authority_or_state"]
    proposal = bind_proposal("55 49 49", observation, envelope)
    proposal["binding"]["plan_sha256"] = "old plan"
    assert not assess_proposal(proposal, observation, observation, envelope, now_s=50)[
        "candidate_eligible"
    ]
    result = check()
    assert result["status"] == "candidate_only"
    assert all(
        result[k] is False
        for k in (
            "approval_granted",
            "dispatch_allowed",
            "dispatch_invoked",
            "physical_execution_invoked",
        )
    )


def px4_row():
    return {
        "run_id": "run",
        "world_sha256": "world",
        "plan_sha256": "plan",
        "elapsed_s": 50.0,
        "attitude_q": [1.0, 0.0, 0.0, 0.0],
        "px4_query_duration_s": 0.1,
        "local_ned": [70.0, 0.0, -30.0],
        "velocity_ned": [0.0, 0.0, 0.0],
        "position_valid": True,
        "nav_state": 4,
        "arming_state": 2,
        "onboard_frame": {
            "file": "rgb/1.png",
            "sha256": hashlib.sha256(b"image").hexdigest(),
            "received_age_s": 0.05,
        },
        "raw_px4": {
            "vehicle_local_position": "timestamp: 10000 (0.001000 seconds ago)\ntimestamp_sample: 6000\nx: 70\ny: 0\nz: -30\nvx: 0\nvy: 0\nvz: 0\nxy_valid: True\nz_valid: True\nv_xy_valid: True\nv_z_valid: True\nheading_good_for_control: True\nxy_reset_counter: 0\nz_reset_counter: 0",
            "vehicle_attitude": "timestamp: 10000 (0.001000 seconds ago)\ntimestamp_sample: 6000\nq: [1, 0, 0, 0]\nquat_reset_counter: 0",
            "vehicle_status": "nav_state: 4\narming_state: 2",
        },
    }


def test_px4_age_includes_query_time_and_raw_state_is_bound():
    config, _, _ = context()
    row = px4_row()
    assert snapshot_from_px4(row, config)["pose_age_s"] == pytest.approx(0.105)
    row["local_ned"][2] = -20
    with pytest.raises(ValueError, match="state_not_bound"):
        snapshot_from_px4(row, config)


def test_recorded_guard_reopened_and_tampering_rejected(tmp_path):
    config, _, _ = context()
    row = px4_row()
    receipt = inspect_live_smoke(row, config)
    assert receipt["all_expected"] is True
    (tmp_path / "rgb").mkdir()
    (tmp_path / "rgb/1.png").write_bytes(b"image")
    source = Path("src/runtime/ship_vla_adapter.py").read_bytes()
    (tmp_path / "sources").mkdir()
    for path in (tmp_path / "ship_vla_adapter.py", tmp_path / "sources/ship_vla_adapter.py"):
        path.write_bytes(source)
    config["urban"]["onboard_source_sha256"] = {
        "ship_vla_adapter.py": hashlib.sha256(source).hexdigest()
    }
    path = tmp_path / "vla-guard-smoke.json"
    path.write_text(json.dumps(receipt))
    event = {
        "event": "vla_guard_inspected",
        "elapsed_s": 50.1,
        "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    events = [
        {"event": "urban_entry_observed", "elapsed_s": 49},
        event,
        {"event": "urban_decision", "elapsed_s": 55},
    ]
    assert verify_guard_artifacts(tmp_path, config, [row], events)["verified"]
    receipt["tests"][1]["assessment"]["candidate_eligible"] = True
    path.write_text(json.dumps(receipt))
    event["receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="not_reproducible"):
        verify_guard_artifacts(tmp_path, config, [row], events)


def test_guard_requires_onboard_before_starting_docker(monkeypatch, tmp_path):
    from src.runtime.ship_delivery import ShipDeliveryScenario
    from src.runtime.ship_delivery_sitl import run_ship_delivery_sitl

    monkeypatch.setattr(
        "src.runtime.ship_delivery_sitl._run", lambda *a, **k: pytest.fail("Docker invoked")
    )
    with pytest.raises(ValueError, match="requires an onboard"):
        run_ship_delivery_sitl(
            ShipDeliveryScenario(),
            output_dir=tmp_path / "run",
            operator_approved=True,
            vla_guard_smoke=True,
        )


def test_nonfinite_observation_never_becomes_candidate():
    _, envelope, observation = context()
    proposal = bind_proposal("55 49 49", observation, envelope)
    observation["position_ned_m"][0] = float("nan")
    result = assess_proposal(proposal, observation, observation, envelope, now_s=50)
    assert result["reasons"] == ["invalid_observation"]
