"""Vehicle shortening is explicit, immutable and subject to the original bounds."""

from copy import deepcopy
import math

import pytest

from scripts.yokohama_endpoint_feedback import FIXED, validate_feedback_candidate
from scripts.yokohama_goal_distance_adapter import POLICY, adapt_candidate, validate_adaptation
from src.runtime.yokohama_native import digest, vla_candidate


def inputs(x=2.8, text="43 49 49"):
    home = [0.0, 0.0, 15.0]
    cfg = {
        "run_id": "adapter-test",
        "world": {
            "world_sha256": "a" * 64,
            "source_sha256": {"collision-footprints.geojson": "b" * 64},
        },
        "flight_stages": [
            {"name": name, "target_world_xyz_m": home.copy()}
            for name in ("00-D1", "01-FEEDBACK-EXIT")
        ],
        "decisions": {
            "goal_distance_adapter": deepcopy(POLICY),
            "backend": "fixture",
            "wam_profile": "motion-v4",
            "points": ["D1"],
            "sea_leg_present": False,
            "payload_release_present": False,
            "endpoint_feedback": dict(
                FIXED,
                entry_world_xyz_m=home,
                exit_world_xyz_m=home.copy(),
                goal_world_xyz_m=[4.0, 0.0, 15.0],
                map_sha256="b" * 64,
            ),
        },
    }
    row = {
        "run_id": cfg["run_id"],
        "world_sha256": "a" * 64,
        "sim_s": 10,
        "wall_s": 10,
        "vehicle": {"xyz": [x, 0.0, 15.0], "quat_wxyz": [1, 0, 0, 0], "age_s": 0.01},
        "velocity_ned": [0, 0, 0],
        "heading_ned_rad": math.pi / 2,
        "battery_fraction": 0.9,
        "position_valid": True,
        "arming_state": 2,
        "landed": False,
        "reset_counters": [0, 0, 0],
        "nav_state": 4,
    }
    return cfg, row, vla_candidate(text, row)


def proposal(cfg, row, raw):
    selected, receipt = adapt_candidate(cfg, raw, row, "c" * 64)
    return dict(
        candidate=selected,
        vehicle_distance_adjustment=receipt,
        input_observation=row,
        vla_response_sha256="c" * 64,
    )


def test_second_translation_is_shortened_without_rewriting_raw_output():
    cfg, row, raw = inputs()
    frozen = deepcopy(raw)
    with pytest.raises(ValueError):
        validate_feedback_candidate(cfg, row["vehicle"]["xyz"], raw["target_world_xyz_m"])
    p = proposal(cfg, row, raw)
    receipt = p["vehicle_distance_adjustment"]
    assert raw == frozen and receipt["original_candidate"] == frozen
    assert p["candidate"]["target_world_xyz_m"] == [4.0, 0.0, 15.0]
    assert receipt["executed_distance_m"] == pytest.approx(1.2)
    assert receipt["original_distance_m"] == pytest.approx(43 / 98 * 5)
    assert receipt["reason"] == "shorten_at_approved_goal_plane"
    assert receipt["raw_model_output_evaluation"] is False
    assert receipt["grants_dispatch_authority"] is False
    assert receipt["original_candidate_sha256"] == digest(raw)
    for field in (
        "bins",
        "target_heading_ned_rad",
        "target_heading_world_ned_rad",
        "heading_source",
    ):
        assert p["candidate"][field] == raw[field]
    assert p["candidate"]["delta_body_frd"][3] == raw["delta_body_frd"][3]
    validate_adaptation(cfg, p)


@pytest.mark.parametrize("remaining", [0.6, 1.2, 2.1, 3.0])
@pytest.mark.parametrize("yaw_bin", [45, 49, 53])
def test_only_translation_magnitude_can_decrease(remaining, yaw_bin):
    cfg, row, raw = inputs(4 - remaining, f"43 49 {yaw_bin}")
    p = proposal(cfg, row, raw)
    start = row["vehicle"]["xyz"]
    before = [b - a for a, b in zip(start, raw["target_world_xyz_m"])]
    after = [b - a for a, b in zip(start, p["candidate"]["target_world_xyz_m"])]
    assert math.hypot(*after) <= math.hypot(*before) + 1e-9
    for original, executed in zip(before, after):
        assert executed == pytest.approx(
            original * p["vehicle_distance_adjustment"]["scale"], abs=1e-9
        )
    assert p["candidate"]["delta_body_frd"][3] == raw["delta_body_frd"][3]
    validate_feedback_candidate(cfg, start, p["candidate"]["target_world_xyz_m"])


def test_adapter_is_opt_in_and_does_not_make_legacy_raw_candidate_safe():
    cfg, row, raw = inputs()
    del cfg["decisions"]["goal_distance_adapter"]
    selected, receipt = adapt_candidate(cfg, raw, row, "c" * 64)
    assert selected == raw and selected is not raw and receipt is None
    with pytest.raises(ValueError):
        validate_feedback_candidate(cfg, row["vehicle"]["xyz"], selected["target_world_xyz_m"])
    with pytest.raises(ValueError, match="Undeclared"):
        validate_adaptation(cfg, {"vehicle_distance_adjustment": {}})


