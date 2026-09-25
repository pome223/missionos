"""Adversarial mutations of published denominator, authority and outcome claims."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "headroom_report_verifier", ROOT / "verify_report.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def original():
    return json.loads((ROOT / "summary.json").read_text())


def test_report_consistency():
    assert module.verify_data(original())["artifact_consistency"] == "passed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gpu_gate_passed", True),
        ("new_model_calls", 1),
        ("oracle_additional_arrivals_upper_bound", 3),
        ("complete_candidate_outcome_matrix_collected", True),
        ("exact_physics_state_cloning_claimed", True),
        ("learned_navigation_benefit_established", True),
        ("verified_baseline_arrivals", 999),
    ],
)
def test_reject_inflated_gate_claim(field, value):
    d = original()
    d["gate"][field] = value
    with pytest.raises(ValueError):
        module.verify_data(d)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_invoked", True),
        ("destination_reached", False),
        ("landing_and_disarm_observed", False),
        ("simulator_removed", False),
        ("building_contact_messages", 1),
        ("observed_distance_m", 0),
        ("minimum_observed_envelope_clearance_m", 99),
        ("route_wall_seconds", 10000),
    ],
)
def test_reject_inflated_flight_claim(field, value):
    d = original()
    d["runs"][0][field] = value
    with pytest.raises(ValueError):
        module.verify_data(d)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uses_scene_truth_for_selection", True),
        ("safety_filter_rejected_frozen_choice", True),
        ("shadow_choices_are_not_additional_flights", False),
        ("building_sensor_positive_control_contacts", 0),
        ("probe_removed_before_flight", False),
        ("observation_age_at_dispatch_wall_s", 180),
        ("initial_position_error_m", 1),
        ("initial_speed_m_s", 2),
    ],
)
def test_reject_unfair_or_unobserved_selection(field, value):
    d = original()
    d["runs"][0]["depth_selection"][field] = value
    with pytest.raises(ValueError):
        module.verify_data(d)


def test_case_cannot_be_counted_twice():
    d = original()
    d["runs"][1] = copy.deepcopy(d["runs"][0])
    with pytest.raises(ValueError):
        module.verify_data(d)


def test_changed_trajectory_is_detected():
    d = original()
    d["runs"][0]["trajectory"][3][1] += 20
    with pytest.raises(ValueError):
        module.verify_data(d)


def test_preflight_failure_cannot_be_relabeled_as_flight():
    d = original()
    d["failures"][0]["controller_events_created"] = True
    with pytest.raises(ValueError):
        module.verify_data(d)


def test_unattempted_cases_cannot_disappear():
    d = original()
    d["unattempted_cases"] = []
    with pytest.raises(ValueError):
        module.verify_data(d)
