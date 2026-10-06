"""Independent finite-world and evidence checks for the synthetic dispenser study."""

from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import json
from math import gcd

import pytest

from src.runtime.starship_dispenser_experiment import (
    FiniteModelSolver,
    ActionProposal,
    PublicBudget,
    PublicHistory,
    SensorObservation,
    RetryObservation,
    constrain_proposal,
    default_experiment_config,
    digest,
    make_world,
    run_dispenser_experiment,
    run_policy_tape,
    validate_config,
)
from src.runtime.starship_dispenser_verifier import verify_dispenser_experiment


@pytest.fixture(scope="module")
def study():
    return json.loads(json.dumps(run_dispenser_experiment(approved=True)))


def _controlled_world(config, recovery=4, healthy=False):
    world = make_world(config, "eval", "short", 1001)
    world["recovery_time_s"] = recovery
    for reading in world["sensor_tape"]:
        reading["healthy"] = healthy
    world["tape_sha256"] = digest(
        {key: value for key, value in world.items() if key != "tape_sha256"}
    )
    return world


def _small_optimum(deadline, payloads, max_attempts):
    """Separate forward-tree expectation, without production transition helpers."""
    recovery_ticks = (2, 4, 7, 9, None)

    def available(index, tick):
        return recovery_ticks[index] is not None and recovery_ticks[index] <= tick

    @lru_cache(maxsize=None)
    def solve(tick, attempts, released, posterior):
        if tick >= deadline or attempts >= max_attempts or released >= payloads:
            return Fraction(0)
        options = [Fraction(0)]
        for action, duration in (("wait", 1), ("retry", 2)):
            if tick + duration > deadline:
                continue
            end = tick + duration
            total = -Fraction(3, 10) * duration - int(action == "retry")
            for success in (False, True) if action == "retry" else (False,):
                for signal in (False, True):
                    weights = []
                    for i, weight in enumerate(posterior):
                        allowed = action != "retry" or available(i, tick) == success
                        chance = 16 if available(i, end) else 3
                        weights.append(
                            weight * (chance if signal else 20 - chance) if allowed else 0
                        )
                    if not sum(weights):
                        continue
                    probability = Fraction(sum(weights), 20 * sum(posterior))
                    divisor = gcd(*weights)
                    belief = tuple(w // divisor for w in weights)
                    total += probability * (
                        10 * int(success)
                        + solve(
                            end, attempts + int(action == "retry"), released + int(success), belief
                        )
                    )
            options.append(total)
        return max(options)

    return solve(0, 0, 0, (1, 1, 1, 1, 2))


def test_finite_optimum_matches_independent_exact_rational_audit():
    solver = FiniteModelSolver(default_experiment_config())
    # Separate audit enumerated the hidden-world/observation branches with exact
    # rational probabilities; this is not an empirical evaluation-set maximum.
    expected = Fraction(12417893326807, 1280000000000)
    assert solver.initial_value() == expected
    assert solver.initial_value() < Fraction(23, 2)  # Recovery-time-informed bound.
    choices = solver.action_values(0, 0, 0, (1, 1, 1, 1, 2))
    assert choices["wait"] == expected
    assert choices["wait"] > choices["retry"]
    assert choices["wait"] > choices["abort"]


@pytest.mark.parametrize(
    "deadline,payloads,attempts", [(2, 1, 1), (3, 1, 2), (4, 1, 2), (5, 2, 3), (6, 2, 3)]
)
def test_small_horizons_match_independent_enumeration(deadline, payloads, attempts):
    config = default_experiment_config()
    config.update(deadline_ticks=deadline, payload_count=payloads, max_attempts=attempts)
    solver = FiniteModelSolver(config)
    assert solver.initial_value() == _small_optimum(deadline, payloads, attempts)
    assert solver.initial_value() <= solver.clairvoyant_expected_value()


def test_retry_uses_start_readiness_and_delays_receipt_until_action_end():
    config = default_experiment_config()
    run = run_policy_tape(
        config, _controlled_world(config, recovery=4), "fixed_retry", {"limit": 5}, approved=True
    )
    first, second = run["steps"][:2]
    assert first["receipt"]["start_s"] == 0 and first["receipt"]["end_s"] == 4
    assert first["receipt"]["success"] is False and first["receipt"]["released_delta"] == 0
    assert second["receipt"]["start_s"] == 4 and second["receipt"]["end_s"] == 8
    assert second["receipt"]["success"] is True and second["receipt"]["released_delta"] == 1
    assert [o["time_ticks"] for o in second["public_history"]["observations"]] == [0, 2]
    assert run["terminal"]["release_count"] == 3
    assert run["terminal"]["elapsed_s"] == 16
    assert run["terminal"]["utility"] == 23.6


def test_rules_deadline_scope_and_budget_are_not_policy_approval():
    scope = {"kind": "bounded_synthetic_subsystem", "simulation_approved": True}
    at_limit = PublicBudget(10, 4, 2, 12, 5, 3)
    verdict = constrain_proposal(ActionProposal("retry", "fixture"), at_limit, scope)
    assert verdict["allowed"] and verdict["duration_ticks"] == 2
    overdue = constrain_proposal(
        ActionProposal("retry", "fixture"), PublicBudget(11, 4, 2, 12, 5, 3), scope
    )
    assert not overdue["allowed"] and "completion_exceeds_return_deadline" in overdue["reasons"]
    depleted = constrain_proposal(
        ActionProposal("retry", "fixture"), PublicBudget(4, 5, 1, 12, 5, 3), scope
    )
    assert not depleted["allowed"] and "retry_budget_exhausted" in depleted["reasons"]
    denied = constrain_proposal(
        ActionProposal("retry", "fixture"),
        at_limit,
        {"kind": "bounded_synthetic_subsystem", "simulation_approved": False},
    )
    assert not denied["allowed"] and "simulation_scope_not_approved" in denied["reasons"]


@pytest.mark.parametrize(
    "policy,parameters",
    [
        ("abort", {}),
        ("fixed_retry", {"limit": 5}),
        ("periodic_retry", {"wait_ticks": 1}),
        ("history_rule", {"posterior_ready_threshold": 0.25}),
        ("finite_model_optimal", {}),
    ],
)
def test_unobserved_future_and_world_identity_cannot_change_first_proposal(policy, parameters):
    config = default_experiment_config()
    first = _controlled_world(config, recovery=8)
    second = _controlled_world(config, recovery=None)
    second["world_id"] = "distinct-hidden-world-identity"
    for observation in second["sensor_tape"][1:]:
        observation["healthy"] = True
    second["tape_sha256"] = digest(
        {key: value for key, value in second.items() if key != "tape_sha256"}
    )
    a = run_policy_tape(config, first, policy, parameters, approved=True)
    b = run_policy_tape(config, second, policy, parameters, approved=True)
    assert a["steps"][0]["public_history"] == b["steps"][0]["public_history"]
    assert a["steps"][0]["public_budget"] == b["steps"][0]["public_budget"]
    assert a["steps"][0]["proposal"] == b["steps"][0]["proposal"]


def test_skipped_sensor_ticks_stay_unobserved_despite_counterfactual_values():
    config = default_experiment_config()
    first = _controlled_world(config, recovery=8)
    second = deepcopy(first)
    for observation in second["sensor_tape"]:
        if observation["time_ticks"] % 2:
            observation["healthy"] = not observation["healthy"]
    second["tape_sha256"] = digest(
        {key: value for key, value in second.items() if key != "tape_sha256"}
    )
    a = run_policy_tape(config, first, "fixed_retry", {"limit": 5}, approved=True)
    b = run_policy_tape(config, second, "fixed_retry", {"limit": 5}, approved=True)
    assert a["steps"] == b["steps"] and a["terminal"] == b["terminal"]
    assert all(
        observation["time_ticks"] % 2 == 0
        for step in a["steps"]
        for observation in step["public_history"]["observations"]
    )


def test_proposal_boundary_has_only_public_dataclasses(monkeypatch):
    import src.runtime.starship_dispenser_experiment as module

    original = module.propose_action
    seen = []

    def audited(policy, history, budget, parameters, solver):
        assert set(history.__dataclass_fields__) == {"observations", "retry_results"}
        assert set(budget.__dataclass_fields__) == {
            "time_ticks",
            "attempts",
            "released",
            "deadline_ticks",
            "max_attempts",
            "payload_count",
            "retry_ticks",
            "wait_ticks",
            "tick_s",
        }
        assert "seed" not in solver.model and "sensor_tape" not in solver.model
        assert all(
            observation.time_ticks <= budget.time_ticks for observation in history.observations
        )
        seen.append((history, budget))
        return original(policy, history, budget, parameters, solver)

    monkeypatch.setattr(module, "propose_action", audited)
    config = default_experiment_config()
    run_policy_tape(config, _controlled_world(config), "finite_model_optimal", approved=True)
    assert seen


def test_retry_receipt_is_information_distinct_from_end_sensor():
    solver = FiniteModelSolver(default_experiment_config())
    history = PublicHistory(
        (SensorObservation(0, False), SensorObservation(4, True)), (RetryObservation(2, 4, False),)
    )
    posterior = solver.posterior(history)
    assert posterior[0] == 0  # Failure at start=4 seconds excludes recovery=4.
    assert posterior[1] > 0  # Recovery at 8 seconds can explain the healthy end sensor.


def test_complete_study_has_common_worlds_frozen_train_selection_and_exact_bounds(study):
    before = digest(study)
    verdict = verify_dispenser_experiment(study)
    assert verdict["verified"], verdict
    assert verdict["runs_checked"] == 870
    assert digest(study) == before
    assert (
        not verdict["input_authentication_performed"] and not verdict["physical_execution_verified"]
    )
    assert len(study["worlds"]) == 90
    assert {world["seed"] for world in study["worlds"] if world["split"] == "train"}.isdisjoint(
        {world["seed"] for world in study["worlds"] if world["split"] == "eval"}
    )
    for world in study["worlds"]:
        matching = [run for run in study["policy_runs"] if run["world_id"] == world["world_id"]]
        assert len(matching) == 5 and {run["tape_sha256"] for run in matching} == {
            world["tape_sha256"]
        }
    assert all(tuning["selection_split"] == "train" for tuning in study["tuning"].values())
    assert study["model_bounds"]["best_nonoptimal_policy"] == "history_rule"
    gap = study["model_bounds"]["optimal_minus_best_nonoptimal_expected_utility"]
    assert 0 < gap["value"] < 0.13
    assert not study["claim_boundary"]["llm_invoked"]


def _first_nonabort(study):
    return next(run for run in study["policy_runs"] if run["policy"] == "fixed_retry")


def _corrupt(study, mutation):
    run = _first_nonabort(study)
    step = run["steps"][0]
    if mutation == "scope":
        run["scope"]["simulation_approved"] = False
    elif mutation == "identity_claim":
        run["scope"]["authenticated_operator_identity"] = True
    elif mutation == "rules":
        step["rules"]["allowed"] = 1
    elif mutation == "rules_duration":
        step["rules"]["duration_ticks"] = 1
    elif mutation == "receipt_early":
        step["receipt"]["end_ticks"] = 1
    elif mutation == "release_before_ready":
        step["receipt"]["success"] = True
        step["receipt"]["released_delta"] = 1
    elif mutation == "false_count_type":
        run["terminal"]["attempt_count"] = float(run["terminal"]["attempt_count"])
    elif mutation == "clock":
        step["public_budget"]["time_ticks"] = 1
    elif mutation == "future_input":
        step["public_history"]["recovery_time_s"] = 4
    elif mutation == "future_observation":
        step["public_history"]["observations"].append({"time_ticks": 1, "healthy": True})
    elif mutation == "observation":
        step["observation"]["healthy"] = not step["observation"]["healthy"]
    elif mutation == "step_order":
        run["steps"][0], run["steps"][1] = run["steps"][1], run["steps"][0]
    elif mutation == "truncated":
        run["steps"].pop()
    elif mutation == "post_terminal":
        run["steps"].append(deepcopy(run["steps"][-1]))
    elif mutation == "world_tape":
        study["worlds"][0]["sensor_tape"][1]["healthy"] = not study["worlds"][0]["sensor_tape"][1][
            "healthy"
        ]
        study["worlds"][0]["tape_sha256"] = digest(
            {k: v for k, v in study["worlds"][0].items() if k != "tape_sha256"}
        )
    elif mutation == "common_world_binding":
        run["tape_sha256"] = study["worlds"][1]["tape_sha256"]
    elif mutation == "training_leak":
        study["training_candidate_runs"][0] = deepcopy(
            next(
                run
                for run in study["policy_runs"]
                if run["split"] == "eval" and run["policy"] == "fixed_retry"
            )
        )
    elif mutation == "tuning_reselected":
        study["tuning"]["history_rule"]["selected_parameters"]["posterior_ready_threshold"] = 0.75
    elif mutation == "summary":
        study["summary"]["eval"]["history_rule"]["mean_utility"] += 0.1
    elif mutation == "duplicate_run":
        study["policy_runs"][1] = deepcopy(study["policy_runs"][0])
    elif mutation == "nonfinite":
        run["terminal"]["utility"] = float("nan")
    elif mutation == "clairvoyant_label":
        study["policy_runs"][4] = deepcopy(study["clairvoyant_runs"][0])
        study["policy_runs"][4]["policy"] = "finite_model_optimal"
    elif mutation == "bound":
        study["model_bounds"]["finite_model_optimal_expected_utility"]["value"] += 1
    elif mutation == "gap":
        study["model_bounds"]["optimal_minus_best_nonoptimal_expected_utility"]["value"] = 10.0
    elif mutation == "physical_claim":
        study["claim_boundary"]["physical_execution"] = True
    elif mutation == "bool_config":
        study["config"]["retry_cost"] = True
        study["config_sha256"] = digest(study["config"])
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        "scope",
        "identity_claim",
        "rules",
        "rules_duration",
        "receipt_early",
        "release_before_ready",
        "false_count_type",
        "clock",
        "future_input",
        "future_observation",
        "observation",
        "step_order",
        "truncated",
        "post_terminal",
        "world_tape",
        "common_world_binding",
        "training_leak",
        "tuning_reselected",
        "summary",
        "duplicate_run",
        "nonfinite",
        "clairvoyant_label",
        "bound",
        "gap",
        "physical_claim",
        "bool_config",
    ],
)
def test_verifier_rejects_tampering_with_boundaries_and_results(study, mutation):
    corrupted = deepcopy(study)
    _corrupt(corrupted, mutation)
    verdict = verify_dispenser_experiment(corrupted)
    assert not verdict["verified"], mutation
    assert verdict["reasons"]


