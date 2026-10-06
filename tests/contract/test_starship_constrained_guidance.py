"""Development guidance requests remain finite, bounded and nonauthoritative."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_physics as env
from src.runtime.starship_constrained_guidance import (
    boostback_force, boostback_plan, entry_axis, fixed_bank_heading, landing_force, point_coast,
)
from src.runtime.starship_sixdof_mission import vehicle


@pytest.fixture(scope="module")
def configuration():
    base = Path(__file__).resolve().parents[2] / "examples" / "spaceflight"
    profile = json.loads((base / "starship-sixdof-profile.json").read_text())
    catch = json.loads((base / "starship-catch-profile.json").read_text())
    return profile, catch, vehicle(profile, "booster")


def test_point_coast_conserves_horizontal_direction_and_retains_negative_outcome(configuration):
    profile, _, booster = configuration
    p, v = [60000., -500., 130000.], [-230., 10., 750.]
    original = deepcopy((p, v, profile))
    axial = point_coast(p, v, 320000., booster, profile, entry_angle_deg=0.)
    trimmed = point_coast(p, v, 320000., booster, profile, entry_angle_deg=35.)
    assert axial["reached_terminal_altitude"] and trimmed["reached_terminal_altitude"]
    assert env.norm(trimmed["velocity_enu_mps"]) < env.norm(axial["velocity_enu_mps"])
    assert axial["velocity_enu_mps"][0] < 0 < axial["velocity_enu_mps"][1]
    assert axial["velocity_enu_mps"][0] / axial["velocity_enu_mps"][1] == pytest.approx(-23.)
    assert axial["prediction_is_execution"] is False
    assert trimmed["attitude_dynamics_integrated"] is False
    assert trimmed["lateral_lift_integrated"] is False
    assert (p, v, profile) == original


def test_full_panel_coast_contains_side_force_and_holds_declared_bank(configuration):
    profile, _, booster = configuration
    bearing = [-1., 0., 0.]
    axial = point_coast([10000., 0., 6000.], [0., 0., -300.], 320000., booster, profile,
                        bank_heading_enu=bearing)
    vector = point_coast([10000., 0., 6000.], [0., 0., -300.], 320000., booster, profile,
                         full_panel_vector=True, bank_heading_enu=bearing, bank_sign=-1)
    assert axial["position_enu_m"][0] == pytest.approx(10000.)
    assert vector["position_enu_m"][0] < 10000.
    assert vector["velocity_enu_mps"][0] < 0
    assert vector["lateral_lift_integrated"] is True
    assert vector["bank_heading_enu"] == bearing
    assert vector["bank_sign"] == -1
    assert vector["trim_update_count"] > 0
    assert vector["integration_steps"] <= vector["point_step_budget"] == 1000
    assert vector["attitude_dynamics_integrated"] is False
    assert bearing == [-1., 0., 0.]


def test_bank_seed_accounts_for_measured_velocity_without_mutation():
    p, v = [10000., 0., 50000.], [-100., 100., 500.]
    heading = fixed_bank_heading(p, v)
    assert env.norm(heading) == pytest.approx(1.)
    assert heading[2] == 0.
    assert heading[1] < 0
    assert p == [10000., 0., 50000.] and v == [-100., 100., 500.]


def test_command_shooting_is_bounded_and_positive_ideal_budget_is_not_admission(configuration):
    profile, catch, booster = configuration
    original = deepcopy((profile, catch))
    result = boostback_plan([34436., 0., 71147.], [1079.35, -4.35, 1291.84],
                           510000., 260000., booster, profile, catch)
    assert len(result["candidates"]) == 5
    assert result["point_continuation_count"] == 25
    assert result["point_budget_candidate_count"] > 0
    assert result["fuel_margin_kg"] > 0
    assert result["admissible"] is False
    assert result["production_policy_admitted"] is False
    assert result["prediction_is_execution"] is False
    for candidate in result["candidates"]:
        assert all(math.isfinite(x) for x in candidate["target_velocity_enu_mps"])
        assert 0 <= candidate["point_burn_duration_s"] <= 70. + 1e-9
        assert candidate["point_post_burn_fuel_kg"] >= profile["booster_return"]["landing_reserve_kg"]
        assert candidate["admissible"] is False
        assert env.norm(candidate["burn_axis_enu"]) == pytest.approx(1.)
        assert candidate["point_post_shutdown_fuel_kg"] < candidate["point_post_burn_fuel_kg"]
        assert candidate["point_shutdown_propellant_kg"] > 0
        assert candidate["point_shutdown_duration_s"] > candidate["point_controlled_settle_horizon_s"] > 0
        assert candidate["point_final_aggregate_throttle"] > 0
        assert candidate["point_post_shutdown_velocity_enu_mps"] != candidate["point_post_burn_velocity_enu_mps"]
    assert result["method"] == "fixed_axis_point_impulse"
    assert (profile, catch) == original


def test_boostback_force_tracks_vector_without_exceeding_main_thrust(configuration):
    profile, _, _ = configuration
    force, receipt = boostback_force([1000., 0., 1200.], [-200., 0., 800.], 500000., 9.81, profile)
    assert force[0] < 0 and force[2] < 0
    assert env.norm(force) <= profile["booster"]["engine_count"] * profile["booster"]["engine_thrust_n"] + 1e-8
    assert receipt["force_capacity_saturated"] is True
    assert receipt["force_request_is_achieved_thrust"] is False
    settled, _ = boostback_force([0., 0., 0.], [0., 0., 0.], 300000., 9.81, profile)
    assert settled == pytest.approx([0., 0., 300000.*9.81])


def test_fixed_impulse_axis_never_chases_the_endpoint_velocity_error(configuration):
    profile, _, _ = configuration
    axis = [-1., 0., 0.]
    first, receipt = boostback_force([1000., 0., 1200.], [-200., 0., 800.],
                                    500000., 9.81, profile, burn_axis_enu=axis)
    second, terminal = boostback_force([-205., 80., 900.], [-200., 0., 800.],
                                       320000., 9.81, profile, burn_axis_enu=axis)
    assert first == second
    assert receipt["force_reference"] == "fixed_axis_impulse"
    assert terminal["along_axis_velocity_remaining_mps"] < 12.
    assert terminal["velocity_error_mps"] > 100.
    assert terminal["perpendicular_velocity_error_mps"] > 100.
    assert terminal["force_request_is_achieved_thrust"] is False
    assert axis == [-1., 0., 0.]


@pytest.mark.parametrize("axis", [[0., 0., 0.], [2., 0., 0.], [True, 0., 0.], [float("nan"), 0., 0.]])
def test_invalid_fixed_impulse_axis_is_refused(configuration, axis):
    profile, _, _ = configuration
    with pytest.raises(ValueError):
        boostback_force([1000., 0., 1200.], [-200., 0., 800.], 500000., 9.81,
                        profile, burn_axis_enu=axis)


def test_entry_requires_live_mass_and_fuel_and_exposes_static_trim_boundary(configuration):
    profile, _, booster = configuration
    with pytest.raises(ValueError, match="observed_mass"):
        entry_axis([10000., 0., 30000.], [-200., 0., -1200.], profile)
    axis, receipt = entry_axis([10000., 500., 35000.], [-220., 20., -1300.], profile,
                               mass_kg=320000., propellant_kg=70000., vehicle=booster,
                               com_body_m=(0., 0., 32.65625))
    assert env.norm(axis) == pytest.approx(1.)
    assert receipt["trim_feasible_count"] > 0
    assert receipt["steady_trim_feasible"] is True
    assert receipt["prediction_is_execution"] is False
    assert receipt["force_prediction_is_achieved_force"] is False
    assert len(receipt["predicted_trim_flap_angles_rad"]) == len(booster.aero_panels)
    for angle, panel in zip(receipt["predicted_trim_flap_angles_rad"], booster.aero_panels):
        assert abs(angle) <= panel.max_deflection_rad + 1e-12
    assert receipt["selected_energy_deficit_mps2"] >= 0


def test_entry_prepares_upright_on_ascent_without_assignment(configuration):
    profile, _, booster = configuration
    axis, receipt = entry_axis([10000., 0., 130000.], [-220., 0., 750.], profile,
                               mass_kg=320000., propellant_kg=70000., vehicle=booster)
    assert axis == (0., 0., 1.)
    assert receipt["prediction_is_execution"] is False


def test_held_entry_bank_does_not_reverse_when_the_site_error_changes_sign(configuration):
    profile, _, booster = configuration
    axes = []
    for error in (10000., -10000.):
        axis, receipt = entry_axis([error, 0., 20000.], [0., 0., -1000.], profile,
                                   mass_kg=320000., propellant_kg=70000., vehicle=booster,
                                   com_body_m=(0., 0., 32.65625), bank_heading_enu=[-1., 0., 0.], bank_sign=-1)
        axes.append(axis)
        assert receipt["bank_bearing_held"] is True
        assert receipt["bank_heading_enu"] == [-1., 0., 0.]
        assert receipt["bank_sign"] == -1
        assert axis[0] >= 0
        assert axis[2] >= 0
    assert env.dot(*axes) >= 0.


@pytest.mark.parametrize("heading,sign", [([0., 0., 0.], -1), ([1., 0., 1.], -1),
                                          ([-1., 0., 0.], 0), ([-1., 0., 0.], True)])
def test_invalid_held_bank_is_refused(configuration, heading, sign):
    profile, _, booster = configuration
    with pytest.raises(ValueError):
        entry_axis([10000., 0., 20000.], [0., 0., -1000.], profile,
                   mass_kg=320000., propellant_kg=70000., vehicle=booster,
                   bank_heading_enu=heading, bank_sign=sign)


@pytest.mark.parametrize("velocity", [[-180., 30., 900.], [-180., 30., 0.], [0., 0., 0.],
                                      [-180., 30., -900.]])
def test_fixed_entry_prepares_descending_altitude_without_apex_singularity(configuration, velocity):
    profile, _, booster = configuration
    p, bearing = [55000., 700., 120000.], [-1., 0., 0.]
    before = deepcopy((p, velocity, bearing, profile))
    axis, receipt = entry_axis(p, velocity, profile, mass_kg=320000., propellant_kg=70000.,
                               vehicle=booster, com_body_m=(0., 0., 32.65625),
                               bank_heading_enu=bearing, bank_sign=-1, fixed_bank_angle_deg=35.)
    assert env.norm(axis) == pytest.approx(1.)
    assert all(math.isfinite(x) for x in axis)
    assert axis[2] >= 0.
    assert receipt["entry_preparation_lookahead_s"] > 0
    assert receipt["predicted_entry_position_enu_m"][2] == 80000.
    assert receipt["predicted_entry_velocity_enu_mps"][2] < 0
    assert receipt["bank_heading_enu"] == bearing and receipt["bank_sign"] == -1
    assert receipt["fixed_bank_trim_feasible"] is True
    assert receipt["entry_axis_uses_future_velocity"] is True
    assert receipt["lookahead_is_executed_velocity"] is False
    assert receipt["actual_body_axis_assigned"] is False
    assert receipt["prediction_is_execution"] is False
    assert receipt["production_policy_admitted"] is False
    assert "predicted_trim_flap_angles_rad" not in receipt
    assert "future_entry_trim" in receipt
    assert (p, velocity, bearing, profile) == before


def test_fixed_entry_at_upward_altitude_crossing_targets_the_later_descending_crossing(configuration):
    profile, _, booster = configuration
    axis, receipt = entry_axis([55000., 0., 80000.], [-180., 0., 800.], profile,
                               mass_kg=320000., propellant_kg=70000., vehicle=booster,
                               bank_heading_enu=[-1., 0., 0.], bank_sign=-1, fixed_bank_angle_deg=35.)
    assert env.norm(axis) == pytest.approx(1.)
    assert receipt["entry_preparation_lookahead_s"] > 0
    assert receipt["predicted_entry_velocity_enu_mps"][2] == pytest.approx(-800.)
    assert receipt["entry_axis_uses_future_velocity"] is True


def test_fixed_entry_bank_below_preparation_altitude_is_held_when_trim_feasible(configuration):
    profile, _, booster = configuration
    axis, receipt = entry_axis([10000., 0., 50000.], [0., 0., -1000.], profile,
                               mass_kg=320000., propellant_kg=70000., vehicle=booster,
                               bank_heading_enu=[-1., 0., 0.], bank_sign=-1, fixed_bank_angle_deg=35.)
    assert axis == pytest.approx([math.sin(math.radians(35.)), 0., math.cos(math.radians(35.))])
    assert receipt["entry_mode"] == "fixed_trimmed_entry_bank"
    assert receipt["entry_angle_deg"] == pytest.approx(35.)
    assert receipt["entry_preparation_lookahead_s"] == 0
    assert receipt["entry_axis_uses_future_velocity"] is False
    assert receipt["steady_trim_feasible"] is True


def test_failed_fixed_trim_is_explicitly_retained_with_command_fallback(configuration, monkeypatch):
    from src.runtime import starship_booster_recovery
    profile, _, booster = configuration
    def failed_trim(velocity, axis, density, panels, **kwargs):
        return (0., 0., 0.), {"steady_trim_feasible": False, "predicted_trim_flap_angles_rad": [0.]*len(panels)}
    monkeypatch.setattr(starship_booster_recovery, "_trim_entry_candidate", failed_trim)
    axis, receipt = entry_axis([10000., 0., 50000.], [0., 0., -1000.], profile,
                               mass_kg=320000., propellant_kg=70000., vehicle=booster,
                               bank_heading_enu=[-1., 0., 0.], bank_sign=-1, fixed_bank_angle_deg=35.)
    assert axis == pytest.approx([0., 0., 1.])
    assert receipt["fixed_bank_trim_feasible"] is False
    assert receipt["requested_bank_accepted"] is False
    assert receipt["entry_mode"] == "fixed_bank_trim_infeasible_fallback"
    assert receipt["requested_fixed_bank_trim"]["steady_trim_feasible"] is False
    assert receipt["production_policy_admitted"] is False


@pytest.mark.parametrize("angle,altitude", [(True, 80000.), (float("nan"), 80000.), (61., 80000.),
                                          (35., True), (35., 0.), (35., float("inf"))])
def test_invalid_fixed_bank_preparation_configuration_is_refused(configuration, angle, altitude):
    profile, _, booster = configuration
    with pytest.raises(ValueError):
        entry_axis([55000., 0., 120000.], [-180., 0., 800.], profile,
                   mass_kg=320000., propellant_kg=70000., vehicle=booster,
                   bank_heading_enu=[-1., 0., 0.], bank_sign=-1,
                   fixed_bank_angle_deg=angle, preparation_altitude_m=altitude)


def test_terminal_force_compensates_aerodynamics_and_uses_pin_clearance(configuration):
    profile, catch, _ = configuration
    mass = 300000.
    inputs = ([.1, -.1, 4.], 3.4, [0., 0., -1.5], [.2, -.3, 1.], mass, 50000., profile, catch)
    force, receipt = landing_force(*inputs)
    force_without_aero, _ = landing_force(*inputs[:3], [0., 0., 0.], *inputs[4:])
    assert receipt["mode"] == "material_pin_terminal_feedback"
    assert receipt["target_pin_vertical_speed_mps"] == -1.5
    assert receipt["target_pin_clearance_m"] == pytest.approx(3.4)
    assert force == pytest.approx([force_without_aero[0]-.2*mass,
                                 force_without_aero[1]+.3*mass,
                                 force_without_aero[2]-mass])
    assert receipt["force_request_is_achieved_thrust"] is False


def test_landing_high_energy_request_is_bounded_and_retains_fuel_deficit(configuration):
    profile, catch, _ = configuration
    force, receipt = landing_force([10000., 0., 3000.], 3000., [-500., 0., -2000.],
                                   [0., 0., 0.], 270000., 20000., profile, catch)
    assert receipt["mode"] == "zem_zev_braking"
    assert receipt["force_capacity_saturated"] is True
    assert receipt["ideal_fuel_margin_kg"] < 0
    assert env.norm(force) <= profile["booster"]["gimbal_engine_count"] * profile["booster"]["engine_thrust_n"] + 1e-8
    assert force[2] > 0
    assert receipt["production_policy_admitted"] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "300000"])
def test_nonfinite_or_non_numeric_mass_is_refused(configuration, bad):
    profile, catch, _ = configuration
    with pytest.raises(ValueError):
        boostback_force([0., 0., 0.], [0., 0., 0.], bad, 9.81, profile)
    with pytest.raises(ValueError):
        landing_force([0., 0., 3.4], 3.4, [0., 0., -1.5], [0., 0., 0.], bad, 50000., profile, catch)


@pytest.mark.parametrize("bad", [[0., 0.], [True, 0., 0.], [0., 0., float("nan")]])
def test_invalid_vector_is_refused(configuration, bad):
    profile, _, booster = configuration
    with pytest.raises(ValueError):
        point_coast(bad, [0., 0., -100.], 300000., booster, profile)


def test_zero_propellant_remains_explicitly_unfunded(configuration):
    profile, catch, _ = configuration
    _, receipt = landing_force([0., 0., 30.], 30., [0., 0., -100.], [0., 0., 0.],
                               250000., 0., profile, catch)
    assert receipt["ideal_fuel_margin_kg"] < 0
    assert receipt["production_policy_admitted"] is False
