"""Public fixtures for frozen landing decisions; no plant integration."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_landing_decision as landing
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import configuration
from src.runtime.starship_constrained_recovery import physical_guidance_configuration, simulate_constrained_recovery
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    body = vehicle(profile, "booster")
    p = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 3000., time_s=100.)
    up, east, _ = env.local_frame(p)
    q = tuple(float(x) for x in _attitude(up, east))
    state = dyn.State6DOF(100., p.r, p.v, q, (0., 0., 0.), 60000.,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    frame = ConditionedGeographicFrame(q, 99.9, maximum_roll_rate_rad_s=.03/.28)
    plan = {"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., 0.],
        "bank_heading_enu": [1., 0., 0.], "bank_sign": -1, "prediction_is_execution": False}
    snapshot = shooting.capture_context(state, profile, catch, physical_guidance_configuration(True),
        phase="recovery_entry_coast", start_time_s=90., deadline_s=1290., plan=plan, refreshed=True,
        burn_start_s=92., settle_start_s=95., previous_entry_axis_enu=[0., 0., 1.],
        entry_pretrim_prepared_at_s=None, next_preview_s=90., braking_preview=None,
        prior_command_reference={"quaternion": list(q), "time_s": 99.9}, conditioned_reference=frame,
        reference_tracker=None, prepare_tail_start_s=99.)
    command = {"engines": [], "flap_angles_rad": []}
    cp = {"time_s": 100., "phase": "recovery_entry_coast", "state": deepcopy(snapshot["state"]),
        "command": command, "com_rate_body_mps": [0., 0., 0.], "navigation": {}}
    source = {"guidance_policy": "constrained_return_development_v11", "recovery_record": {
        "checkpoints": [cp], "input_separation_state": deepcopy(snapshot["state"]),
        "plans": [{"plan": deepcopy(plan)} for _ in range(3)], "actual_planning_receipt": {
            "schema": shooting.TRANSPORT_SCHEMA, "baseline_equivalence": {"matched": True},
            "origin_context": deepcopy(snapshot), "origin_state_sha256": shooting.digest(snapshot["state"])}}}
    request = {"origin_context_sha256": shooting.digest(snapshot), "duration_s": 40.,
        "maximum_integration_steps": 400, "wall_deadline_monotonic_s": 1e20}
    baseline = {"recovery_record": {"checkpoints": [deepcopy(cp), {**deepcopy(cp), "command": None}]},
        "forecast_metadata": {"request": request, "failure": None}}
    binding = landing.compare_saved_baseline(baseline, source, snapshot, profile, catch, source_map={"public.py": "a"*64})
    prediction = {"recovery_record": {"handoff": {"eligible": True, "state": asdict(state),
        "observation": {"eligible": True}}}, "final_state": deepcopy(snapshot["state"]),
        "forecast_metadata": {"failure": None, "prediction_complete": True},
        "outcome": {"termination": "catch_handoff"}}
    return profile, catch, snapshot, source, baseline, binding, prediction


def test_comparison_normalizes_runtime_tuples_and_ignores_only_unexecuted_command():
    profile, catch, snapshot, source, baseline, binding, _ = fixture()
    assert binding["matched"] is True
    assert binding["baseline_checkpoint_count"] == binding["saved_checkpoint_count"] == 1
    baseline["recovery_record"]["checkpoints"][0]["state"]["q_body_to_eci"] = tuple(snapshot["state"]["q_body_to_eci"])
    assert landing.compare_saved_baseline(baseline, source, snapshot, profile, catch, source_map={})["matched"] is True
    baseline["recovery_record"]["checkpoints"][0]["time_s"] += 1e-10
    assert landing.compare_saved_baseline(baseline, source, snapshot, profile, catch, source_map={})["matched"] is False


@pytest.mark.parametrize("change", ["state", "command", "phase", "failure", "wrong_origin", "wrong_budget"])
def test_baseline_rejects_divergence_or_failed_continuation(change):
    profile, catch, snapshot, source, baseline, _, _ = fixture()
    if change in ("state", "command", "phase"):
        cp = baseline["recovery_record"]["checkpoints"][0]
        if change == "state":
            cp["state"]["propellant_kg"] -= 1.
        elif change == "command":
            cp["command"]["flap_angles_rad"] = [1.]
        else:
            cp["phase"] = "recovery_landing_13"
    elif change == "failure":
        baseline["forecast_metadata"]["failure"] = "RuntimeError"
    else:
        key = "origin_context_sha256" if change == "wrong_origin" else "duration_s"
        baseline["forecast_metadata"]["request"][key] = "b"*64 if change == "wrong_origin" else 41.
    assert landing.compare_saved_baseline(baseline, source, snapshot, profile, catch, source_map={})["matched"] is False


@pytest.mark.parametrize("key,value", [("matched", False), ("matched", 1), ("baseline_checkpoint_count", True),
    ("saved_checkpoint_count", True), ("baseline_checkpoint_count", 401), ("source_map_sha256", "bad"),
    ("source_authentication_independently_verified", True), ("prediction_is_execution", True)])
def test_closed_baseline_binding(key, value):
    profile, catch, snapshot, _, _, binding, _ = fixture()
    binding[key] = value
    with pytest.raises(ValueError, match="landing_baseline_saved_data_binding"):
        landing.validate_baseline_binding(binding, snapshot, profile, catch)


def test_decision_is_one_closed_request_no_admission_and_does_not_mutate_inputs():
    _, _, snapshot, _, _, binding, prediction = fixture()
    before = deepcopy((snapshot, binding, prediction))
    decision = landing.decision_from_prediction(snapshot, .5, prediction, baseline_binding=binding)
    landing.validate_live_decision(decision, snapshot)
    assert decision["scheduled_onset_s"] == 100.5
    assert decision["runtime_preview_calls"] == 0
    assert all(decision[key] is False for key in ("arrival_admitted", "support_admitted", "physical_execution"))
    assert (snapshot, binding, prediction) == before


@pytest.mark.parametrize("key,value", [("delay_s", True), ("delay_s", .25), ("scheduled_onset_s", 101.),
    ("arrival_admitted", True), ("support_admitted", True), ("prediction_is_execution", True),
    ("runtime_preview_calls", False), ("origin_state_sha256", "c"*64), ("prediction_sha256", "bad")])
def test_live_decision_rejects_widened_schedule_or_authority(key, value):
    _, _, snapshot, _, _, binding, prediction = fixture()
    decision = landing.decision_from_prediction(snapshot, .5, prediction, baseline_binding=binding)
    decision[key] = value
    with pytest.raises(ValueError):
        landing.validate_live_decision(decision, snapshot)


@pytest.mark.parametrize("key,value", [("prediction_complete", False), ("failure", "ValueError")])
def test_no_full_return_admission_from_incomplete_or_failed_forecast(key, value):
    _, _, snapshot, _, _, binding, prediction = fixture()
    prediction["forecast_metadata"][key] = value
    with pytest.raises(ValueError, match="requires_predicted_full_handoff"):
        landing.decision_from_prediction(snapshot, 0., prediction, baseline_binding=binding)


def test_full_handoff_is_required_even_with_better_xy_or_staging_score():
    _, _, snapshot, _, _, binding, prediction = fixture()
    prediction["recovery_record"]["handoff"]["observation"]["eligible"] = False
    prediction["score"] = 0.
    with pytest.raises(ValueError, match="requires_predicted_full_handoff"):
        landing.decision_from_prediction(snapshot, 0., prediction, baseline_binding=binding)


def test_bundle_exact_input_and_refresh_binding_without_numeric_planning():
    profile, catch, snapshot, source, _, _, _ = fixture()
    bundle = landing.frozen_boostback_bundle(source, profile, catch, source_map={})
    landing.validate_bundle(bundle, profile, catch, snapshot["state"])
    landing.validate_refresh(bundle, snapshot)
    snapshot["context"]["prior_command_reference"]["time_s"] -= .01
    with pytest.raises(ValueError, match="frozen_refresh_actual_state_or_controller_mismatch"):
        landing.validate_refresh(bundle, snapshot)


@pytest.mark.parametrize("delay", [True, -.5, .25, 1.])
def test_forecast_closed_actions_before_any_plant_call(monkeypatch, delay):
    profile, catch, snapshot, _, baseline, _, _ = fixture()
    monkeypatch.setattr(dyn, "step", lambda *a: pytest.fail("not allowed to integrate"))
    with pytest.raises(ValueError, match="unsupported_landing_decision_delay"):
        landing.forecast_landing_decision(snapshot, profile, catch, delay_s=delay,
            request=baseline["forecast_metadata"]["request"])


def test_actual_decision_requires_bundle_before_any_observation(monkeypatch):
    profile, catch, snapshot, _, _, binding, prediction = fixture()
    decision = landing.decision_from_prediction(snapshot, 0., prediction, baseline_binding=binding)
    monkeypatch.setattr(dyn, "observe", lambda *a: pytest.fail("not allowed to observe"))
    with pytest.raises(ValueError, match="requires_frozen_boostback_bundle"):
        simulate_constrained_recovery(profile, snapshot["state"], catch, development_landing_decision=decision)


def test_forecast_passes_exact_context_and_local_budget_only(monkeypatch):
    profile, catch, snapshot, _, baseline, _, _ = fixture()
    calls = []
    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {"synthetic_stub": True}
    monkeypatch.setattr("src.runtime.starship_constrained_recovery.simulate_constrained_recovery", stub)
    result = landing.forecast_landing_decision(snapshot, profile, catch, delay_s=.5,
        request=baseline["forecast_metadata"]["request"])
    assert result == {"synthetic_stub": True}
    assert calls[0][1]["_resume_context"] == snapshot
    assert calls[0][1]["_forecast_landing_delay_s"] == .5
    assert calls[0][1]["duration_s"] == 40.
    assert calls[0][1]["_forecast_landing_local"] is True


@pytest.mark.parametrize("remainder,expected", [(.1-1e-12, .1), (.1+1e-12, .1), (.1-.0001, .1-.0001), (.2, .1)])
def test_local_clock_tolerance_preserves_only_numerical_roundoff(remainder, expected):
    assert landing.local_macrostep(.1, remainder) == expected


@pytest.mark.parametrize("value", [True, 0., -1., float("inf"), float("nan")])
def test_local_clock_rejects_invalid_clock(value):
    with pytest.raises(ValueError, match="invalid_local_landing_clock"):
        landing.local_macrostep(.1, value)
