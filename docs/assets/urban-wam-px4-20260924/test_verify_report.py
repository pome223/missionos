"""Mutations must not turn missing execution, risk or model value into claims."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "urban_report_verifier", ROOT / "verify_report.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def report():
    return json.loads((ROOT / "summary.json").read_text())


def test_reviewed_measurements_reproduce(report):
    assert MODULE.verify_data(report)["artifact_consistency"] == "passed"


@pytest.mark.parametrize(
    "field",
    [
        "learned_navigation_benefit_established",
        "physical_hardware_executed",
        "collision_prediction_validated",
        "receding_horizon_replanning_implemented",
    ],
)
def test_unsupported_scope_is_rejected(report, field):
    report[field] = True
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


@pytest.mark.parametrize(
    "field", ["destination_reached", "landing_and_disarm_observed", "simulator_removed"]
)
def test_incomplete_outcome_is_rejected(report, field):
    report["runs"][0]["verification"][field] = False
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_missing_trajectory_interval_is_rejected(report):
    del report["runs"][0]["trajectory"][5:20]
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_clearance_cannot_be_invented(report):
    report["runs"][0]["verification"]["minimum_observed_envelope_clearance_m"] += 1
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_simulator_time_cannot_be_replaced_with_wall_time(report):
    v = report["runs"][0]["verification"]
    v["route_simulation_seconds"] = v["route_wall_seconds"]
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_model_ranking_cannot_be_changed_after_dispatch(report):
    run = next(r for r in report["runs"] if r["verification"]["model_invoked"])
    scores = run["verification"]["model_selection"]["scores"]
    chosen = run["verification"]["route_id"]
    scores[chosen]["goal_mse"] = 100
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_expired_observation_cannot_be_claimed_fresh(report):
    run = next(r for r in report["runs"] if r["verification"]["model_invoked"])
    run["model"]["observation_age_at_dispatch_seconds"] = 181
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_geometric_flight_cannot_be_credited_to_model(report):
    run = next(r for r in report["runs"] if not r["verification"]["model_invoked"])
    run["model"] = copy.deepcopy({"actual_model_calls": 2})
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


@pytest.mark.parametrize("field", ["created_vm_absent", "created_boot_disk_absent"])
def test_resource_cleanup_must_be_observed(report, field):
    report["resources"][field] = False
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_budget_cannot_be_overrun_in_public_claim(report):
    report["resources"]["total_task_conservative_estimate_upper_bound_usd"] = 11
    with pytest.raises(ValueError):
        MODULE.verify_data(report)


def test_estimate_cannot_be_relabelled_as_invoice(report):
    report["resources"]["invoice_reconciled"] = True
    with pytest.raises(ValueError):
        MODULE.verify_data(report)
