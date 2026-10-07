"""Mission-level invariants for the finite-force public-inspired 3-D surrogate."""

from dataclasses import replace
import math

import pytest

from src.runtime.starship_flight import FlightProfile, simulate_flight
from src.runtime.starship_physics import State3D, Vehicle, Control, step, orbital_elements


@pytest.fixture(scope="module")
def flight():
    return simulate_flight()


def _state(record):
    return State3D(record["time_s"], record["r"], record["v"], record["propellant_kg"])


def _events(run, name):
    return [event for event in run["events"] if event["event"] == name]


def test_orbit_release_and_earth_return_are_measured_separate_outcomes(flight):
    result = flight["outcomes"]
    assert result["stage_separation_observed"]
    assert result["orbit_insertion_completed"]
    assert result["satellites_released"] == 26
    assert result["ship_surface_contact"]
    assert result["booster_surface_contact"]
    for name in (
        "mission_completed",
        "real_flight_validated",
        "tps_survival_validated",
        "satellite_service_verified",
        "satellite_communications_verified",
        "ship_survival_verified",
        "ship_return_zone_verified",
        "booster_catch_verified",
    ):
        assert result[name] is False
    assert len(flight["traces"]["satellites"]) == 26
    assert len(_events(flight, "satellite_released")) == 26
    assert flight["integration"]["attitude_model"].startswith("prescribed")
    # A rotating planet with a non-equatorial launch cannot stay in the old xy plane.
    assert max(point["z_m"] for point in flight["traces"]["ship"]) > 1_000_000
    assert min(point["z_m"] for point in flight["traces"]["ship"]) < -1_000_000


def test_selected_steps_independently_replay_and_burn_fuel(flight):
    for body in ("ship", "booster"):
        records = flight["step_records"][body]
        phases = {}
        for record in records:
            phases.setdefault(record["phase"], record)
        for record in [*phases.values(), records[-1]]:
            before = _state(record["before"])
            after = step(
                before, Vehicle(**record["vehicle"]), Control(**record["control"]), record["dt_s"]
            )
            expected = _state(record["after"])
            assert after.time_s == pytest.approx(expected.time_s, abs=1e-9)
            assert after.r == pytest.approx(expected.r, abs=1e-7)
            assert after.v == pytest.approx(expected.v, abs=1e-9)
            assert after.propellant_kg == pytest.approx(expected.propellant_kg, abs=1e-8)
            assert expected.propellant_kg <= before.propellant_kg
        assert any(
            record["before"]["propellant_kg"] > record["after"]["propellant_kg"]
            for record in records
        )


def test_no_instantaneous_state_change_without_separation_receipt(flight):
    transitions = flight["state_transitions"]
    assert len(transitions) == 27
    for body, records in flight["step_records"].items():
        for first, second in zip(records, records[1:]):
            assert first["after"]["time_s"] == second["before"]["time_s"]
            if first["after"] != second["before"]:
                matches = [
                    item
                    for item in transitions
                    if item["body_id"] == body
                    and item["before"] == first["after"]
                    and item["after"] == second["before"]
                ]
                assert len(matches) == 1
    stage = _events(flight, "hot_stage_separation")[0]
    stack_state = stage["stack_before"]
    ship_state = stage["state"]
    booster_state = stage["booster_state"]
    assert ship_state["r"] == booster_state["r"] == stack_state["r"]
    assert ship_state["v"] == booster_state["v"] == stack_state["v"]
    p = flight["profile"]
    stack_mass = (
        p["booster_dry_mass_kg"]
        + p["ship_dry_mass_kg"]
        + p["ship_propellant_kg"]
        + p["satellite_mass_kg"] * p["satellite_count"]
        + stack_state["propellant_kg"]
    )
    separated_mass = (
        stage["booster_dry_mass_kg"]
        + booster_state["propellant_kg"]
        + stage["ship_dry_mass_kg"]
        + ship_state["propellant_kg"]
    )
    assert stack_mass == pytest.approx(separated_mass, abs=1e-8)


def test_release_impulses_conserve_mass_and_linear_momentum(flight):
    for receipt in flight["release_receipts"]:
        ship_before = receipt["ship_before"]
        ship_after = receipt["ship_after"]
        satellite = receipt["satellite_state"]
        before_mass = receipt["ship_mass_before_kg"]
        after_mass = receipt["ship_mass_after_kg"]
        satellite_mass = receipt["satellite_mass_kg"]
        assert before_mass == pytest.approx(after_mass + satellite_mass, abs=1e-8)
        assert ship_before["r"] == ship_after["r"] == satellite["r"]
        for i in range(3):
            assert before_mass * ship_before["v"][i] == pytest.approx(
                after_mass * ship_after["v"][i] + satellite_mass * satellite["v"][i], abs=1e-6
            )
        assert satellite["propellant_kg"] == 0
        trace = flight["traces"]["satellites"][receipt["satellite_id"]]
        assert trace[0]["time_s"] == receipt["time_s"]
        assert trace[-1]["time_s"] == flight["traces"]["ship"][-1]["time_s"]
        assert (
            orbital_elements(_state(satellite))["perigee_altitude_m"]
            >= flight["profile"]["minimum_deploy_perigee_m"]
        )
        assert receipt["communications_verified"] is False
        assert receipt["service_verified"] is False


