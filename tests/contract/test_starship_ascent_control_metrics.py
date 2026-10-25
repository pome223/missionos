"""Pure record/reference checks; fixture states are never flown."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_ascent_control_metrics as metrics
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_ascent import AscentControlOptions, AscentController, AscentCoastOrbitObjectives, AscentScreenSettings
from src.runtime.starship_ascent_control import AscentControlRuntime
from src.runtime.starship_reference_tracking import ReferenceTracker


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def forbid_physics(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("record checks cannot advance physical states")
    monkeypatch.setattr(dyn, "step", forbidden)
    monkeypatch.setattr(env, "step", forbidden)
    monkeypatch.setattr(mission, "advance_plant", forbidden)


def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


def settings(tracking=True, first_only=True):
    return AscentScreenSettings(policy="controlled_coast_orbit_v6",
        objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
        control_options=AscentControlOptions(first_only, tracking))


def fixture_record(tracking=True):
    """Two scripted states demonstrate consistency, not real flight dynamics."""
    p, specification = profile(), settings(tracking)
    ship, booster = mission.vehicle(p), mission.vehicle(p, "booster")
    craft = mission.stack_vehicle(p, ship, booster)
    altitude = dyn.mass_properties(craft, craft.propellant_capacity_kg).com_body_m[2]+p["launch"]["release_base_altitude_m"]
    base = env.surface_state(p["launch"]["latitude_deg"], p["launch"]["longitude_deg"], altitude, craft.propellant_capacity_kg)
    up, _, north = env.local_frame(base)
    q = mission._attitude(up, north)
    initial = dyn.State6DOF(0., base.r, base.v, q, (0., 0., 0.), craft.propellant_capacity_kg,
        tuple(dyn.EngineState(throttle=1. if "main" in engine.name else 0.) for engine in craft.engines),
        tuple(0. for _ in craft.aero_panels))
    runtime = AscentControlRuntime(specification, p)
    samples = []
    state = initial
    for index in range(2):
        command, diagnostic = runtime.command(state, craft, q, 1., 33, phase="stack_ascent",
            control_interval_s=.1, control_fn=mission.control)
        samples.append(mission._sample(state, craft, "stack_ascent", command, diagnostic))
        returned = replace(state, time_s=(index+1)*.1,
                           omega_body_rad_s=(6., 0., 0.) if index == 1 else (0., 0., 0.))
        runtime.record_step(index, state, returned, .1)
        state = returned
    terminal_command, terminal_diagnostic = runtime.command(state, craft, q, 1., 33, phase="stack_ascent",
        control_interval_s=.1, control_fn=mission.control, advancing=False,
        terminal_reason="angular_rate_envelope_exceeded")
    last = mission._sample(state, craft, "stack_ascent", terminal_command, terminal_diagnostic)
    last["contact"] = False
    samples.append(last)
    record = {"scenario": "launch", "initial_state": asdict(initial), "final_state": asdict(state),
        "samples": samples, "events": [{"time_s": .2, "event": "angular_rate_envelope_exceeded", "detail": "scripted pure fixture"}],
        "booster_separation_state": None, "booster_run": None, "satellites": [], "final_vehicle": asdict(craft),
        "outcome": {"termination": "angular_rate_envelope_exceeded", "phase": "stack_ascent", "duration_s": .2,
            "integration_steps": 2, "six_dof_integrated": True, "attitude_prescribed": False,
            "starship_vehicle_validated": False, "booster_return_invoked": False, "payload_released_count": 0,
            "orbit_gate_reached": False},
        "ascent_control_trace": runtime.record(),
        "ascent_screen": {"settings": specification.record(), "policy": specification.policy,
            "phase_objectives": asdict(specification.objectives), "final_mission_perigee_gate_m": 220000.,
            "phase_objectives_are_registered_engineering_inputs": True, "legacy_v3_verification_scope": False,
            "mission_dispatch": False, "return_qualified": False, "payload_release_authorized": False,
            "physical_execution": False, "propulsion_reference_connected": False, "shutdown_schedule": [],
            "shutdown_receipts": [], "separation_time_s": None, "transition_witnesses": [],
            "first_cutoff_measurement": None, "cutoff_measurement": None,
            "final_orbit": metrics._orbit(asdict(state)), "insertion_gate_reached": False}}
    return json.loads(json.dumps(record)), p, specification


@pytest.mark.parametrize("tracking", [False, True])
def test_scripted_full_trace_passes_without_reusing_any_producer(monkeypatch, tracking):
    record, p, specification = fixture_record(tracking)
    def forbidden(*args, **kwargs):
        raise AssertionError("independent checker called the command producer")
    monkeypatch.setattr(ReferenceTracker, "update", forbidden)
    monkeypatch.setattr(mission, "control", forbidden)
    result = metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)
    assert result["passed"], result
    assert result["verified_control_ticks"] == 2
    assert result["physical_reexecution"] is result["mission_authority_granted"] is False
    assert result["state_assignment_runtime_proven"] is False


@pytest.mark.parametrize("mutation", ["missing_tick", "duplicate_tick", "index", "interval", "requested_interval", "given_q", "returned_q",
    "raw_goal", "bounded_goal", "previous_rate", "rate_bound", "reference_flag", "controller_torque",
    "control_transport", "control_clip", "screen_authority", "trace_authority", "step_count", "settings",
    "fake_orbital_gate", "sample_command", "sample_mass", "nonfinite"])
def test_tampering_fails_closed(mutation):
    record, p, specification = fixture_record()
    rows = record["ascent_control_trace"]["rows"]
    row = rows[1]
    if mutation == "missing_tick":
        rows.pop()
    elif mutation == "duplicate_tick":
        rows.append(deepcopy(rows[-1]))
    elif mutation == "index":
        row["step_index"] = True
    elif mutation == "interval":
        row["interval_s"] = .2
    elif mutation == "requested_interval":
        row["requested_interval_s"] = .2
    elif mutation == "given_q":
        row["state_given_to_plant"]["q_body_to_eci"] = [1., 0., 0., 0.]
    elif mutation == "returned_q":
        rows[0]["state_returned_by_plant"]["q_body_to_eci"] = [1., 0., 0., 0.]
    elif mutation == "raw_goal":
        row["raw_goal_q_body_to_eci"] = [1., 0., 0., 0.]
    elif mutation == "bounded_goal":
        row["requested_q_body_to_eci"] = [1., 0., 0., 0.]
    elif mutation == "previous_rate":
        row["controller"]["reference_tracking"]["previous_reference_rate_eci_rad_s"][0] = .01
    elif mutation == "rate_bound":
        row["controller"]["reference_tracking"]["maximum_reference_rate_rad_s"] = 10.
    elif mutation == "reference_flag":
        row["controller"]["reference_tracking"]["actual_state_assigned"] = True
    elif mutation == "controller_torque":
        row["controller"]["requested_torque_body_nm"][0] += 100.
    elif mutation == "control_transport":
        row["controller"]["reference_tracking_control"]["transport_term_body_rad_s2"][0] = .01
    elif mutation == "control_clip":
        row["controller"]["reference_tracking_control"]["limited_requested_acceleration_body_rad_s2"][0] = .5
    elif mutation == "screen_authority":
        record["ascent_screen"]["return_qualified"] = True
    elif mutation == "trace_authority":
        record["ascent_control_trace"]["mission_authority_granted"] = True
    elif mutation == "step_count":
        record["outcome"]["integration_steps"] = True
    elif mutation == "settings":
        record["ascent_screen"]["settings"]["control_options"]["first_burn_only_bias"] = False
    elif mutation == "fake_orbital_gate":
        record["ascent_screen"]["insertion_gate_reached"] = True
    elif mutation == "sample_command":
        record["samples"][1]["command"]["engines"][0]["throttle"] = .5
    elif mutation == "sample_mass":
        record["samples"][1]["mass_kg"] += 100.
    elif mutation == "nonfinite":
        row["state_before_control"]["propellant_kg"] = math.nan
    result = metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)
    assert result["passed"] is False, mutation


def test_explicit_typed_v6_expected_settings_are_mandatory():
    record, p, specification = fixture_record()
    for wrong in (None, specification.record(), AscentScreenSettings(policy="coast_orbit_v5", objectives=specification.objectives)):
        assert metrics.verify_controlled_ascent_record(record, p, expected_settings=wrong)["passed"] is False


def test_old_v5_verifier_does_not_accept_v6_record():
    from src.runtime.starship_ascent_phase_metrics import verify_ascent_record
    record, p, specification = fixture_record()
    assert verify_ascent_record(record, profile=p, expected_settings=specification.record())["passed"] is False


def test_angular_rate_negative_has_separate_nonintegrating_untracked_terminal_command():
    record, p, specification = fixture_record()
    terminal = record["ascent_control_trace"]["terminal_commands"][0]
    assert terminal["integrated"] is terminal["reference_updated"] is False
    assert "reference_tracking" not in terminal["controller"]
    assert terminal["command"]["engines"][0]["throttle"] == 1.
    result = metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)
    assert result["passed"] and result["physical_failure"] and result["terminal_command_count"] == 1
    terminal["state"]["omega_body_rad_s"][0] = 0.
    assert metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)["passed"] is False


def test_validated_saved_reference_inputs_do_not_amplify_rounding_by_stack_inertia():
    """Tiny accepted stage-local rounding must not become a different PD input."""
    record, p, specification = fixture_record()
    row = record["ascent_control_trace"]["rows"][1]
    state = metrics._state(row["state_before_control"])
    craft = mission.stack_vehicle(p, mission.vehicle(p), mission.vehicle(p, "booster"))
    receipt = row["controller"]["reference_tracking"]
    original_acceleration = tuple(receipt["reference_acceleration_body_rad_s2"])
    # The independent quaternion/finite-difference path may differ at this
    # scale. 3e-14 rad/s² times ~5e9 kg m² exceeds the unchanged 1e-5 Nm bound.
    receipt["reference_acceleration_body_rad_s2"][1] += 3e-14
    command, diagnostic = mission.control(state, craft, row["requested_q_body_to_eci"], 1., 33, p,
        reference_rate_body_rad_s=receipt["reference_rate_body_rad_s"],
        reference_acceleration_body_rad_s=receipt["reference_acceleration_body_rad_s2"])
    diagnostic["ascent_control"] = row["controller"]["ascent_control"]
    diagnostic["reference_tracking"] = receipt
    row["command"], row["controller"] = asdict(command), diagnostic
    sample = next(sample for sample in record["samples"] if sample["time_s"] == row["time_s"])
    sample["command"], sample["controller"] = deepcopy(row["command"]), deepcopy(row["controller"])
    record = json.loads(json.dumps(record))
    row = record["ascent_control_trace"]["rows"][1]
    # Reconstructing a slightly different downstream input reproduces the old
    # failure without running or changing either controller or plant physics.
    with pytest.raises(ValueError, match="requested_control_torque_mismatch"):
        metrics._check_control(row, craft, p, receipt["reference_rate_body_rad_s"], original_acceleration)
    result = metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)
    assert result["passed"], result
    # A real inconsistent numerical input is still refused by the independent
    # reference relation; the correction is not a new permissive torque gate.
    row["controller"]["reference_tracking"]["reference_acceleration_body_rad_s2"][1] += .001
    assert metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)["passed"] is False


def test_production_generator_fake_steps_cover_split_first_cutoff_coast_and_reserve_stop(monkeypatch):
    """Exercise actual orchestration, replacing every dynamics advance."""
    p = profile()
    specification = AscentScreenSettings(-10., -10., policy="controlled_coast_orbit_v6",
        objectives=settings().objectives, control_options=AscentControlOptions(True, True))
    calls = []
    def fake_advance(state, craft, command, interval, *unused):
        calls.append((state.time_s, interval, len(craft.engines)))
        if len(calls) == 1:
            return replace(state, time_s=state.time_s+interval,
                           propellant_kg=p["booster"]["separation_reserve_kg"]), None
        if len(calls) == 2:
            rp, ra = env.EARTH_EQUATORIAL_RADIUS_M-19000., env.EARTH_EQUATORIAL_RADIUS_M+301000.
            radius = env.EARTH_EQUATORIAL_RADIUS_M+200000.
            semimajor = (rp+ra)/2
            momentum2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            tangent = math.sqrt(momentum2)/radius
            radial = math.sqrt(env.EARTH_MU_M3_S2*(2/radius-1/semimajor)-momentum2/radius**2)
            return replace(state, time_s=state.time_s+interval, r_eci_m=(radius, 0., 0.),
                           v_eci_mps=(radial, tangent, 0.), propellant_kg=100000.), None
        if len(calls) == 3:
            return replace(state, time_s=state.time_s+interval, propellant_kg=74000.), None
        raise AssertionError("unexpected extra simulated plant step")
    monkeypatch.setattr(mission, "advance_plant", fake_advance)
    record = mission.simulate_ascent_screen(p, stack_pitch_offset_deg=-10., ship_pitch_offset_deg=-10.,
        policy=specification.policy, objectives=specification.objectives, control_options=specification.control_options)
    record = json.loads(json.dumps(record))
    assert calls == [(0., .1, 45), (.1, .1, 18), (.2, .25, 18)]
    assert record["outcome"]["termination"] == "ascent_reserve_exhausted"
    assert record["ascent_screen"]["first_cutoff_measurement"]["time_s"] == .2
    result = metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)
    assert result["passed"], result
    assert result["verified_control_ticks"] == 3 and result["terminal_command_count"] == 1
    record["ascent_control_trace"]["rows"][1]["state_before_control"]["q_body_to_eci"] = [1., 0., 0., 0.]
    assert metrics.verify_controlled_ascent_record(record, p, expected_settings=specification)["passed"] is False


def ship_request_row(*, time=1516., phase="suborbital_coast", fuel=200000., first_only=True, bias=-10., circular=False):
    p = profile()
    specification = AscentScreenSettings(-10., bias, policy="controlled_coast_orbit_v6",
        objectives=settings().objectives, control_options=AscentControlOptions(first_only, False))
    craft = mission.vehicle(p)
    radius = env.EARTH_EQUATORIAL_RADIUS_M+275000.
    speed = math.sqrt(env.EARTH_MU_M3_S2/radius) if circular else 4000.
    state = dyn.State6DOF(time, (radius, 0., 0.), (0., speed, 0.), (1., 0., 0., 0.), (0., 0., 0.), fuel,
        tuple(dyn.EngineState() for _ in craft.engines), tuple(0. for _ in craft.aero_panels))
    observation = dyn.observe(state, craft)
    point = env.State3D(time, state.r_eci_m, state.v_eci_mps, fuel)
    up, east, north = env.local_frame(point)
    radial = env.dot(state.v_eci_mps, up)
    tangent = env.add(state.v_eci_mps, env.scale(up, -radial))
    controller = AscentController(specification)
    controller.separated(135.)
    request = controller.request(state, craft, p, phase, observation, up, east, north, radial,
        tangent, env.unit(tangent), env.orbital_elements(point), mission._attitude)
    command, diagnostic = mission.control(state, craft, request.target, request.throttle, request.count, p)
    return {"phase": phase, "time_s": time, "state_before_control": json.loads(json.dumps(asdict(state))),
            "raw_goal_q_body_to_eci": [float(value) for value in request.target], "command": json.loads(json.dumps(asdict(command))),
            "controller": diagnostic}, craft, p, specification


def test_coast_command_is_valid_before_all_three_guards():
    row, craft, p, specification = ship_request_row()
    metrics._raw_request(row, craft, p, specification, 135.)


@pytest.mark.parametrize("change", [{"time": 1517.}, {"fuel": 75000.}, {"circular": True}])
def test_coast_cannot_ignore_restart_final_orbit_or_reserve(change):
    row, craft, p, specification = ship_request_row(**change)
    with pytest.raises(ValueError, match="coast_ignored"):
        metrics._raw_request(row, craft, p, specification, 135.)


def test_second_burn_bias_is_independently_rejected_when_first_burn_only_registered():
    correct, craft, p, specification = ship_request_row(time=1517., phase="orbit_insertion")
    metrics._raw_request(correct, craft, p, specification, 135.)
    old_bias, _, _, _ = ship_request_row(time=1517., phase="orbit_insertion", first_only=False)
    correct["raw_goal_q_body_to_eci"] = old_bias["raw_goal_q_body_to_eci"]
    with pytest.raises(ValueError, match="raw_guidance_axis"):
        metrics._raw_request(correct, craft, p, specification, 135.)


@pytest.mark.parametrize("axis", [(1., 0., 0.), (0., 1., 0.), (1., 2., 3.)])
def test_independent_reference_math_checks_nonlinear_reversal_and_frame_transport(axis):
    p = profile()
    tracker = ReferenceTracker(p, (1., 0., 0., 0.), 0.)
    history = ((1., 0., 0., 0.), 0., (0., 0., 0.), (1., 0., 0., 0.))
    length = math.hypot(*axis)
    actual = (math.cos(.2), 0., 0., math.sin(.2))
    for index in range(21):
        angle = 1.5 if index < 11 else -1.5
        goal = (math.cos(angle/2), *(x*math.sin(angle/2)/length for x in axis))
        requested, details = tracker.update(goal, actual, (.01, .02, .03), index*.1)
        row = {"step_index": index, "time_s": index*.1,
            "state_before_control": {"q_body_to_eci": actual, "omega_body_rad_s": (.01, .02, .03)},
            "raw_goal_q_body_to_eci": goal, "requested_q_body_to_eci": requested,
            "controller": {"reference_tracking": json.loads(json.dumps(details["receipt"]))}}
        history, _, _ = metrics._check_reference(row, history, p)


def reference_fixture(tmp_path):
    paths = [
        metrics.REFERENCE_PATH, metrics.ORIGINAL_CSV,
        "docs/assets/starship-reference-v3-20261018/ascent_stack_and_ship.csv",
        "docs/assets/starship-reference-v2-20261018/reference.json",
        "docs/assets/starship-reference-v2-20261018/acceptance.json",
        "docs/assets/starship-reference-v1-20261018/data-quality.json",
        "docs/assets/starship-system-repair-20261018/speed-reference-review-v2-manual.csv"]
    for relative in paths:
        path = tmp_path/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT/relative).read_bytes())
    registration = json.loads((ROOT/"examples/spaceflight/starship-system-repair-ascent-v3.json").read_text())
    registration["fit_data"]["file"] = paths[2]
    registration["input_sha256"] = {path: hashlib.sha256((tmp_path/path).read_bytes()).hexdigest() for path in paths}
    registration["source_reference"] = {"path": metrics.REFERENCE_PATH,
        "sha256": registration["input_sha256"][metrics.REFERENCE_PATH]}
    return registration


def test_new_reference_keeps_masks_and_uses_manual_visible_clock(tmp_path):
    registration = reference_fixture(tmp_path)
    reference = metrics.load_reference(tmp_path, registration)
    assert reference["counts"] == {"altitude_points": 145, "speed_points": 442, "altitude_bins": 36, "speed_bins": 96}
    values = {row["time_s"]: row["observed"] for row in reference["speed"]}
    assert values[80.] == pytest.approx(2230/3.6)
    assert values[83.] == pytest.approx(2388/3.6)
    assert len(reference["manual_speed_overrides"]) == 4


@pytest.mark.parametrize("mutation", ["csv_hash", "extra_cell", "wrong_manual_clock", "wrong_manifest_clock", "quarantine", "unselected"])
def test_reference_tampering_cannot_change_masks_or_cells(tmp_path, mutation):
    registration = reference_fixture(tmp_path)
    if mutation == "csv_hash":
        path = registration["fit_data"]["file"]
        (tmp_path/path).write_text((tmp_path/path).read_text()+"\n")
    elif mutation == "extra_cell":
        path = registration["fit_data"]["file"]
        content = (tmp_path/path).read_text().replace("2230", "2231", 1)
        (tmp_path/path).write_text(content)
        digest = hashlib.sha256((tmp_path/path).read_bytes()).hexdigest()
        registration["input_sha256"][path] = digest
        manifest = json.loads((tmp_path/metrics.REFERENCE_PATH).read_text())
        manifest["ascent_csv"]["sha256"] = digest
        (tmp_path/metrics.REFERENCE_PATH).write_text(json.dumps(manifest))
    elif mutation == "wrong_manual_clock":
        path = "docs/assets/starship-system-repair-20261018/speed-reference-review-v2-manual.csv"
        (tmp_path/path).write_text((tmp_path/path).read_text().replace("V2-14,81,80,", "V2-14,81,81,"))
        registration["input_sha256"][path] = hashlib.sha256((tmp_path/path).read_bytes()).hexdigest()
    elif mutation == "wrong_manifest_clock":
        manifest = json.loads((tmp_path/metrics.REFERENCE_PATH).read_text())
        manifest["field_overrides"][0]["t_plus_s"] = 81
        (tmp_path/metrics.REFERENCE_PATH).write_text(json.dumps(manifest))
    elif mutation == "quarantine":
        registration["fit_data"]["quarantine"]["source_lines"].pop()
    else:
        registration["source_reference"]["path"] = "docs/assets/starship-reference-v2-20261018/reference.json"
    if mutation in ("extra_cell", "wrong_manifest_clock"):
        digest = hashlib.sha256((tmp_path/metrics.REFERENCE_PATH).read_bytes()).hexdigest()
        registration["input_sha256"][metrics.REFERENCE_PATH] = digest
        registration["source_reference"]["sha256"] = digest
    with pytest.raises(ValueError):
        metrics.load_reference(tmp_path, registration)
