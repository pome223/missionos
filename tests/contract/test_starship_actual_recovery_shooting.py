"""Public short resume smoke and synthetic persistence/budget contracts.

Stubbed full forecasts below test bookkeeping only; they are not flight evidence.
The actual finite-plant smoke advances only .1-second macrosteps.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import time

import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import configuration as catch_configuration
from src.runtime.starship_booster_recovery import _tower_observation
from src.runtime.starship_constrained_recovery import CONFIG as DRIVER_CONFIG
from src.runtime.starship_reference_tracking import ReferenceTracker
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def fixture(altitude=30000.):
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = catch_configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], altitude, time_s=100.)
    up, east, _ = env.local_frame(point)
    quaternion = tuple(float(x) for x in _attitude(up, east))
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.scale(up, -50.)), quaternion,
        (0., 0., 0.), 78000., tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    reference = ConditionedGeographicFrame(quaternion, 100., maximum_roll_rate_rad_s=.03/.28)
    plan = {"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., -50.],
        "bank_heading_enu": [1., 0., 0.], "bank_sign": -1, "prediction_is_execution": False}
    snapshot = shooting.capture_context(state, profile, catch, DRIVER_CONFIG,
        phase="recovery_entry_coast", start_time_s=100., deadline_s=1300., plan=plan, refreshed=True,
        burn_start_s=None, settle_start_s=None, previous_entry_axis_enu=[0., 0., 1.],
        entry_pretrim_prepared_at_s=None, next_preview_s=100., braking_preview=None,
        prior_command_reference={"quaternion": list(quaternion), "time_s": 100.},
        conditioned_reference=reference, reference_tracker=None)
    return profile, catch, body, state, snapshot


def request(snapshot, duration, steps=6500):
    return {"origin_context_sha256": shooting.digest(snapshot), "duration_s": duration,
        "maximum_integration_steps": steps, "wall_deadline_monotonic_s": time.monotonic()+60.}


def memory_sink(records):
    def sink(identifier, payload):
        assert identifier not in records
        records[identifier] = deepcopy(payload)
        digest = shooting.digest(payload)
        return {"artifact_id": identifier, "format": "json", "relative_path": identifier+".json",
            "sha256": digest, "raw_json_sha256": digest,
            "bytes": len(json.dumps(payload)), "persisted_before_analysis": True}
    return sink


def test_actual_public_short_continuation_preserves_state_and_all_control_history_across_resume():
    profile, catch, _, _, snapshot = fixture()
    original = deepcopy(snapshot)
    uninterrupted = shooting.forecast_constrained_continuation(snapshot, profile, catch,
        snapshot["context"]["plan"], request(snapshot, .4))
    prefix = shooting.forecast_constrained_continuation(snapshot, profile, catch,
        snapshot["context"]["plan"], request(snapshot, .2))
    resumed = prefix["final_controller_context"]
    remaining = 100.4-resumed["state"]["time_s"]
    suffix = shooting.forecast_constrained_continuation(resumed, profile, catch,
        resumed["context"]["plan"], request(resumed, remaining))
    assert uninterrupted["final_state"] == suffix["final_state"]
    assert uninterrupted["final_controller_context"] == suffix["final_controller_context"]
    assert uninterrupted["outcome"]["integration_steps"] == 4
    assert prefix["outcome"]["integration_steps"] == suffix["outcome"]["integration_steps"] == 2
    assert snapshot == original
    assert uninterrupted["forecast_metadata"]["prediction_complete"] is False
    assert uninterrupted["outcome"]["mission_completed"] is False


def test_short_forecast_step_budget_keeps_partial_raw_state_and_checkpoints():
    profile, catch, _, state, snapshot = fixture()
    run = shooting.forecast_constrained_continuation(snapshot, profile, catch, snapshot["context"]["plan"], request(snapshot, .3, 1))
    assert run["outcome"]["termination"] == "forecast_step_budget_exhausted"
    assert run["outcome"]["integration_steps"] == 1
    assert run["final_state"]["time_s"] > state.time_s
    assert len(run["recovery_record"]["checkpoints"]) == 2
    assert run["final_controller_context"]["context"]["deadline_s"] == 1300.


def test_controller_exception_retains_partial_record_without_a_retry(monkeypatch):
    profile, catch, _, state, snapshot = fixture()
    import src.runtime.starship_constrained_recovery as driver
    def fail(*args, **kwargs):
        raise ArithmeticError("fixture")
    monkeypatch.setattr(driver, "control_coast_stopping_distance", fail)
    run = shooting.forecast_constrained_continuation(snapshot, profile, catch, snapshot["context"]["plan"], request(snapshot, .2))
    assert run["outcome"]["termination"] == "forecast_controller_exception"
    assert run["forecast_metadata"]["failure"] == "ArithmeticError"
    assert run["final_state"] == asdict(state)
    assert run["outcome"]["integration_steps"] == 0
    assert len(run["recovery_record"]["checkpoints"]) == 1


def test_post_loop_context_failure_preserves_executed_contact_and_all_checkpoints(monkeypatch):
    profile, catch, _, _, snapshot = fixture(40.)
    def fail(*args, **kwargs):
        raise ValueError("injected end-context failure")
    monkeypatch.setattr(shooting, "capture_context", fail)
    run = shooting.forecast_constrained_continuation(snapshot, profile, catch, snapshot["context"]["plan"], request(snapshot, .2))
    assert run["contact"] is not None
    assert run["outcome"]["termination"] == "surface_contact"
    assert run["final_state"]["time_s"] > snapshot["state"]["time_s"]
    assert len(run["recovery_record"]["checkpoints"]) >= 2
    assert run["final_controller_context"] is None
    assert run["forecast_metadata"]["failure"] == "ValueError"
    assert run["forecast_metadata"]["final_context_error"] == "ValueError"
    assert run["forecast_metadata"]["prediction_complete"] is False


def test_dual_controller_and_end_context_failure_retains_both_error_classes(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    import src.runtime.starship_constrained_recovery as driver
    def controller_fail(*args, **kwargs):
        raise ArithmeticError("controller fixture")
    def context_fail(*args, **kwargs):
        raise ValueError("context fixture")
    monkeypatch.setattr(driver, "control_coast_stopping_distance", controller_fail)
    monkeypatch.setattr(shooting, "capture_context", context_fail)
    run = shooting.forecast_constrained_continuation(snapshot, profile, catch, snapshot["context"]["plan"], request(snapshot, .2))
    assert run["final_controller_context"] is None
    assert run["forecast_metadata"]["controller_error"] == "ArithmeticError"
    assert run["forecast_metadata"]["failure"] == run["forecast_metadata"]["final_context_error"] == "ValueError"
    assert run["forecast_metadata"]["prediction_complete"] is False
    assert run["final_state"] == snapshot["state"] or shooting.saved(run["final_state"]) == snapshot["state"]


def test_restore_reference_history_is_exact_and_not_seeded_from_actual_pose():
    profile, catch, _, state, snapshot = fixture()
    frame, _ = shooting.restore_references(snapshot["context"], profile)
    tracker = ReferenceTracker(profile, state.q_body_to_eci, 100.)
    tracker.update(state.q_body_to_eci, state.q_body_to_eci, state.omega_body_rad_s, 100.)
    snapshot = shooting.capture_context(state, profile, catch, DRIVER_CONFIG,
        **{key: value for key, value in snapshot["context"].items() if key not in ("conditioned_reference", "reference_tracker")},
        conditioned_reference=frame, reference_tracker=tracker)
    restored_frame, restored_tracker = shooting.restore_references(snapshot["context"], profile)
    assert shooting.saved(vars(restored_tracker)) == shooting.saved(vars(tracker))
    assert restored_frame.frame.quaternion == frame.frame.quaternion
    assert restored_frame.time_s == frame.time_s
    assert restored_frame.bridging == frame.bridging


@pytest.mark.parametrize("field,value", [("refreshed", False), ("deadline_s", 1500.),
    ("previous_entry_axis_enu", [0., 0., 2.]), ("next_preview_s", math.inf)])
def test_context_rejects_invalid_clocks_axes_and_recursive_planning(field, value):
    profile, catch, _, _, snapshot = fixture()
    snapshot["context"][field] = value
    with pytest.raises(ValueError):
        shooting.validate_context(snapshot, profile, catch, DRIVER_CONFIG)


def test_context_rejects_stale_configuration_and_mismatching_tracker_bounds():
    profile, catch, _, _, snapshot = fixture()
    changed = deepcopy(profile)
    changed["guidance"]["attitude_frequency_rad_s"] = .3
    with pytest.raises(ValueError):
        shooting.validate_context(snapshot, changed, catch, DRIVER_CONFIG)


def test_zero_offset_preserves_exact_seed_bits_and_bounds_keep_original_immutable():
    _, _, _, _, snapshot = fixture()
    seed = snapshot["context"]["plan"]
    zero = shooting.candidate_plan(seed, [0., 0., 0.])
    assert zero == seed and zero is not seed
    bounded = shooting.candidate_plan(seed, [.002, -.002, 30.])
    assert bounded["bank_heading_enu"] == seed["bank_heading_enu"]
    assert bounded["bank_sign"] == seed["bank_sign"]
    assert math.hypot(*bounded["burn_axis_enu"]) == pytest.approx(1.)
    assert seed["target_velocity_enu_mps"] == [0., 0., -50.]
    with pytest.raises(ValueError):
        shooting.candidate_plan(seed, [0., 0., 301.])


def synthetic_forecast(profile, catch, parameters, snapshot):
    """Synthetic bookkeeping result, explicitly not an integrated prediction."""
    _, _, body, state, _ = fixture(80.)
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, body.dry_mass_kg+state.propellant_kg)
    _, east, north = env.local_frame(point)
    displacement = 10.+1000.*parameters[0]+.1*parameters[2]
    state = replace(state, time_s=snapshot["state"]["time_s"]+.1,
        r_eci_m=env.add(state.r_eci_m, env.add(env.scale(east, displacement), env.scale(north, 1000.*parameters[1]))))
    observation = shooting.saved(_tower_observation(state, body, profile, catch))
    cp = {"time_s": state.time_s, "phase": "recovery_landing_13", "state": shooting.saved(asdict(state)),
        "command": None, "com_rate_body_mps": [0., 0., 0.], "navigation": {"arrival_observation": observation}}
    return {"guidance_policy": "constrained_return_development_v8", "final_state": cp["state"], "contact": None,
        "recovery_record": {"checkpoints": [cp], "handoff": {"eligible": False, "observation": observation}},
        "forecast_metadata": {"prediction_complete": True, "failure": None},
        "outcome": {"termination": "surface_contact", "integration_steps": 1}, "synthetic_fixture": True}


def test_terminal_handoff_observation_is_scored_without_mutating_the_raw_checkpoint():
    profile, catch, _, _, snapshot = fixture()
    forecast = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    point = forecast["recovery_record"]["checkpoints"][0]
    point["navigation"] = {}
    before = deepcopy(forecast)
    score = shooting.score_forecast(forecast)
    assert score is not None
    assert score["observation_source"] == "terminal_handoff_observation"
    assert score["checkpoint_index"] == 0
    assert score["checkpoint"]["navigation"] == {}
    assert len(score["residual"]) == 14
    assert score["objective"] == sum(value*value for value in score["residual"])
    assert forecast == before


def test_same_time_score_rejects_a_later_observation_and_never_collects_individual_best_margins():
    profile, catch, _, _, snapshot = fixture()
    forecast = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    point = deepcopy(forecast["recovery_record"]["checkpoints"][0])
    point["navigation"]["arrival_observation"]["time_s"] += .1
    assert shooting.checkpoint_score(point) is None
    score = shooting.score_forecast(forecast)
    assert score["checkpoint"]["time_s"] == score["actual_same_time_observation"]["time_s"]


def stub_forecasts(monkeypatch, profile, catch, calls):
    def run(snapshot, cfg, recovery, candidate, request):
        parameters = candidate.get("fixture_parameters", [0., 0., 0.])
        calls.append(parameters)
        return synthetic_forecast(profile, catch, parameters, snapshot)
    original = shooting.candidate_plan
    def candidate(seed, parameters):
        return {**original(seed, parameters), "fixture_parameters": list(parameters)}
    monkeypatch.setattr(shooting, "candidate_plan", candidate)
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", run)


def test_eight_call_cap_persists_every_attempt_and_raw_before_score_then_keeps_compact_receipt(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    records, calls = {}, []
    stub_forecasts(monkeypatch, profile, catch, calls)
    base = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    real_score = shooting.score_forecast
    def score(forecast):
        assert sum(item["kind"] == "raw_forecast" for item in records.values()) == len(calls)
        return real_score(forecast)
    monkeypatch.setattr(shooting, "score_forecast", score)
    selected, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch,
        snapshot["context"]["plan"], artifact_sink=memory_sink(records), baseline_reference=base)
    assert 7 <= len(calls) <= 8
    assert receipt["attempted_forecast_count"] == receipt["completed_forecast_count"] == len(calls)
    assert receipt["failed_forecast_count"] == receipt["partial_forecast_count"] == 0
    assert receipt["baseline_equivalence"]["matched"] is True
    assert receipt["origin_context"] == snapshot
    assert receipt["raw_forecasts_persisted_before_analysis"] is True
    assert all("forecast" not in entry for entry in receipt["forecasts"])
    assert sum(item["kind"] == "attempted" for item in records.values()) == len(calls)
    assert sum(item["kind"] == "raw_forecast" for item in records.values()) == len(calls)
    assert selected["production_policy_admitted"] is selected["admissible"] is False
    assert selected["actual_recovery_prediction"] == receipt


def test_missing_or_mismatched_baseline_stops_after_one_attempt_without_selector_leakage(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    calls, records = [], {}
    stub_forecasts(monkeypatch, profile, catch, calls)
    selected, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch,
        snapshot["context"]["plan"], artifact_sink=memory_sink(records), baseline_reference=None)
    assert len(calls) == 1
    assert receipt["optimizer_status"] == "baseline_equivalence_not_established"
    assert receipt["selected_parameters"] == [0., 0., 0.]
    assert selected["target_velocity_enu_mps"] == snapshot["context"]["plan"]["target_velocity_enu_mps"]


def test_baseline_equivalence_normalizes_runtime_tuples_and_parsed_json_lists():
    profile, catch, _, _, snapshot = fixture()
    actual = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    point = actual["recovery_record"]["checkpoints"][0]
    for key in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "engine_states", "flap_angles_rad"):
        point["state"][key] = tuple(point["state"][key])
    point["command"] = {"engines": ({"enabled": True, "throttle": .4},), "flap_angles_rad": (0.,)}
    expected = shooting.saved(actual)
    assert shooting.physical_checkpoint_view(point) != shooting.physical_checkpoint_view(expected["recovery_record"]["checkpoints"][0])
    result = shooting.baseline_equivalence(actual, expected, snapshot)
    assert result["actual_checkpoint_sha256"] == result["expected_checkpoint_sha256"]
    assert result["checked"] is result["matched"] is True
    assert result["reason"] == "exact_physical_checkpoint_suffix"


def test_failed_neighbor_is_persisted_and_consumes_a_call_without_retry(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    calls, records = [], {}
    base = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    def run(origin, cfg, recovery, candidate, request):
        calls.append(1)
        if len(calls) == 2:
            raise ArithmeticError("synthetic fault")
        return deepcopy(base)
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", run)
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch,
        snapshot["context"]["plan"], artifact_sink=memory_sink(records), baseline_reference=base)
    assert len(calls) == 7  # Joint step requires all six neighbors; no retry.
    assert receipt["failed_forecast_count"] == 1
    assert receipt["completed_forecast_count"] == 6
    assert receipt["forecasts"][1]["error_type"] == "ArithmeticError"
    assert sum(item["kind"] == "raw_forecast" for item in records.values()) == 7


def test_eligible_prediction_outranks_lower_soft_score_and_uses_its_terminal_checkpoint(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    calls, records = [], {}
    def terminal_fixture(horizontal_position, horizontal_speed, clearance):
        # Public, physically consistent instantaneous material-point geometry;
        # the enclosing stub still is NOT an executed flight continuation.
        _, _, body, state, _ = fixture()
        props = dyn.mass_properties(body, state.propellant_kg)
        altitude = catch["support_height_m"]+clearance-catch["support_points_body_m"][0][2]+props.com_body_m[2]
        point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
            altitude, time_s=100.1)
        up, east, _ = env.local_frame(point)
        quaternion = tuple(float(x) for x in _attitude(up, east))
        position = env.add(point.r, env.scale(east, horizontal_position))
        earth = (0., 0., env.EARTH_ROTATION_RAD_S)
        state = replace(state, time_s=100.1, r_eci_m=position, q_body_to_eci=quaternion,
            v_eci_mps=env.add(env.cross(earth, position), env.add(env.scale(east, horizontal_speed), env.scale(up, -1.75))),
            omega_body_rad_s=dyn.inverse_rotate(quaternion, earth))
        observation = shooting.saved(_tower_observation(state, body, profile, catch))
        cp = {"time_s": state.time_s, "phase": "recovery_landing_13", "state": shooting.saved(asdict(state)),
            "command": None, "com_rate_body_mps": [0., 0., 0.], "navigation": {"arrival_observation": observation}}
        return {"guidance_policy": "constrained_return_development_v8", "final_state": cp["state"], "contact": None,
            "recovery_record": {"checkpoints": [cp], "handoff": {"eligible": observation["eligible"], "observation": observation}},
            "forecast_metadata": {"prediction_complete": True, "failure": None},
            "outcome": {"termination": "catch_handoff" if observation["eligible"] else "surface_contact", "integration_steps": 1},
            "synthetic_fixture": True}
    base = terminal_fixture(.201, 0., 3.4)
    winner = terminal_fixture(.199, .3, 3.75)
    assert base["recovery_record"]["handoff"]["eligible"] is False
    assert winner["recovery_record"]["handoff"]["eligible"] is True
    assert shooting.score_forecast(winner)["objective"] > shooting.score_forecast(base)["objective"]
    def run(origin, cfg, recovery, candidate, request):
        calls.append(1)
        return deepcopy(winner if len(calls) == 2 else base)
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", run)
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
        artifact_sink=memory_sink(records), baseline_reference=base)
    assert receipt["selected_attempt_index"] == 2
    assert receipt["forecasts"][1]["score"]["observation_source"] == "terminal_handoff_observation"
    assert receipt["schema"] == "missionos.starship_actual_recovery_shooting.v2"


def test_failed_forecast_with_a_complete_flag_is_never_scored(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    records = {}
    bad = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    bad["forecast_metadata"]["failure"] = "ValueError"
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", lambda *a, **k: deepcopy(bad))
    monkeypatch.setattr(shooting, "score_forecast", lambda *a, **k: pytest.fail("failed raw must never be scored"))
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
        artifact_sink=memory_sink(records), baseline_reference=bad)
    assert receipt["failed_forecast_count"] == 1
    assert receipt["forecasts"][0]["score"] is None
    assert sum(item["kind"] == "raw_forecast" for item in records.values()) == 1


def test_missing_sink_and_unpersisted_manifests_fail_closed_before_claiming_a_forecast(monkeypatch):
    profile, catch, _, _, snapshot = fixture()
    with pytest.raises(ValueError, match="requires_artifact_sink"):
        shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"], artifact_sink=None)
    calls = []
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", lambda *a, **k: calls.append(1))
    sink = memory_sink({})
    def bad_sink(identifier, payload):
        manifest = sink(identifier, payload)
        manifest["relative_path"] = "."
        return manifest
    with pytest.raises(ValueError, match="invalid_persisted_forecast_manifest"):
        shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"], artifact_sink=bad_sink)
    assert not calls


def test_joint_step_is_bounded_and_matches_a_public_full_rank_linear_residual_fixture():
    baseline = {"residual": [1., -2., 3.]+[0.]*11}
    neighbors = []
    increments = [.002, .002, 30.]
    for axis, increment in enumerate(increments):
        for sign in (1., -1.):
            residual = deepcopy(baseline["residual"])
            residual[axis] += sign*increment
            neighbors.append({"residual": residual})
    parameters, detail = shooting.joint_parameters(baseline, neighbors)
    assert parameters == pytest.approx([-math.radians(15), math.radians(15), -3.])
    assert all(abs(value) <= 1 for value in detail["bounded_scaled_step"])


def transport_context():
    profile, catch, _, _, original = fixture()
    from src.runtime.starship_constrained_recovery import physical_guidance_configuration
    snapshot = deepcopy(original)
    configuration = physical_guidance_configuration(True)
    snapshot.update(schema=shooting.TRANSPORT_CONTEXT_SCHEMA, physical_guidance_configuration=configuration,
                    guidance_configuration_sha256=shooting.digest(configuration))
    snapshot["context"]["prepare_tail_start_s"] = None
    return profile, catch, snapshot


def transport_reference(profile, catch, snapshot):
    from src.runtime.starship_constrained_recovery import physical_guidance_configuration
    reference = synthetic_forecast(profile, catch, [0., 0., 0.], snapshot)
    reference["guidance_policy"] = "constrained_return_development_v10"
    reference["booster_separation_state"] = deepcopy(snapshot["state"])
    reference["recovery_record"].update(input_separation_state=deepcopy(snapshot["state"]),
        guidance_configuration=physical_guidance_configuration(True))
    binding = {"run_sha256": shooting.digest(reference), "profile_sha256": shooting.digest(profile),
               "catch_profile_sha256": shooting.digest(catch)}
    return reference, binding


def test_schema3_binds_current_transport_law_reference_inputs_and_persists_binding_before_calls(monkeypatch):
    profile, catch, snapshot = transport_context()
    reference, binding = transport_reference(profile, catch, snapshot)
    records, calls = {}, []
    def forecast(origin, cfg, recovery, plan, request):
        assert any(value["kind"] == "origin_context" and value["baseline_binding"] == binding for value in records.values())
        calls.append(1)
        return deepcopy(reference)
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", forecast)
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
        artifact_sink=memory_sink(records), baseline_reference=reference, baseline_binding=binding)
    assert 7 <= len(calls) <= 8
    assert receipt["schema"] == "missionos.starship_actual_recovery_shooting.v3"
    assert receipt["configuration"] == shooting.TRANSPORT_CONFIG
    assert receipt["baseline_binding"] == binding
    assert receipt["baseline_binding_is_caller_assertion"] is True
    assert receipt["source_binding_independently_verified"] is False
    assert receipt["expected_reference_policy_id"] == receipt["reference_policy_id"] == "constrained_return_development_v10"
    assert receipt["baseline_equivalence"]["matched"] is True
    assert receipt["baseline_equivalence"]["expected_reference_policy_id"] == "constrained_return_development_v10"


@pytest.mark.parametrize("change", ["old_policy", "wrong_config", "profile", "catch", "run_hash", "missing"])
def test_schema3_rejects_unmatched_baseline_before_persistence_or_forecasts(monkeypatch, change):
    profile, catch, snapshot = transport_context()
    reference, binding = transport_reference(profile, catch, snapshot)
    if change == "old_policy":
        reference["guidance_policy"] = "constrained_return_development_v8"
    elif change == "wrong_config":
        reference["recovery_record"]["guidance_configuration"]["transport_prepare_roll"] = False
    elif change in ("profile", "catch", "run_hash"):
        binding[{"profile": "profile_sha256", "catch": "catch_profile_sha256", "run_hash": "run_sha256"}[change]] = "0"*64
    elif change == "missing":
        binding = None
    calls = []
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", lambda *a, **k: calls.append("forecast"))
    with pytest.raises(ValueError, match="matching_v10_reference_inputs"):
        shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
            artifact_sink=lambda *a: calls.append("persist"), baseline_reference=reference, baseline_binding=binding)
    assert not calls


def test_schema3_rejects_initial_separation_mismatch_before_observing_dynamics(monkeypatch):
    profile, catch, snapshot = transport_context()
    reference, binding = transport_reference(profile, catch, snapshot)
    initial = deepcopy(snapshot["state"])
    initial["propellant_kg"] -= 1.
    monkeypatch.setattr(dyn, "observe", lambda *a, **k: pytest.fail("preflight must precede dynamics"))
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    with pytest.raises(ValueError, match="initial_state_mismatch"):
        simulate_constrained_recovery(profile, initial, catch, development_actual_planning=True,
            development_transport_prepare_roll=True, actual_planning_artifact_sink=lambda *a: None,
            actual_planning_baseline_reference=reference, actual_planning_baseline_binding=binding)


def test_old_schema2_does_not_gain_transport_binding_fields():
    assert shooting.CONFIG["baseline_equivalence"] == "exact_physical_checkpoint_suffix_of_saved_v8"
    assert "physical_guidance_policy" not in shooting.CONFIG
    assert "context_schema" not in shooting.CONFIG
    assert shooting.actual_planning_configuration(False) == shooting.CONFIG
