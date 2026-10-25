"""Saved-only v7 whole-record contracts; no physics, forecast or provider calls."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_state_mission_metrics as metrics
from src.runtime import starship_ascent_state_metrics as prefix
from src.runtime.starship_ascent import AscentShutdown
from src.runtime.starship_ascent_control import AscentControlRuntime
from src.runtime.starship_experimental_continuation import development_settings
from src.runtime.starship_retained_return import TRIMMED_POLICY_ID, new_record
from src.runtime.starship_state_mission import ExperimentalStateMissionAscentSettings
from tests.contract.test_starship_ascent_control_metrics import profile, ROOT
from tests.contract.test_starship_ascent_state_metrics import settings, ellipse_state


def wire(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved-only verifier invoked a control, integration, forecast or provider seam")
    from src.runtime import starship_sixdof_booster as booster
    from src.runtime import starship_return_prediction as prediction
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (env, "step"),
        (mission, "advance_plant"), (mission, "simulate_concurrent"),
        (booster, "_simulate_booster_steps"), (prediction, "forecast")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def evidence():
    p = profile()
    specification = settings()
    whole = ExperimentalStateMissionAscentSettings(specification)
    craft = mission.stack_vehicle(p, mission.vehicle(p), mission.vehicle(p, "booster"))
    altitude = dyn.mass_properties(craft, craft.propellant_capacity_kg).com_body_m[2]+p["launch"]["release_base_altitude_m"]
    point = env.surface_state(p["launch"]["latitude_deg"], p["launch"]["longitude_deg"], altitude, craft.propellant_capacity_kg)
    up, _, north = env.local_frame(point)
    q = mission._attitude(up, north)
    initial = dyn.State6DOF(0., point.r, point.v, q, dyn.inverse_rotate(q, (0., 0., env.EARTH_ROTATION_RAD_S)),
        craft.propellant_capacity_kg, tuple(dyn.EngineState(throttle=1. if "main" in engine.name else 0.)
            for engine in craft.engines), tuple(0. for _ in craft.aero_panels))
    runtime = AscentControlRuntime(specification, p)
    state, samples = initial, []
    for i in range(2):
        command, diagnostic = runtime.command(state, craft, q, 1., 33, phase="stack_ascent",
            control_interval_s=.1, control_fn=mission.control)
        samples.append(mission._sample(state, craft, "stack_ascent", command, diagnostic))
        after = replace(state, time_s=(i+1)*.1, omega_body_rad_s=(6., 0., 0.) if i == 1 else state.omega_body_rad_s)
        runtime.record_step(i, state, after, .1)
        state = after
    command, diagnostic = runtime.command(state, craft, q, 1., 33, phase="stack_ascent", control_interval_s=.1,
        control_fn=mission.control, advancing=False, terminal_reason="angular_rate_envelope_exceeded")
    samples.append(mission._sample(state, craft, "stack_ascent", command, diagnostic))
    samples[-1]["contact"] = False
    sites = json.loads((ROOT/"examples/spaceflight/starship-return-sites-model-test.json").read_bytes())
    goal = json.loads((ROOT/"examples/spaceflight/starship-splashdown-goal.json").read_bytes())
    site = next(row for row in sites["sites"] if row["site_id"] == "divert")
    metadata = {"schema": "missionos.experimental_state_mission_ascent.v1", "settings": whole.record(),
        "separation_time_s": None, "transition_witnesses": [], "shutdown_schedule": [], "shutdown_receipts": [],
        "first_cutoff_measurement": None, "cutoff_measurement": None, "insertion_vehicle": None,
        "schedule": None, "requested_absolute_duration_s": 7500., "final_mission_perigee_gate_m": 220000.,
        "engineering_objectives_not_real_flight_identification": True, "shared_production_deployment_and_return_loop": True,
        "concurrent_actual_separation": True, "qualified_return": False, "mission_authority_granted": False,
        "physical_execution": False, "state_reset": False, "all_features_completed": False, "native_model_calls": 0,
        "booster_policy": "fixed_v1", "booster_splashdown_goal": goal, "booster_return_sites": sites,
        "insertion_restart_time_s": None, "insertion_shutdown_until_s": None, "settled_measurement": None,
        "ascent_trace_steps": 2}
    record = {"scenario": "experimental_physical_mission", "initial_state": asdict(initial), "final_state": asdict(state),
        "final_vehicle": asdict(craft), "samples": samples, "events": [{"event": "angular_rate_envelope_exceeded", "time_s": .2}],
        "booster_separation_state": None, "booster_run": None, "satellites": [], "ascent_control_trace": runtime.record(),
        "development_return_qualification": development_settings(), "retained_return": new_record(TRIMMED_POLICY_ID),
        "experimental_mission_ascent": metadata, "experimental_mission_execution": {
            "schema": "missionos.experimental_state_physical_mission_execution.v1", "source_sha256": "a"*64,
            "profile_sha256": metrics.digest(p), "settings_sha256": metrics.digest(whole.record())},
        "concurrent_execution": {"schema": "missionos.ship_booster_clock.v1", "absolute_deadline_s": 7500.,
            "maximum_tick_s": .25, "maximum_observed_body_lead_s": 0., "separation_state_preserved": False,
            "observers_run_before_each_body_step": True, "posthoc_ordering": False,
            "satellite_propagation": "existing_postprocessed_orbit_only", "physical_execution": False,
            "tick_order": [{"sequence": i, "body_id": "ship", "phase": "stack_ascent", "time_s": i*.1,
                "next_time_s": (i+1)*.1} for i in range(2)]+[{"sequence": 2, "body_id": "ship", "phase": "stack_ascent",
                    "time_s": .2, "next_time_s": .2}]},
        "outcome": {"termination": "angular_rate_envelope_exceeded", "phase": "stack_ascent", "duration_s": .2,
            "integration_steps": 2, "max_altitude_m": max(row["altitude_m"] for row in samples),
            "final_altitude_m": samples[-1]["altitude_m"], "final_ground_speed_mps": samples[-1]["ground_speed_mps"],
            "six_dof_integrated": True, "attitude_prescribed": False, "starship_vehicle_validated": False,
            "orbit_gate_reached": False, "payload_released_count": 0, "booster_return_invoked": False, "contact_receipt": None}}
    return wire(record), p, dict(expected_settings=wire(whole.record()), expected_source_sha256="a"*64,
        expected_site=site, expected_goal=goal, expected_return_sites=sites, expected_duration_s=7500.)


def test_actual_negative_prefix_valid_without_physical_success_or_any_producer(evidence, monkeypatch):
    record, p, expected = evidence
    before = deepcopy(record)
    def forbidden(*args, **kwargs):
        raise AssertionError("Verifier reused the request producer")
    monkeypatch.setattr(mission, "control", forbidden)
    from src.runtime import starship_ascent as ascent
    monkeypatch.setattr(ascent.AscentController, "request", forbidden)
    result = metrics.verify_state_physical_mission(record, p, **expected)
    assert result["passed"], result
    assert result["verified_ascent_control_ticks"] == 2
    assert result["physical_success"] is result["ai_control_completed"] is result["old_certificate_admitted"] is False
    assert record == before


@pytest.mark.parametrize("path,value", [
    (("scenario",), "launch"),
    (("experimental_mission_execution", "schema"), "missionos.experimental_physical_mission_execution.v1"),
    (("experimental_mission_execution", "source_sha256"), "b"*64),
    (("experimental_mission_execution", "profile_sha256"), "b"*64),
    (("experimental_mission_execution", "settings_sha256"), "b"*64),
    (("experimental_mission_ascent", "native_model_calls"), True),
    (("experimental_mission_ascent", "qualified_return"), True),
    (("experimental_mission_ascent", "ascent_trace_steps"), 1),
    (("ascent_control_trace", "rows", 0, "state_given_to_plant", "propellant_kg"), 1.),
    (("ascent_control_trace", "rows", 1, "command", "engines", 0, "throttle"), .4),
    (("ascent_control_trace", "rows", 1, "state_returned_by_plant", "engine_states", 0, "available"), False),
    (("concurrent_execution", "tick_order", 1, "phase"), "orbital_coast"),
    (("outcome", "integration_steps"), 3),
    (("samples", 1, "mass_kg"), 1.),
    (("samples", 0, "r_eci_m", 0), 1.),
    (("outcome", "orbit_gate_reached"), True),
])
def test_source_clock_full_chain_or_authority_tamper_fails(evidence, path, value):
    record, p, expected = evidence
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = metrics.verify_state_physical_mission(record, p, **expected)
    assert not result["passed"], (path, result)


@pytest.mark.parametrize("field", metrics.FORBIDDEN)
def test_no_diagnostic_fixture_or_qualification_scope_can_be_borrowed(evidence, field):
    record, p, expected = evidence
    record[field] = {}
    assert not metrics.verify_state_physical_mission(record, p, **expected)["passed"]


def test_old_whole_verifier_does_not_accept_new_scope(evidence):
    from src.runtime.starship_experimental_mission_metrics import verify_experimental_physical_mission
    record, p, expected = evidence
    assert not verify_experimental_physical_mission(record, p, **expected)["passed"]


@pytest.mark.parametrize("kind", ["ship", "booster"])
def test_availability_schedule_never_repairs_a_declared_engine(kind):
    specification = replace(settings(), shutdowns=(AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.)))
    whole = ExperimentalStateMissionAscentSettings(specification)
    craft = mission.vehicle(profile(), kind)
    count, time = (33, 76.) if kind == "booster" else (6, 437.)
    index = 32 if kind == "booster" else 3
    sample = {"time_s": time, "engine_states": [asdict(dyn.EngineState()) for _ in craft.engines]}
    with pytest.raises(ValueError, match="availability"):
        metrics._check_availability(sample, kind, whole)
    sample["engine_states"][index]["available"] = False
    assert metrics._check_availability(sample, kind, whole)
    sample["engine_states"][count]["available"] = False
    with pytest.raises(Exception, match="RCS"):
        metrics._check_availability(sample, kind, whole)


def test_geometry_for_settled_gate_is_independent_and_rejects_hyperbolic():
    p = profile()
    state, _ = ellipse_state(p, rp_alt=230000.)
    assert prefix._orbit(wire(asdict(state)))["status"] == "bound"
    bad = replace(state, v_eci_mps=(0., 12000., 0.))
    assert prefix._orbit(wire(asdict(bad)))["status"] != "bound"


@pytest.fixture
def seam_record(monkeypatch):
    # Explicit fixture generator only. Its invented plant states are not valid
    # dynamics, so these tests isolate event/handoff arithmetic while the full
    # trace checker is exercised by evidence above and its own v7 contracts.
    from tests.contract.test_starship_state_mission import scripted_record, setup as runtime_setup
    setup = runtime_setup.__wrapped__()
    record, _, _, _ = scripted_record(setup, monkeypatch)
    record = wire(record)
    monkeypatch.setattr(prefix, "_verify_control_trace", lambda **_kwargs: 4)
    return record, setup[0], setup[1]


def check_seam(value):
    record, p, typed = value
    events = saved_events(record)
    return metrics._ascent(record, p, typed, record["experimental_mission_ascent"], events)


def saved_events(record):
    from src.runtime import starship_sixdof_verifier as saved
    return saved._events(record["events"], "$.events", 0., record["final_state"]["time_s"])


def test_direct_actual_settled_handoff_keeps_tail_and_cutoff_schedule(seam_record):
    record, _, _ = seam_record
    original = deepcopy(record)
    reached, commands = check_seam(seam_record)
    assert reached is True and commands == 4
    meta = record["experimental_mission_ascent"]
    assert meta["settled_measurement"]["time_s"] == meta["cutoff_measurement"]["time_s"]+5.
    assert meta["schedule"]["first_release_time_s"] == meta["cutoff_measurement"]["time_s"]+45.
    assert record == original


@pytest.mark.parametrize("mutation", ["settle_clock", "retime_release", "retime_return", "reset_state", "landing_outage",
    "hyperbolic", "skip_tail", "trace_length", "vehicle", "drop_handoff", "false_gate", "invented_cutoff"])
def test_settled_tail_or_schedule_tamper_rejected_independently(seam_record, mutation):
    record, _, _ = seam_record
    meta = record["experimental_mission_ascent"]
    settled = meta["settled_measurement"]
    if mutation == "settle_clock":
        settled["time_s"] += .1
    elif mutation in ("retime_release", "retime_return"):
        meta["schedule"]["first_release_time_s" if mutation == "retime_release" else "return_request_time_s"] += 5.
    elif mutation == "reset_state":
        meta["transition_witnesses"][-1]["state_after"]["propellant_kg"] += 1.
    elif mutation == "landing_outage":
        settled["state"]["engine_states"][0]["available"] = False
    elif mutation == "hyperbolic":
        settled["state"]["v_eci_mps"] = [0., 12000., 0.]
    elif mutation == "skip_tail":
        meta["insertion_shutdown_until_s"] -= 5.
    elif mutation == "trace_length":
        meta["ascent_trace_steps"] -= 1
    elif mutation == "vehicle":
        settled["vehicle"]["dry_mass_kg"] += 1.
    elif mutation == "drop_handoff":
        record["events"] = [row for row in record["events"] if row["event"] != "state_insertion_settled_handoff"]
    elif mutation == "false_gate":
        settled["gate_reached"] = False
    else:
        meta["cutoff_measurement"]["state"]["propellant_kg"] += 1.
    with pytest.raises(Exception):
        check_seam(seam_record)


def test_rejected_actual_settled_gate_is_valid_negative_without_schedule(monkeypatch):
    from tests.contract.test_starship_state_mission import scripted_record, setup as runtime_setup
    setup = runtime_setup.__wrapped__()
    record, _, _, _ = scripted_record(setup, monkeypatch, settled_bad=True)
    record = wire(record)
    monkeypatch.setattr(prefix, "_verify_control_trace", lambda **_kwargs: 4)
    reached, _ = metrics._ascent(record, setup[0], setup[1], record["experimental_mission_ascent"], saved_events(record))
    assert reached is False
    first, last, outcome, _, speed = metrics._run_state_basics(record, setup[0])
    assert first["time_s"] == 0 and last["time_s"] == outcome["duration_s"]
    assert speed is None and outcome["termination"] == "state_insertion_settled_gate_unreached"
