"""Independent v7 saved-record checks with scripted states, never real flights."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math

import pytest

from src.runtime import starship_ascent as ascent
from src.runtime import starship_ascent_state_metrics as metrics
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_reference_tracking import ReferenceTracker
from tests.contract.test_starship_ascent_control_metrics import fixture_record, profile


def json_value(value):
    return json.loads(json.dumps(value))


def settings(tracking=False):
    return ascent.AscentScreenSettings(policy="state_insertion_v7",
        objectives=ascent.AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
        control_options=ascent.AscentControlOptions(True, tracking),
        insertion_options=ascent.StateInsertionOptions())


@pytest.fixture(autouse=True)
def forbid_propagation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("saved record checks cannot propagate a trajectory")
    monkeypatch.setattr(dyn, "step", forbidden)
    monkeypatch.setattr(dyn, "_rhs", forbidden)
    monkeypatch.setattr(env, "step", forbidden)
    monkeypatch.setattr(mission, "advance_plant", forbidden)


def early_record(tracking=False):
    record, p, _ = fixture_record(tracking)
    specification = settings(tracking)
    record["ascent_screen"].update(settings=specification.record(), policy=specification.policy,
        insertion_restart_time_s=None, insertion_shutdown_until_s=None, registered_duration_s=4100.)
    record["ascent_control_trace"]["settings"] = specification.record()
    return record, p, specification


def forbid_producers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("independent verifier called a request producer")
    monkeypatch.setattr(ascent.AscentController, "request", forbidden)
    monkeypatch.setattr(ascent, "state_insertion_reference", forbidden)
    monkeypatch.setattr(ReferenceTracker, "update", forbidden)
    monkeypatch.setattr(mission, "control", forbidden)


@pytest.mark.parametrize("tracking", [False, True])
def test_independent_negative_record_checks_without_producers(monkeypatch, tracking):
    record, p, specification = early_record(tracking)
    before = deepcopy(record)
    forbid_producers(monkeypatch)
    result = metrics.verify_state_insertion_record(record, p, expected_settings=specification)
    assert result["passed"], result
    assert result["physical_failure"] and result["verified_control_ticks"] == 2
    assert result["physical_reexecution"] is result["mission_authority_granted"] is False
    assert record == before


@pytest.mark.parametrize("mutation", ["tick", "interval", "assigned_q", "controller_torque", "sample_mass",
    "authority", "horizon", "restart_claim", "settled_claim", "changed_settings", "nonfinite"])
def test_tampered_v7_records_fail_closed(mutation):
    record, p, specification = early_record()
    row = record["ascent_control_trace"]["rows"][1]
    if mutation == "tick":
        row["step_index"] = True
    elif mutation == "interval":
        row["requested_interval_s"] = .2
    elif mutation == "assigned_q":
        row["state_given_to_plant"]["q_body_to_eci"] = [1., 0., 0., 0.]
    elif mutation == "controller_torque":
        row["controller"]["requested_torque_body_nm"][0] += 100.
    elif mutation == "sample_mass":
        record["samples"][0]["mass_kg"] += 1.
    elif mutation == "authority":
        record["ascent_screen"]["return_qualified"] = True
    elif mutation == "horizon":
        record["ascent_screen"]["registered_duration_s"] = 1537.
    elif mutation == "restart_claim":
        record["ascent_screen"]["insertion_restart_time_s"] = .1
    elif mutation == "settled_claim":
        record["ascent_screen"]["insertion_shutdown_until_s"] = .2
    elif mutation == "changed_settings":
        record["ascent_control_trace"]["settings"]["insertion_options"]["maximum_restart_time_s"] = 3900.
    else:
        row["state_before_control"]["propellant_kg"] = math.nan
    assert metrics.verify_state_insertion_record(record, p, expected_settings=specification)["passed"] is False


def test_v7_settings_explicit_and_old_verifier_remains_closed():
    from src.runtime.starship_ascent_control_metrics import verify_controlled_ascent_record
    record, p, specification = early_record()
    assert verify_controlled_ascent_record(record, p, expected_settings=specification)["passed"] is False
    for wrong in (None, specification.record(), replace(specification, policy="controlled_coast_orbit_v6", insertion_options=None)):
        assert metrics.verify_state_insertion_record(record, p, expected_settings=wrong)["passed"] is False


def ellipse_state(p, *, radial=0., rp_alt=150000., ra_alt=350000., rate=0., nose_offset=0.):
    craft = mission.vehicle(p)
    rp, ra = env.EARTH_EQUATORIAL_RADIUS_M+rp_alt, env.EARTH_EQUATORIAL_RADIUS_M+ra_alt
    vt = math.sqrt(2*env.EARTH_MU_M3_S2*rp/(ra*(rp+ra)))
    tangent = (0., math.cos(nose_offset), math.sin(nose_offset))
    q = mission._attitude(tangent, (0., 0., 1.))
    state = dyn.State6DOF(2000., (ra, 0., 0.), (radial, vt, 0.), q, (rate, 0., 0.), 100000.,
        tuple(dyn.EngineState() for _ in craft.engines), tuple(0. for _ in craft.aero_panels))
    return state, craft


def test_geometry_discriminates_apo_from_peri_and_outbound_without_producer():
    p, specification = profile(), settings()
    apo, _ = ellipse_state(p)
    assert metrics._geometry(json_value(asdict(apo)), specification)["ready"]
    assert not metrics._geometry(json_value(asdict(replace(apo, v_eci_mps=(1., apo.v_eci_mps[1], 0.)))), specification)["ready"]
    assert not metrics._geometry(json_value(asdict(replace(apo, omega_body_rad_s=(.03, 0., 0.)))), specification)["ready"]
    bad_attitude, _ = ellipse_state(p, nose_offset=math.radians(6.))
    assert not metrics._geometry(json_value(asdict(bad_attitude)), specification)["ready"]
    rp, ra = env.EARTH_EQUATORIAL_RADIUS_M+150000., env.EARTH_EQUATORIAL_RADIUS_M+350000.
    vt_peri = math.sqrt(2*env.EARTH_MU_M3_S2*ra/(rp*(rp+ra)))
    peri = replace(apo, r_eci_m=(rp, 0., 0.), v_eci_mps=(0., vt_peri, 0.))
    assert not metrics._geometry(json_value(asdict(peri)), specification)["ready"]


def request_row(phase="orbit_insertion", *, radial=0., time=2000., fuel=100000.):
    p, specification = profile(), settings()
    state, craft = ellipse_state(p, radial=radial)
    state = replace(state, time_s=time, propellant_kg=fuel)
    _, _, north = env.local_frame(env.State3D(time, state.r_eci_m, state.v_eci_mps, fuel))
    target = mission._attitude((0., 1., 0.), north)
    throttle = craft.engines[0].min_throttle if phase == "orbit_insertion" else 0.
    command, controller = mission.control(state, craft, target, throttle, int(throttle > 0), p)
    controller["state_insertion_reference"] = ascent.state_insertion_reference(state, craft, specification.objectives,
                                                                             specification.insertion_options)
    return json_value({"phase": phase, "time_s": time, "state_before_control": asdict(state),
        "raw_goal_q_body_to_eci": target, "command": asdict(command), "controller": controller}), craft, p, specification


def test_fixed_low_throttle_and_tangent_without_altitude_servo(monkeypatch):
    row, craft, p, specification = request_row()
    forbid_producers(monkeypatch)
    metrics._raw_request(row, craft, p, specification, 135.)
    row["command"]["engines"][0]["throttle"] = 1.
    with pytest.raises(ValueError, match="main_command_differs"):
        metrics._raw_request(row, craft, p, specification, 135.)


@pytest.mark.parametrize("mutation", ["axis", "diagnostic", "near_flag", "force_flag"])
def test_new_request_reference_tampering_is_independently_rejected(mutation):
    row, craft, p, specification = request_row()
    if mutation == "axis":
        row["raw_goal_q_body_to_eci"] = dyn.IDENTITY
    elif mutation == "diagnostic":
        row["controller"]["state_insertion_reference"]["radial_velocity_mps"] = 1.
    elif mutation == "near_flag":
        row["controller"]["state_insertion_reference"]["near_apoapsis_window"] = False
    else:
        row["controller"]["state_insertion_reference"]["actual_state_assigned"] = True
    with pytest.raises(ValueError):
        metrics._raw_request(row, craft, p, specification, 135.)


@pytest.mark.parametrize("change", [{}, {"time": 4000.}, {"fuel": 75000.}])
def test_coast_cannot_ignore_ready_window_deadline_or_reserve(change):
    row, craft, p, specification = request_row(phase="suborbital_coast", **change)
    with pytest.raises(ValueError):
        metrics._raw_request(row, craft, p, specification, 135.)


def test_returned_actuator_slew_limit_is_independent_of_maximum_angle():
    p = profile()
    first, craft = ellipse_state(p)
    statuses = list(first.engine_states)
    statuses[0] = replace(statuses[0], throttle=.3)
    after = replace(first, time_s=first.time_s+.1, engine_states=tuple(statuses))
    with pytest.raises(ValueError, match="actual_throttle_rate"):
        metrics._actuator_step_limits(first, after, craft, .1)


@pytest.mark.parametrize("becomes_unbound", [False, True])
def test_scripted_generator_checks_restart_cutoff_and_finite_shutdown_without_real_steps(monkeypatch, becomes_unbound):
    """Fixture state changes establish wiring only; they are not valid dynamics."""
    p, specification, calls = profile(), settings(), []
    def fake_advance(state, craft, command, interval, *unused):
        calls.append((state.time_s, interval))
        if len(calls) == 1:
            return replace(state, time_s=state.time_s+interval,
                           propellant_kg=p["booster"]["separation_reserve_kg"]), None
        if len(calls) == 2:
            rp, ra = env.EARTH_EQUATORIAL_RADIUS_M-19000., env.EARTH_EQUATORIAL_RADIUS_M+301000.
            radius = env.EARTH_EQUATORIAL_RADIUS_M+200000.
            h2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            vr = math.sqrt(env.EARTH_MU_M3_S2*(2/radius-2/(rp+ra))-h2/radius**2)
            return replace(state, time_s=state.time_s+interval, r_eci_m=(radius, 0., 0.),
                v_eci_mps=(vr, math.sqrt(h2)/radius, 0.), propellant_kg=100000.), None
        if len(calls) == 3:
            apo, _ = ellipse_state(p)
            return replace(apo, time_s=state.time_s+interval), None
        if len(calls) == 4:
            radius = env.norm(state.r_eci_m)
            return replace(state, time_s=state.time_s+interval,
                           v_eci_mps=(0., 12000. if becomes_unbound else math.sqrt(env.EARTH_MU_M3_S2/radius), 0.)), None
        return replace(state, time_s=state.time_s+interval), None
    monkeypatch.setattr(mission, "advance_plant", fake_advance)
    record = mission.simulate_ascent_screen(p, duration_s=4100., policy=specification.policy,
        objectives=specification.objectives, control_options=specification.control_options,
        insertion_options=specification.insertion_options)
    record = json.loads(json.dumps(record))
    if becomes_unbound:
        assert record["outcome"]["termination"] == "state_insertion_unavailable"
        assert record["ascent_screen"]["cutoff_measurement"] is None
        forbid_producers(monkeypatch)
        result = metrics.verify_state_insertion_record(record, p, expected_settings=specification)
        assert result["passed"] and result["physical_failure"], result
        assert result["measured_final_insertion_gate_reached"] is False
        assert result["measured_settled_insertion_gate_reached"] is False
        return
    assert record["outcome"]["termination"] == "insertion_gate_reached"
    assert record["ascent_screen"]["insertion_restart_time_s"] == pytest.approx(.45)
    cutoff = record["ascent_screen"]["cutoff_measurement"]["time_s"]
    assert record["final_state"]["time_s"] == cutoff+5.
    forbid_producers(monkeypatch)
    result = metrics.verify_state_insertion_record(record, p, expected_settings=specification)
    assert result["passed"], result
    assert result["measured_final_insertion_gate_reached"]
    assert result["measured_settled_insertion_gate_reached"]
    record["ascent_screen"]["insertion_shutdown_until_s"] -= .25
    assert not metrics.verify_state_insertion_record(record, p, expected_settings=specification)["passed"]


def test_powered_unbound_saved_state_rejected_without_reusing_request_producer(monkeypatch):
    row, craft, p, specification = request_row()
    unbound = replace(metrics._state(row["state_before_control"]), v_eci_mps=(0., 12000., 0.))
    row["state_before_control"] = json_value(asdict(unbound))
    row["controller"]["state_insertion_reference"] = ascent.state_insertion_reference(
        unbound, craft, specification.objectives, specification.insertion_options)
    forbid_producers(monkeypatch)
    with pytest.raises(ValueError, match="state_insertion_powered_or_coasting_on_unbound_orbit"):
        metrics._raw_request(row, craft, p, specification, 135.)
