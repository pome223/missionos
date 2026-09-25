"""Detect hidden retries, old-result pooling, timing and outcome overclaims."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "r2_headroom_verifier", ROOT / "verify_report.py"
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def inputs():
    return [
        json.loads((ROOT / name).read_text())
        for name in (
            "summary.json",
            "registration.json",
            "registration-timing.json",
            "case-bindings.json",
        )
    ]


def test_saved_report():
    assert report.verify()["registration_binding"] == "passed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gpu_gate_passed", True),
        ("new_model_calls", 1),
        ("oracle_additional_arrivals_upper_bound", 999),
        ("complete_candidate_outcome_matrix_collected", True),
        ("learned_navigation_benefit_established", True),
    ],
)
def test_reject_unsupported_gate_claim(field, value):
    data = inputs()
    data[0]["gate"][field] = value
    with pytest.raises(ValueError):
        report.verify_data(*data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cases_executed_at_registration", 1),
        ("previous_outcomes_combined", True),
        ("additional_model_calls_permitted", 1),
        ("no_retry_or_case_replacement", False),
        ("frozen_record_sha256", "0" * 64),
    ],
)
def test_reject_changed_registration(field, value):
    data = inputs()
    data[1][field] = value
    with pytest.raises(ValueError):
        report.verify_data(*data)


def test_reject_post_execution_registration():
    data = inputs()
    data[2]["driver_started_at_unix_s"] = data[1]["frozen_at_unix_s"] - 1
    with pytest.raises(ValueError):
        report.verify_data(*data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("destination_reached", False),
        ("landing_and_disarm_observed", False),
        ("simulator_removed", False),
        ("building_contact_messages", 1),
        ("observed_distance_m", 0),
    ],
)
def test_reject_overstated_flight(field, value):
    data = inputs()
    data[0]["runs"][0][field] = value
    with pytest.raises(ValueError):
        report.verify_data(*data)


def test_reject_old_or_replaced_source():
    data = inputs()
    data[0]["runtime_source_sha256"]["scripts/urban_headroom_contact_probe.py"] = (
        "0" * 64
    )
    with pytest.raises(ValueError):
        report.verify_data(*data)


def test_reject_duplicate_case():
    data = inputs()
    data[0]["runs"].append(data[0]["runs"][0])
    with pytest.raises(ValueError):
        report.verify_data(*data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("probe_receipt_sha256", "0" * 64),
        ("scene_ready_after_zero_probe_exit", False),
        ("case_started_at_unix_s", 0),
    ],
)
def test_reject_unbound_or_old_preflight(field, value):
    data = inputs()
    data[3][0][field] = value
    with pytest.raises(ValueError):
        report.verify_data(*data)


def test_observed_frame_cannot_be_relabeled_as_generated_evidence():
    data = inputs()[0]
    observations = json.loads((ROOT / "observations.json").read_text())
    observations[0]["generated_image"] = True
    with pytest.raises(ValueError):
        report.verify_observations(data, observations)
