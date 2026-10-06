"""Forecast admission arithmetic; these tests do not certify forecast dynamics."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.runtime import starship_booster_recovery as producer
from src.runtime import starship_booster_recovery_verifier as verifier

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("termination,arrival,reserve,admitted,fallback", [
    ("surface_contact", False, False, False, True),
    ("surface_contact", False, True, False, True),
    ("catch_handoff", True, False, False, True),
    ("catch_handoff", True, True, True, False),
    ("rate_settle_failed", False, True, False, False),
])
def test_intercept_is_not_a_capture_plan(termination, arrival, reserve, admitted, fallback):
    prediction = {"termination": termination, "terminal_handoff_eligible": arrival,
                  "terminal_fuel_reserve_met": reserve, "position_enu_m": [1., 0., 30.]}
    before = deepcopy(prediction)
    decision = producer._cutoff_admission(prediction, [1000., 0., 100000.])
    assert decision["capture_plan_admissible"] is admitted
    assert decision["best_effort_cutoff_requested"] is fallback
    assert decision["cutoff_requested"] is (admitted or fallback)
    assert decision["dispatch_authority_created"] is False
    assert decision["prediction_is_execution"] is False
    assert prediction == before


@pytest.mark.parametrize("predicted_horizontal", [10., 1000., -1000.])
def test_numpy_navigation_admission_survives_json_storage(predicted_horizontal):
    import numpy as np
    prediction = {"termination": "surface_contact", "terminal_handoff_eligible": False,
                  "terminal_fuel_reserve_met": False,
                  "position_enu_m": np.array([predicted_horizontal, 0., 30.])}
    decision = producer._cutoff_admission(prediction, np.array([1000., 0., 100000.]))
    restored = json.loads(json.dumps(decision, allow_nan=False))
    for key in ("capture_plan_admissible", "geographic_intercept_predicted", "best_effort_cutoff_requested",
                "cutoff_requested", "prediction_is_execution", "dispatch_authority_created"):
        assert type(restored[key]) is bool
    assert restored["geographic_intercept_predicted"] is (predicted_horizontal <= 100.)


@pytest.fixture(scope="module")
def evidence():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    config = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              50000., 200000., time_s=180.)
    up, east, north = env.local_frame(point)
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.add(env.scale(up, 600.), env.scale(east, 1000.))),
                          _attitude(up, north), (.001, -.002, .003), 200000.,
                          tuple(dyn.EngineState(throttle=.4 if i < 3 else 0.) for i in range(len(booster.engines))),
                          tuple(0. for _ in booster.aero_panels))
    initial = asdict(state)
    run = producer.simulate_recovery(profile, initial, config, duration_s=.6)
    # A genuine short, counterfactual cutoff continuation. It is not execution
    # of the return trajectory, whose inherited state and .6s horizon persist.
    prediction = producer._predict_cutoff(state, booster, profile, config, full=True, remaining_duration_s=.6)
    position = verifier._prediction_origin(json.loads(json.dumps(initial)), profile)
    decision = producer._cutoff_admission(prediction, position)
    record = run["recovery_record"]
    record["full_coast_prediction_count"] = 1
    record["cutoff_predictions"] = [{"time_s": 180., "prediction": prediction,
        "origin_state": initial, "origin_position_enu_m": position, "admission": decision}]
    record["capture_planning"]["admissible_prediction_count"] = 0
    return profile, config, json.loads(json.dumps(initial)), json.loads(json.dumps(run, allow_nan=False))


def test_independent_checker_assesses_budget_without_claiming_forecast_reexecution(evidence):
    profile, config, initial, run = evidence
    verdict = verifier.verify_recovery(run, initial, profile, config)
    assert verdict["passed"], verdict
    assert verdict["capture_planning"] == {"assessed": True, "forecast_count": 1,
        "admissible_prediction_count": 0, "best_effort_cutoff_used": False,
        "forecast_dynamics_reexecuted": False, "dispatch_authority_created": False}
    assert verdict["handoff_reached"] is False


@pytest.mark.parametrize("mutation", ["admission", "authority", "integer_boolean", "reserve_flag", "reserve_amount",
                                     "ground_speed", "origin_pose", "origin_position", "origin_clock", "summary",
                                     "summary_removed", "coarse_budget", "coarse_selected_budget", "coarse_count"])
def test_admission_cannot_hide_missing_budget_or_disconnected_origin(evidence, mutation):
    profile, config, initial, original = evidence
    run = deepcopy(original)
    record = run["recovery_record"]
    receipt = record["cutoff_predictions"][0]
    if mutation == "admission":
        receipt["admission"]["capture_plan_admissible"] = True
    elif mutation == "authority":
        receipt["admission"]["dispatch_authority_created"] = True
    elif mutation == "integer_boolean":
        receipt["admission"]["capture_plan_admissible"] = 0
    elif mutation == "reserve_flag":
        receipt["prediction"]["terminal_fuel_reserve_met"] = False
    elif mutation == "reserve_amount":
        receipt["prediction"]["terminal_propellant_reserve_kg"] = 0.
    elif mutation == "ground_speed":
        receipt["prediction"]["terminal_ground_speed_mps"] = 0.
    elif mutation == "origin_pose":
        receipt["origin_state"]["r_eci_m"][0] += 10000.
    elif mutation == "origin_position":
        receipt["origin_position_enu_m"][0] += 10000.
    elif mutation == "origin_clock":
        receipt["origin_state"]["time_s"] += .1
    elif mutation == "summary":
        record["capture_planning"]["admissible_prediction_count"] = 1
    elif mutation == "summary_removed":
        del record["capture_planning"]
    else:
        # Mutate both copies so event/plan binding itself still passes.
        plan = record["boostback_plan"]
        if mutation == "coarse_budget":
            plan["candidates"][0]["point_model_contact_propellant_reserve_kg"] = 0.
        elif mutation == "coarse_selected_budget":
            plan["selected_contact_budget_met"] = not plan["selected_contact_budget_met"]
        else:
            plan["contact_budget_candidate_count"] += 1
        run["events"][1]["plan"] = deepcopy(plan)
    verdict = verifier.verify_recovery(run, initial, profile, config)
    assert verdict["passed"] is False, verdict
    assert verdict["issues"][0]["code"] == "capture_planning", verdict
    assert "capture_planning" not in verdict
    assert verdict["handoff_reached"] is False


def test_cutoff_cannot_rename_failed_prediction_as_admitted(evidence):
    profile, config, _, original = evidence
    record = deepcopy(original["recovery_record"])
    events = deepcopy(original["events"])
    events.insert(2, {"time_s": 180., "event": "boostback_complete_rate_settle",
                      "cutoff_basis": "admitted_capture_prediction"})
    with pytest.raises(verifier._Invalid) as error:
        verifier._capture_planning(record, events, profile, config)
    assert error.value.issue["code"] == "capture_planning"


def test_older_records_remain_unassessed_instead_of_gaining_admission(evidence):
    profile, config, initial, original = evidence
    run = deepcopy(original)
    record = run["recovery_record"]
    del record["capture_planning"]
    record["full_coast_prediction_count"] = 0
    record["cutoff_predictions"] = []
    for plan in [record["boostback_plan"], run["events"][1]["plan"]]:
        for key in ("selection_rule", "contact_budget_candidate_count", "selected_contact_budget_met"):
            del plan[key]
        for candidate in plan["candidates"]+[plan["selected"]]:
            candidate.pop("point_model_contact_propellant_reserve_kg", None)
            candidate.pop("point_model_contact_budget_met", None)
    verdict = verifier.verify_recovery(run, initial, profile, config)
    assert verdict["passed"], verdict
    assert "capture_planning" not in verdict


def test_report_keeps_zero_admitted_plans_and_failed_execution_visible():
    from src.runtime.starship_sixdof_report import _recovery_panel
    report = _recovery_panel({"runs": [{"booster_recovery": {"policy_id": producer.POLICY_ID},
        "booster_run": {"outcome": {"termination": "surface_contact"}, "recovery_record": {
            "full_coast_prediction_count": 3, "capture_planning": {"admissible_prediction_count": 0,
                                                                   "best_effort_cutoff_used": True}}}}]})
    assert "0 / 3 件" in report
    assert "成立計画を得られず継続" in report
    assert "接触機構の実行</th><td>未実行" in report
    assert "連続支持の計算結果</th><td>未達" in report