def test_finite_burn_events_stop_at_measured_orbit_thresholds(flight):
    for event_name, key in (
        ("ship_engine_cutoff", "suborbital_perigee_target_m"),
        ("orbit_insertion_cutoff", "orbit_insertion_perigee_m"),
        ("deorbit_cutoff", "deorbit_perigee_m"),
    ):
        event = _events(flight, event_name)[0]
        measured = orbital_elements(_state(event["state"]))["perigee_altitude_m"]
        assert measured == pytest.approx(flight["profile"][key], abs=0.001)
    engine_out = _events(flight, "rvac_engine_out")[0]
    assert engine_out["engine_type"] == "RVac"
    assert engine_out["engine_count_after"] == 5
    assert _events(flight, "orbit_insertion_ignition")[0]["engine_count"] == 1
    booster_fault = _events(flight, "booster_ascent_engine_out")[0]
    assert booster_fault["engine_count_after"] == 32
    assert booster_fault["time_s"] == flight["profile"]["booster_engine_failure_time_s"]


def test_health_no_go_coasts_to_entry_and_never_releases():
    run = simulate_flight(scenario="orbit_no_go")
    assert run["orbit_gate"]["decision"] == "orbit_no_go"
    assert run["orbit_gate"]["orbit_insertion_navigation_health_ok"] is False
    assert run["orbit_gate"]["sea_level_engine_health_ok"] is True
    assert not run["outcomes"]["orbit_insertion_attempted"]
    assert run["outcomes"]["satellites_released"] == 0
    assert not run["release_receipts"]
    assert run["outcomes"]["entry_interface_observed"]
    assert run["outcomes"]["ship_surface_contact"]


def test_dispenser_jam_preserves_partial_release_and_inhibits_rest():
    run = simulate_flight(scenario="dispenser_jam")
    assert run["outcomes"]["satellites_released"] == 7
    assert run["outcomes"]["payload_release_count_complete"] is False
    stopped = _events(run, "deployment_inhibited")[0]
    assert stopped["reason"] == "synthetic_dispenser_jam"
    assert all(receipt["time_s"] < stopped["time_s"] for receipt in run["release_receipts"])


def test_landing_engine_failure_keeps_impact_velocity_and_no_propulsive_landing():
    run = simulate_flight(scenario="landing_engine_failure")
    assert run["outcomes"]["ship_surface_contact"]
    assert run["outcomes"]["ship_contact_ground_speed_mps"] > 100
    assert not run["outcomes"]["ship_soft_contact_candidate"]
    terminal = [
        record for record in run["step_records"]["ship"] if record["phase"] == "terminal_flip_burn"
    ]
    assert terminal
    assert all(record["control"]["throttle"] == 0 for record in terminal)
    assert terminal[0]["before"]["propellant_kg"] == terminal[-1]["after"]["propellant_kg"]


def test_duration_budget_stops_without_inventing_downstream_events():
    run = simulate_flight(replace(FlightProfile(), max_mission_duration_s=12.0))
    assert run["outcomes"]["duration_limit_reached"]
    assert not run["outcomes"]["stage_separation_observed"]
    assert not run["outcomes"]["ship_surface_contact"]
    assert run["outcomes"]["satellites_released"] == 0
    assert run["traces"]["ship"][-1]["time_s"] == 12.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dt_s": 0},
        {"dt_s": float("nan")},
        {"dt_s": True},
        {"sample_interval_s": 0.1},
        {"scenario": "real_spacecraft"},
    ],
)
def test_invalid_execution_parameters_rejected(kwargs):
    with pytest.raises(ValueError):
        simulate_flight(**kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"ship_dry_mass_kg": -1},
        {"satellite_count": True},
        {"satellite_mass_kg": math.nan},
        {"ship_return_reserve_kg": 2_000_000},
        {"entry_bank_deg": 190},
        {"terminal_descent_time_constant_s": 0},
        {"ascent_altitude_response_s": 0},
    ],
)
def test_invalid_profile_rejected(changes):
    with pytest.raises(ValueError):
        simulate_flight(replace(FlightProfile(), **changes))


def test_counterfactual_ascent_has_all_engines_and_separate_label():
    run = simulate_flight(
        replace(FlightProfile(), max_mission_duration_s=700.0), scenario="counterfactual_nominal"
    )
    assert not _events(run, "booster_ascent_engine_out")
    assert not _events(run, "rvac_engine_out")
    assert run["outcomes"]["orbit_insertion_completed"]
    assert all(
        record["engine_count"] == 33
        for record in run["step_records"]["ship"]
        if record["phase"] == "stack_ascent"
    )
    assert all(
        record["engine_count"] == 6
        for record in run["step_records"]["ship"]
        if record["phase"] == "ship_ascent"
    )
