"""Short initialized return cases; not evidence of a real booster recovery."""
from dataclasses import asdict, replace
import copy
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_booster import DEFAULTS, _landing_engine_demand, simulate_booster, _drag_aware_stop_estimate, _coast_control_profile
from src.runtime.starship_sixdof_mission import _attitude, vehicle


@pytest.fixture
def profile():
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "examples/spaceflight/starship-sixdof-profile.json").read_text())


def initialized(profile, *, altitude=50_000., vertical=600., horizontal=1000., fuel=200_000., attitude="up", throttle=0.):
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], altitude,
                               fuel, time_s=180.)
    up, east, north = env.local_frame(point)
    quaternion = _attitude(up if attitude == "up" else env.scale(east, -1), north)
    state = dyn.State6DOF(time_s=point.time_s, r_eci_m=point.r,
                          v_eci_mps=env.add(point.v, env.add(env.scale(up, vertical), env.scale(east, horizontal))),
                          q_body_to_eci=quaternion, omega_body_rad_s=(0., 0., 0.), propellant_kg=fuel,
                          engine_states=tuple(dyn.EngineState(throttle=throttle if "main" in e.name else 0.) for e in booster.engines),
                          flap_angles_rad=tuple(0. for _ in booster.aero_panels))
    return booster, state


def test_return_inherits_pose_velocity_rates_actuators_and_fault_without_mutation(profile):
    booster, state = initialized(profile, throttle=.7)
    engines = list(state.engine_states)
    engines[3] = replace(engines[3], available=False)
    state = replace(state, omega_body_rad_s=(.001, -.002, .003), engine_states=tuple(engines))
    original = asdict(state)
    frozen = copy.deepcopy(original)
    result = simulate_booster(profile, original, duration_s=.3)
    assert original == frozen
    first = result["initial_state"]
    for key in ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg", "engine_states"):
        if isinstance(original[key], (tuple, list)):
            assert first[key] == list(original[key])
        else:
            assert first[key] == original[key]
    assert result["final_state"]["engine_states"][3]["available"] is False
    assert result["final_state"]["time_s"] == pytest.approx(180.3)
    assert result["outcome"]["duration_s"] == pytest.approx(.3)
    assert result["outcome"]["termination"] == "time_limit"
    assert all(p["body_id"] == "booster" for p in result["samples"])
    assert result["guidance_configuration"]["boostback_max_burn_s"] == DEFAULTS["boostback_max_burn_s"]
    assert len(booster.engines) == len(result["final_state"]["engine_states"])


def test_boostback_requests_configured_v3_main_engines_with_finite_spool(profile):
    _, state = initialized(profile, attitude="retrograde")
    result = simulate_booster(profile, asdict(state), duration_s=.3)
    first = result["initial_state"]
    assert first["phase"] == "booster_boostback"
    commands = first["command"]["engines"]
    assert all(x["enabled"] for x in commands[:33])
    assert first["applied_thrust_n"] == 0.
    assert 0 < result["final_state"]["engine_states"][0]["throttle"] < commands[0]["throttle"]
    assert result["final_state"]["propellant_kg"] < state.propellant_kg


def test_unaligned_slew_uses_three_finite_gimballed_engines_without_fake_torque(profile, monkeypatch):
    _, state = initialized(profile)
    actual = dyn.step
    def checked(before, craft, command, dt, **kwargs):
        assert "external_force_body_n" not in kwargs and "external_torque_body_nm" not in kwargs
        return actual(before, craft, command, dt, **kwargs)
    monkeypatch.setattr(dyn, "step", checked)
    result = simulate_booster(profile, asdict(state), duration_s=.6)
    first, last = result["initial_state"], result["samples"][-1]
    assert first["phase"] == "booster_boostback_slew"
    commands = first["command"]["engines"]
    assert all(x["enabled"] for x in commands[:3])
    assert all(not x["enabled"] for x in commands[3:33])
    assert any(abs(x["gimbal_x_rad"])+abs(x["gimbal_y_rad"]) > 0 for x in commands[:3])
    assert first["applied_thrust_n"] == 0
    assert last["applied_thrust_n"] > 0
    assert 0 < result["final_state"]["engine_states"][0]["throttle"] < commands[0]["throttle"]
    assert env.norm(result["final_state"]["omega_body_rad_s"]) > .001
    assert result["outcome"]["boostback_started"] is False
    assert any(e["event"] == "booster_powered_slew_command" for e in result["events"])


