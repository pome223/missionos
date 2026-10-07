"""Reject coherent-looking edits to synthetic experiment evidence."""

from copy import deepcopy
from fractions import Fraction
import json

import pytest

from src.runtime.starship_dispenser_experiment import digest, run_dispenser_experiment
from src.runtime.starship_dispenser_verifier import verify_dispenser_experiment


@pytest.fixture(scope="module")
def verified_study():
    study = json.loads(json.dumps(run_dispenser_experiment(approved=True)))
    verdict = verify_dispenser_experiment(study)
    assert verdict["verified"], verdict
    return study


def _evaluation_run(study):
    return next(
        run for run in study["policy_runs"]
        if run["split"] == "eval" and run["policy"] == "fixed_retry"
    )


def _rebind_world(study, world):
    """Repair every hash binding, so rejection must go beyond hash consistency."""
    world["tape_sha256"] = digest(
        {key: value for key, value in world.items() if key != "tape_sha256"}
    )
    for collection in ("training_candidate_runs", "policy_runs", "clairvoyant_runs"):
        for run in study[collection]:
            if run["world_id"] == world["world_id"]:
                run["tape_sha256"] = world["tape_sha256"]


def _increment_fraction(record):
    value = Fraction(record["numerator"], record["denominator"]) + 1
    record.update(numerator=value.numerator, denominator=value.denominator, value=float(value))


def _corrupt(study, mutation):
    run = _evaluation_run(study)
    first = run["steps"][0]
    if mutation == "rehash_future_sensor_and_rebind_all_runs":
        world = study["worlds"][0]
        world["sensor_tape"][-1]["healthy"] = not world["sensor_tape"][-1]["healthy"]
        _rebind_world(study, world)
    elif mutation == "rehash_recovery_and_rebind_all_runs":
        world = study["worlds"][0]
        world["recovery_time_s"] = 8 if world["recovery_time_s"] == 4 else 4
        _rebind_world(study, world)
    elif mutation == "seed_in_policy_history":
        first["public_history"]["seed"] = 1001
    elif mutation == "hidden_recovery_in_policy_observation":
        first["public_history"]["observations"][0]["recovery_time_s"] = 4
    elif mutation == "receipt_early_with_consistent_time_units":
        first["receipt"].update(end_ticks=1, end_s=2)
    elif mutation == "invented_first_retry_release":
        # The system is known jammed at t=0 for every latent hypothesis.
        first["receipt"].update(success=True, released_delta=1, release_count_after=1)
    elif mutation == "terminal_utility_and_exact_fraction":
        _increment_fraction(run["terminal"]["utility_exact"])
        run["terminal"]["utility"] = run["terminal"]["utility_exact"]["value"]
    elif mutation == "blocked_rules_with_existing_receipt":
        first["rules"].update(allowed=False, reasons=["retry_budget_exhausted"])
    elif mutation == "physical_authority_in_scope":
        run["scope"]["physical_execution_authorized"] = True
    elif mutation == "physical_execution_claim":
        study["claim_boundary"]["physical_execution"] = True
    elif mutation == "evaluation_summary_improvement":
        study["summary"]["eval"]["finite_model_optimal"]["mean_release_count"] += 0.5
    elif mutation == "rewrite_training_winner":
        tuning = study["tuning"]["fixed_retry"]
        selected = tuning["selected_parameters"]
        tuning["selected_parameters"] = deepcopy(next(
            candidate["parameters"] for candidate in tuning["candidates"]
            if candidate["parameters"] != selected
        ))
    elif mutation == "overlapping_train_eval_seeds_with_rehash":
        study["config"]["eval_seeds"][0] = study["config"]["train_seeds"][0]
        study["config_sha256"] = digest(study["config"])
    elif mutation == "coherent_false_optimal_fraction":
        _increment_fraction(study["model_bounds"]["finite_model_optimal_expected_utility"])
    elif mutation == "coherent_false_clairvoyant_fraction":
        _increment_fraction(study["model_bounds"]["clairvoyant_expected_utility"])
    elif mutation == "coherent_false_baseline_fraction":
        _increment_fraction(study["model_bounds"]["policy_expected_utilities"]["history_rule"])
    elif mutation == "duplicate_policy_run_preserving_count":
        study["policy_runs"][-1] = deepcopy(study["policy_runs"][-6])
    elif mutation == "missing_policy_run":
        study["policy_runs"].pop()
    elif mutation == "duplicate_training_run_preserving_count":
        study["training_candidate_runs"][-1] = deepcopy(study["training_candidate_runs"][-2])
    elif mutation == "missing_clairvoyant_run":
        study["clairvoyant_runs"].pop()
    else:
        raise AssertionError(f"Unknown mutation: {mutation}")


def test_json_roundtrip_preserves_verifiable_accounting(verified_study):
    before = digest(verified_study)
    verdict = verify_dispenser_experiment(verified_study)
    assert verdict["verified"] and verdict["runs_checked"] == 870
    assert verdict["physical_execution_verified"] is False
    assert digest(verified_study) == before


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        ("rehash_future_sensor_and_rebind_all_runs", "study.worlds"),
        ("rehash_recovery_and_rebind_all_runs", "study.worlds"),
        ("seed_in_policy_history", "step.public_history"),
        ("hidden_recovery_in_policy_observation", "step.public_history"),
        ("receipt_early_with_consistent_time_units", "step.receipt"),
        ("invented_first_retry_release", "step.receipt"),
        ("terminal_utility_and_exact_fraction", "run.terminal"),
        ("blocked_rules_with_existing_receipt", "step.rules"),
        ("physical_authority_in_scope", "run.scope"),
        ("physical_execution_claim", "study.claim_boundary"),
        ("evaluation_summary_improvement", "study.summary"),
        ("rewrite_training_winner", "study.tuning"),
        ("overlapping_train_eval_seeds_with_rehash", "study.config"),
        ("coherent_false_optimal_fraction", "finite_model_optimal_expected_utility"),
        ("coherent_false_clairvoyant_fraction", "clairvoyant_expected_utility"),
        ("coherent_false_baseline_fraction", "policy_expected_utilities"),
        ("duplicate_policy_run_preserving_count", "run.world_id"),
        ("missing_policy_run", "policy_runs"),
        ("duplicate_training_run_preserving_count", "run.world_id"),
        ("missing_clairvoyant_run", "clairvoyant_runs"),
    ],
)
def test_verifier_rejects_semantic_evidence_edits(verified_study, mutation, expected_reason):
    corrupted = deepcopy(verified_study)
    _corrupt(corrupted, mutation)
    before = digest(corrupted)
    verdict = verify_dispenser_experiment(corrupted)
    assert verdict["verified"] is False, mutation
    assert any(expected_reason in reason for reason in verdict["reasons"]), verdict
    assert verdict["physical_execution_verified"] is False
    assert digest(corrupted) == before