@pytest.mark.parametrize(
    "fault", ["original", "selected", "receipt", "response", "observation", "policy", "missing"]
)
def test_tampering_cannot_reach_wam_or_dispatch(fault):
    cfg, row, raw = inputs()
    p = proposal(cfg, row, raw)
    if fault == "original":
        p["vehicle_distance_adjustment"]["original_candidate"]["target_world_xyz_m"][0] += 0.1
    elif fault == "selected":
        p["candidate"]["target_world_xyz_m"][0] += 0.1
    elif fault == "receipt":
        p["vehicle_distance_adjustment"]["scale"] = 1
    elif fault == "response":
        p["vla_response_sha256"] = "d" * 64
    elif fault == "observation":
        p["input_observation"]["wall_s"] += 1
    elif fault == "policy":
        cfg["decisions"]["goal_distance_adapter"]["allow_extension"] = True
    else:
        del p["vehicle_distance_adjustment"]
    with pytest.raises(ValueError):
        validate_adaptation(cfg, p)


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "battery",
        "moving",
        "altitude",
        "nan",
        "off_corridor",
        "raw_modified",
        "raw_long",
        "raw_down",
        "unbound_response",
        "too_short",
        "already_goal",
        "away",
    ],
)
def test_adapter_does_not_rescue_invalid_inputs(fault):
    cfg, row, raw = inputs()
    response = "c" * 64
    if fault == "stale":
        row["vehicle"]["age_s"] = 2.01
    elif fault == "battery":
        row["battery_fraction"] = 0.19
    elif fault == "moving":
        row["velocity_ned"] = [0.31, 0, 0]
    elif fault == "altitude":
        row["vehicle"]["xyz"][2] += 0.51
    elif fault == "nan":
        row["wall_s"] = math.nan
    elif fault == "off_corridor":
        row["vehicle"]["xyz"][1] = 2
    elif fault == "raw_modified":
        raw["target_world_xyz_m"][0] -= 0.5
    elif fault == "raw_long":
        raw = vla_candidate("98 49 49", row)
    elif fault == "raw_down":
        row["vehicle"]["xyz"][2] = 15.3
        raw = vla_candidate("43 49 49", row)
    elif fault == "unbound_response":
        response = "not-a-hash"
    elif fault in ("too_short", "already_goal"):
        cfg, row, raw = inputs(3.6 if fault == "too_short" else 4)
    else:
        row["vehicle"]["quat_wxyz"] = [0, 0, 0, 1]
        raw = vla_candidate("43 49 49", row)
    with pytest.raises(ValueError):
        adapt_candidate(cfg, raw, row, response)


def test_existing_minimum_progress_is_not_relaxed():
    cfg, row, raw = inputs()
    cfg["decisions"]["endpoint_feedback"]["minimum_progress_m"] = 0
    with pytest.raises(ValueError, match="Unsupported endpoint"):
        adapt_candidate(cfg, raw, row, "c" * 64)


@pytest.mark.parametrize("fault", ["changed_receipt", "missing_receipt", "expired"])
def test_worker_rejects_changed_adjustment_or_expired_activation(tmp_path, fault):
    from scripts.yokohama_decision_worker import CityDecisions

    cfg, row, raw = inputs()
    p = proposal(cfg, row, raw)
    prepared = dict(
        candidate=p["candidate"], vehicle_distance_adjustment=p["vehicle_distance_adjustment"]
    )
    active = dict(
        prepared,
        prepared_permit_sha256=digest(prepared),
        observation_sha256=digest(row),
        expires_at_worker_wall_s=12,
    )
    if fault == "changed_receipt":
        active["vehicle_distance_adjustment"] = dict(p["vehicle_distance_adjustment"], scale=1)
    elif fault == "missing_receipt":
        del active["vehicle_distance_adjustment"]
    else:
        active["expires_at_worker_wall_s"] = 9
    events = []
    worker = CityDecisions(
        tmp_path, cfg, lambda: row, lambda name, **kw: events.append(name), lambda: 10
    )
    worker.exchange = lambda *args, **kw: active
    with pytest.raises(ValueError, match="stale or unbound"):
        worker.activation_permit(prepared)
    assert "city_permit_consumed" not in events


def test_adapter_does_not_reopen_revoked_worker(tmp_path):
    from scripts.yokohama_decision_worker import CityDecisions

    cfg, row, raw = inputs()
    worker = CityDecisions(tmp_path, cfg, lambda: row, lambda *args, **kw: None, lambda: 10)
    worker.closed = True
    with pytest.raises(ValueError, match="inactive"):
        worker.exchange("authorize", row, vla=proposal(cfg, row, raw))
    assert not list(tmp_path.iterdir())


def test_native_adapter_requires_a_fresh_separate_runtime_approval(tmp_path):
    from scripts.yokohama_sitl import main

    with pytest.raises(SystemExit) as failure:
        main(
            [
                "--phase",
                "flight",
                "--output-dir",
                str(tmp_path / "run"),
                "--approve-sitl",
                "--endpoint-feedback",
                "--goal-distance-adapter",
                "--decision-backend",
                "native",
                "--wam-profile",
                "motion-v4",
            ]
        )
    assert failure.value.code == 2 and not (tmp_path / "run").exists()


@pytest.mark.parametrize("fault", ["no_endpoint", "fixed_rejection_recovery"])
def test_adapter_cannot_enter_an_unapproved_or_previous_recovery_contract(fault):
    from scripts.yokohama_endpoint_feedback import feedback_policy

    cfg, _, _ = inputs()
    if fault == "no_endpoint":
        del cfg["decisions"]["endpoint_feedback"]
    else:
        cfg["candidate_recovery"] = {}
    with pytest.raises(ValueError):
        feedback_policy(cfg)