def test_landing_engine_selection_avoids_three_engine_minimum_overthrust_and_preserves_faults(profile):
    booster, state = initialized(profile)
    force = booster.engines[0].max_thrust_n
    count, throttle = _landing_engine_demand(state, booster, 1.1*force, 3)
    assert count == 2
    assert throttle == pytest.approx(.55)
    assert count*force*throttle == pytest.approx(1.1*force)
    assert 3*force*booster.engines[0].min_throttle > 1.1*force
    assert _landing_engine_demand(state, booster, .6*force, 3) == pytest.approx((1, .6))
    engines = list(state.engine_states)
    engines[0] = replace(engines[0], available=False)
    failed = replace(state, engine_states=tuple(engines))
    assert _landing_engine_demand(failed, booster, 1.1*force, 3) == pytest.approx((3, .55))


def test_reserve_cutoff_retains_physical_shutdown_spool(profile):
    _, state = initialized(profile, fuel=30_000., throttle=.7)
    result = simulate_booster(profile, asdict(state), duration_s=.1)
    assert result["initial_state"]["phase"] == "booster_entry_coast"
    assert all(not x["enabled"] for x in result["initial_state"]["command"]["engines"][:33])
    assert 0 < result["final_state"]["engine_states"][0]["throttle"] < .7
    assert result["final_state"]["propellant_kg"] < state.propellant_kg
    assert not any("entry_ignition" in x["event"] for x in result["events"])


def test_aligned_but_insufficient_fuel_does_not_report_boostback_ignition(profile):
    _, state = initialized(profile, fuel=30_000., attitude="retrograde")
    result = simulate_booster(profile, asdict(state), duration_s=.1)
    assert result["outcome"]["boostback_started"] is False
    assert not any(x["event"] == "booster_boostback_ignition_command" for x in result["events"])
    assert all(not c["enabled"] for c in result["initial_state"]["command"]["engines"][:33])


def test_landing_requests_three_main_engines_and_never_injects_external_wrench(profile, monkeypatch):
    _, state = initialized(profile, altitude=1000., vertical=-120., horizontal=0., fuel=30_000.)
    actual = dyn.step
    observed_commands = []
    def checked(before, craft, command, dt, **kwargs):
        assert "external_force_body_n" not in kwargs and "external_torque_body_nm" not in kwargs
        observed_commands.append(command)
        return actual(before, craft, command, dt, **kwargs)
    monkeypatch.setattr(dyn, "step", checked)
    result = simulate_booster(profile, asdict(state), duration_s=.2)
    assert result["initial_state"]["phase"] == "booster_landing_burn"
    commands = result["initial_state"]["command"]["engines"]
    assert all(x["enabled"] for x in commands[:3])
    assert all(not x["enabled"] for x in commands[3:33])
    assert observed_commands
    assert result["final_state"]["r_eci_m"] != state.r_eci_m
    assert result["outcome"]["catch_verified"] is False
    assert result["outcome"]["simulated_contact_envelope_met"] is False


def test_surface_impact_is_bracketed_and_not_declared_recovered(profile):
    booster, _ = initialized(profile)
    height = dyn.mass_properties(booster, 0.).com_body_m[2]+2.
    _, state = initialized(profile, altitude=height, vertical=-30., horizontal=0., fuel=0.)
    result = simulate_booster(profile, asdict(state), duration_s=2.)
    assert result["outcome"]["termination"] == "surface_contact"
    assert result["outcome"]["duration_s"] < 1.
    assert result["contact"]["contact"] is True
    assert result["contact"]["surface_relative_speed_mps"] > 20.
    assert abs(result["outcome"]["final_hull_clearance_m"]) < .1
    assert result["outcome"]["simulated_contact_envelope_met"] is False
    assert result["outcome"]["catch_verified"] is False
    assert result["final_state"]["propellant_kg"] == 0.


