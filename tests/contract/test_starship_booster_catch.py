"""Terminal surrogate mechanics, not launch-to-catch or SpaceX validation."""
from dataclasses import asdict, replace
import copy
import json
from pathlib import Path

import pytest

from src.runtime import starship_physics as env
from src.runtime.starship_booster_catch import _initialized, configuration, simulate_catch

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


@pytest.fixture(scope="module")
def config():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())


@pytest.fixture(scope="module")
def nominal(profile, config):
    return simulate_catch(profile, config)


def test_two_supports_integrate_until_engine_off_and_continuously_settled(nominal, config):
    result, record = nominal["outcome"], nominal["catch_record"]
    assert result["simulated_catch_supported"] is True
    assert result["termination"] == "supported_settled"
    assert result["catch_verified"] is False
    assert result["physical_execution"] is False
    assert record["initialization"] == {"kind": "terminal_initialized", "launch_connected": False}
    assert record["settling"]["observed_s"] == pytest.approx(2.)
    assert 1e6 < record["peak_support_force_n"] < config["maximum_support_force_n"]
    assert 0.1 < record["peak_compression_m"] < config["maximum_compression_m"]
    first_contact = next(f for f in record["frames"] if any(p["normal_force_n"] > 0 for p in f["pins"]))
    assert first_contact["engine_thrust_n"] > 1e6
    assert record["frames"][-1]["engine_thrust_n"] < config["settle_engine_thrust_n"]
    settled = [f for f in record["frames"] if f["time_s"] >= record["frames"][-1]["time_s"]-2.-1e-8]
    assert all(f["settle_eligible"] for f in settled)
    assert all(all(p["normal_force_n"] > 0 for p in f["pins"]) for f in settled)
    # Velocity is never set to zero; the compliant support still oscillates.
    assert env.norm(record["frames"][-1]["pins"][0]["relative_velocity_enu_mps"]) > 1e-5


def test_contact_halving_step_preserves_outcome_and_loads(profile, config, nominal):
    finer = simulate_catch(profile, config, dt_s=.005)
    assert finer["outcome"]["simulated_catch_supported"]
    assert finer["outcome"]["duration_s"] == pytest.approx(nominal["outcome"]["duration_s"], abs=.05)
    assert finer["catch_record"]["peak_support_force_n"] == pytest.approx(nominal["catch_record"]["peak_support_force_n"], rel=.005)
    assert finer["catch_record"]["peak_compression_m"] == pytest.approx(nominal["catch_record"]["peak_compression_m"], rel=.005)


def test_arms_close_with_finite_motion_and_contact_does_not_precede_top_eligibility(nominal, config):
    frames = nominal["catch_record"]["frames"]
    assert frames[0]["arm_half_span_m"] == config["arm_open_half_span_m"]
    assert frames[100]["arm_half_span_m"] == pytest.approx(7.)
    assert frames[0]["force_body_n"] == [0., 0., 0.]
    assert frames[-1]["arm_half_span_m"] == config["arm_closed_half_span_m"]
    for f in frames:
        for pin in f["pins"]:
            if pin["normal_force_n"]:
                assert pin["top_contact_eligible"] and pin["footprint_active"]
            tangential = env.norm(pin["force_enu_n"][:2]+[0.])
            assert tangential <= config["friction_coefficient"]*pin["normal_force_n"]+1e-7


@pytest.mark.parametrize("scenario,termination", [
    ("booster_catch_tower_unavailable", "time_limit"),
    ("booster_catch_lateral_offset", "time_limit"),
    ("booster_catch_fast_descent", "time_limit"),
    ("booster_catch_one_support", "surface_contact"),
])
def test_missing_conditions_do_not_become_catches(profile, config, scenario, termination):
    result = simulate_catch(profile, config, scenario)
    assert result["scenario"] == scenario
    assert not result["outcome"]["simulated_catch_supported"]
    assert result["outcome"]["termination"] == termination
    frames = result["catch_record"]["frames"]
    assert frames[-1]["time_s"] == result["final_state"]["time_s"]
    assert frames[-1]["r_eci_m"] == result["samples"][-1]["r_eci_m"]
    if scenario != "booster_catch_one_support":
        assert all(all(p["normal_force_n"] == 0 for p in f["pins"]) for f in frames)
    if scenario == "booster_catch_tower_unavailable":
        assert all(f["arm_half_span_m"] == config["arm_open_half_span_m"] for f in frames)
        assert all(not f["eligibility"]["rules_catch_allowed"] for f in frames)
        assert frames[-1]["r_eci_m"] != frames[0]["r_eci_m"]
        assert frames[-1]["propellant_kg"] < frames[0]["propellant_kg"]