@pytest.mark.parametrize("value", [None, [], 1, True, {"schema": "wrong"}])
def test_verifier_fails_closed_on_malformed_top_level(value):
    assert not verify_dispenser_experiment(value)["verified"]


@pytest.mark.parametrize(
    "key,value", [("retry_cost", True), ("tick_s", 2.0), ("deadline_ticks", True)]
)
def test_configuration_types_are_not_numeric_coercions(key, value):
    config = default_experiment_config()
    config[key] = value
    with pytest.raises(ValueError):
        validate_config(config, frozen=True)
    with pytest.raises(ValueError):
        FiniteModelSolver(config)
    with pytest.raises(ValueError):
        run_dispenser_experiment(config, approved=True)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("llm_invoked", True),
        ("physical_execution", True),
        ("backend", "physical_spacecraft"),
        ("schema", "unrecognized"),
        ("llm_invoked", 0),
    ],
)
def test_provenance_cannot_contradict_bounded_claims(study, field, bad):
    modified = deepcopy(study)
    modified["provenance"] = {
        "schema": "missionos.synthetic_dispenser_provenance.v1",
        "backend": "local_in_process_synthetic_fixture",
        "source_hashes_bind_working_files": True,
        "llm_invoked": False,
        "physical_execution": False,
    }
    assert verify_dispenser_experiment(modified)["verified"]
    modified["provenance"][field] = bad
    assert not verify_dispenser_experiment(modified)["verified"]


def test_simulation_opt_in_and_clairvoyant_labels_are_enforced():
    config = default_experiment_config()
    world = make_world(config, "train", "short", 101)
    with pytest.raises(PermissionError):
        run_dispenser_experiment()
    with pytest.raises(PermissionError):
        run_policy_tape(config, world, "abort")
    with pytest.raises(ValueError):
        run_policy_tape(config, world, "finite_model_optimal", approved=True, clairvoyant=True)
    with pytest.raises(ValueError):
        run_policy_tape(config, world, "clairvoyant_reference", approved=True)
    with pytest.raises(ValueError):
        run_policy_tape(config, world, "fixed_retry", {"limit": True}, approved=True)


def test_smaller_solver_fixture_does_not_change_full_experiment_contract():
    config = default_experiment_config()
    config["deadline_ticks"] = 4
    assert FiniteModelSolver(config).initial_value() >= 0
    with pytest.raises(ValueError):
        run_dispenser_experiment(config, approved=True)