def test_initial_overlap_cannot_count_as_return_envelope_success(profile):
    booster, _ = initialized(profile)
    height = dyn.mass_properties(booster, 0.).com_body_m[2]-.1
    _, state = initialized(profile, altitude=height, vertical=0., horizontal=0., fuel=0.)
    result = simulate_booster(profile, asdict(state), duration_s=.1)
    assert result["contact"]["initial_overlap"] is True
    assert result["outcome"]["simulated_contact_envelope_met"] is False
    assert result["outcome"]["catch_verified"] is False


@pytest.mark.parametrize("duration", [0., -1., 2000.1, float("nan"), float("inf"), True])
def test_invalid_duration_is_rejected(profile, duration):
    _, state = initialized(profile)
    with pytest.raises(ValueError, match="duration"):
        simulate_booster(profile, asdict(state), duration_s=duration)


def test_invalid_state_actuator_count_is_not_reinitialized(profile):
    _, state = initialized(profile)
    wrong = asdict(state)
    wrong["engine_states"] = []
    with pytest.raises(ValueError, match="count mismatch"):
        simulate_booster(profile, wrong, duration_s=.1)


def test_unknown_or_nonfinite_guidance_configuration_is_rejected(profile):
    _, state = initialized(profile)
    for settings in ({"invented": 1}, {"landing_reserve_kg": float("nan")}, {"max_duration_s": 2001.},
                     {"boostback_slew_throttle": 1.1}, {"boostback_engine_count": 34}, {"boostback_engine_count": 3.5}):
        amended = copy.deepcopy(profile)
        amended["booster_return"] = settings
        with pytest.raises(ValueError, match="configuration"):
            simulate_booster(amended, asdict(state), duration_s=.1)


def test_angular_rate_failure_is_preserved_at_inherited_state(profile):
    _, state = initialized(profile)
    state = replace(state, omega_body_rad_s=(5.1, 0., 0.))
    result = simulate_booster(profile, asdict(state), duration_s=.1)
    assert result["outcome"]["termination"] == "angular_rate_envelope_exceeded"
    assert result["outcome"]["integration_steps"] == 0
    assert result["final_state"]["time_s"] == state.time_s
    assert result["outcome"]["simulated_contact_envelope_met"] is False
    assert all(math.isfinite(x) for x in result["final_state"]["omega_body_rad_s"])