def test_supplied_state_preserves_pose_velocity_rates_and_engine_failure(profile, config):
    booster, state = _initialized(profile, config, "booster_catch")
    engines = list(state.engine_states)
    engines[7] = replace(engines[7], available=False)
    state = replace(state, engine_states=tuple(engines), omega_body_rad_s=(.001, -.002, .003))
    original = asdict(state)
    result = simulate_catch(profile, config, initial_state=original, duration_s=.02)
    assert original == asdict(state)
    for field in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s"):
        assert result["initial_state"][field] == list(original[field])
    assert result["final_state"]["engine_states"][7]["available"] is False
    assert result["catch_record"]["initialization"]["kind"] == "supplied_state"
    assert not result["outcome"]["simulated_catch_supported"]
    assert len(booster.engines) == len(result["final_state"]["engine_states"])


def test_net_thrust_trim_is_command_only_and_retains_finite_contact_mechanics(profile, config):
    _, state = _initialized(profile, config, "booster_catch")
    initial = asdict(state)
    original = copy.deepcopy(initial)
    run = simulate_catch(profile, config, initial_state=initial,
                         control_policy="net_thrust_trim_v1", duration_s=30.)
    assert initial == original
    assert run["catch_record"]["control_policy"] == "net_thrust_trim_v1"
    assert run["catch_record"]["configuration"] == config
    assert run["catch_record"]["requested_duration_s"] == 30.
    assert run["outcome"]["simulated_catch_supported"]
    for name in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s"):
        assert run["initial_state"][name] == list(original[name])
    assert run["initial_state"]["engine_states"] == list(original["engine_states"])
    assert any("net_thrust_trim_quaternion" in s["controller"] for s in run["samples"])
    assert run["catch_record"]["peak_support_force_n"] > 1e6
    assert run["catch_record"]["peak_compression_m"] > .1
    assert run["catch_record"]["settling"]["observed_s"] >= 2.-1e-8


@pytest.mark.parametrize("scenario", ["booster_catch_tower_unavailable", "booster_catch_lateral_offset",
                                     "booster_catch_fast_descent", "booster_catch_one_support"])
def test_trim_does_not_bypass_failed_capture_conditions(profile, config, scenario):
    run = simulate_catch(profile, config, scenario, control_policy="net_thrust_trim_v1", duration_s=30.)
    assert run["outcome"]["simulated_catch_supported"] is False
    assert run["outcome"]["physical_execution"] is False


def test_overlap_below_support_plane_cannot_be_acquired_later_from_underneath(profile, config):
    _, state = _initialized(profile, config, "booster_catch")
    up, _, _ = env.local_frame(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(up, -4.)))
    result = simulate_catch(profile, config, initial_state=asdict(state), duration_s=3.)
    assert all(p["normal_force_n"] == 0 for f in result["catch_record"]["frames"] for p in f["pins"])
    assert not result["outcome"]["simulated_catch_supported"]


def test_overload_is_preserved_without_force_clamp_or_success(profile, config):
    fast_arms = {**config, "arm_speed_mps": 100.}
    result = simulate_catch(profile, fast_arms, "booster_catch_fast_descent")
    assert result["outcome"]["termination"] == "support_overload"
    assert result["catch_record"]["peak_support_force_n"] > config["maximum_support_force_n"]
    assert not result["outcome"]["simulated_catch_supported"]


@pytest.mark.parametrize("field,value", [("integration_dt_s", .02), ("duration_s", 31),
    ("settle_time_s", 1), ("normal_stiffness_npm", float("nan")), ("maximum_support_force_n", True)])
def test_unsafe_configuration_is_rejected(config, field, value):
    altered = copy.deepcopy(config)
    altered[field] = value
    with pytest.raises(ValueError):
        configuration(altered)


@pytest.mark.parametrize("kwargs", [{"duration_s": 31}, {"dt_s": .1}, {"dt_s": True}, {"dt_s": 1e-300},
                                    {"dt_s": 1e-9}, {"duration_s": 1e-300},
                                    {"duration_s": float("inf")}, {"scenario": "actual_spacex_catch"},
                                    {"control_policy": "teleport"}])
def test_invalid_runtime_parameters_are_rejected(profile, config, kwargs):
    with pytest.raises(ValueError):
        simulate_catch(profile, config, **kwargs)