def test_drag_preview_rejects_fuel_infeasible_burn_and_does_not_hang_at_empty_tank(profile):
    booster, state = initialized(profile, altitude=84_000., vertical=-1473., horizontal=0., fuel=75_000.)
    observed = dyn.observe(state, booster)
    up, _, _ = env.local_frame(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    before = asdict(state)
    preview = _drag_aware_stop_estimate(state, booster, observed, up, -1473., profile, 13)
    assert preview["burn_fuel_feasible"] is False
    assert preview["estimated_burn_propellant_kg"] == pytest.approx(75_000., abs=.001)
    assert preview["available_landing_engine_count"] == 13
    assert preview["estimated_burn_time_s"] < 10.
    assert asdict(state) == before


def test_drag_preview_accounts_for_speed_available_engines_and_fuel(profile):
    booster, state = initialized(profile, altitude=3000., vertical=-600., horizontal=0., fuel=75_000.)
    up, _, _ = env.local_frame(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    fast = _drag_aware_stop_estimate(state, booster, dyn.observe(state, booster), up, -600., profile, 13)
    _, slower = initialized(profile, altitude=3000., vertical=-400., horizontal=0., fuel=75_000.)
    slow = _drag_aware_stop_estimate(slower, booster, dyn.observe(slower, booster), up, -400., profile, 13)
    assert fast["burn_fuel_feasible"] and slow["burn_fuel_feasible"]
    assert 0 < slow["estimated_stopping_height_m"] < fast["estimated_stopping_height_m"] < 3000.
    assert slow["estimated_burn_propellant_kg"] < fast["estimated_burn_propellant_kg"] < 75_000.
    engines = list(state.engine_states)
    engines[0] = replace(engines[0], available=False)
    fault = replace(state, engine_states=tuple(engines))
    failed = _drag_aware_stop_estimate(fault, booster, dyn.observe(fault, booster), up, -600., profile, 13)
    assert failed["available_landing_engine_count"] == 12
    assert failed["available_landing_thrust_n"] < fast["available_landing_thrust_n"]


def test_coast_pd_authority_uses_jets_and_preserves_actual_shutdown_tvc_in_v3(profile):
    booster, state = initialized(profile, fuel=75_000., throttle=.4)
    observed = dyn.observe(state, booster)
    before = copy.deepcopy(profile)
    configured, frequency, alpha = _coast_control_profile(profile, observed)
    _, powered_frequency, powered_alpha = _coast_control_profile(profile, observed, include_spooled_tvc=True)
    assert profile == before
    assert 0 < frequency < powered_frequency <= profile["guidance"]["attitude_frequency_rad_s"]
    assert 0 < alpha < powered_alpha <= profile["guidance"]["max_angular_acceleration_rad_s2"]
    assert configured["guidance"]["attitude_frequency_rad_s"] == frequency


@pytest.mark.parametrize("policy", ["fixed_v1", "site_return_v1", "site_return_v2", "site_return_v3"])
def test_diagnostic_policies_preserve_exact_supplied_separation_and_are_not_catches(profile, policy):
    _, state = initialized(profile, altitude=50_000., vertical=-1000., horizontal=50., fuel=50_000.)
    result = simulate_booster(profile, asdict(state), duration_s=.1, guidance_policy=policy)
    assert result["guidance_policy"] == policy
    assert result["initial_state"]["r_eci_m"] == list(state.r_eci_m)
    assert result["initial_state"]["v_eci_mps"] == list(state.v_eci_mps)
    assert result["initial_state"]["q_body_to_eci"] == list(state.q_body_to_eci)
    assert result["outcome"]["catch_verified"] is False


def test_v3_spends_finite_fuel_to_mitigate_imminent_impact_when_complete_arrest_is_infeasible(profile):
    _, state = initialized(profile, altitude=4000., vertical=-1400., horizontal=0., fuel=50_000.)
    v2 = simulate_booster(profile, asdict(state), duration_s=.2, guidance_policy="site_return_v2")
    v3 = simulate_booster(profile, asdict(state), duration_s=.2, guidance_policy="site_return_v3")
    assert not any(e["event"] == "booster_landing_burn_requested" for e in v2["events"])
    event = next(e for e in v3["events"] if e["event"] == "booster_landing_burn_requested")
    assert event["fuel_feasible"] is False
    assert event["burn_intent"] == "impact_mitigation_insufficient_predicted_fuel"
    assert v3["final_state"]["propellant_kg"] < v2["final_state"]["propellant_kg"]


@pytest.mark.parametrize("step", [1e-300, 1e-8, float("nan"), True])
def test_return_producer_rejects_nonadvancing_or_unbounded_steps(profile, step):
    _, state = initialized(profile)
    tiny = copy.deepcopy(profile)
    tiny["integration"]["powered_dt_s"] = step
    with pytest.raises(ValueError, match="integration"):
        simulate_booster(tiny, asdict(state), duration_s=1.)


def test_return_producer_rejects_horizon_below_inherited_clock_resolution(profile):
    _, state = initialized(profile)
    with pytest.raises(ValueError, match="clock"):
        simulate_booster(profile, asdict(state), duration_s=1e-300)
