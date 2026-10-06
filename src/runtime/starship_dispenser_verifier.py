"""Independent record accounting for the bounded synthetic dispenser experiment.

This verifier walks receipts and sampled observations without rerunning the
executor. It separately reproduces admissible policy choices from public data;
the Bellman implementation is reused only for that declared policy check.
Content hashes establish bindings, not operator identity or physical truth.
"""

from __future__ import annotations

from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
import json
import math
import random

from .starship_dispenser_experiment import (
    FiniteModelSolver,
    PublicBudget,
    PublicHistory,
    RetryObservation,
    SensorObservation,
)


FROZEN_CONFIG_SHA256 = "fcda770c815266e714aa06f5876b81a21338be3dd388e7763f42404dc3ea7018"
GROUPS = ("short", "late", "never")
POLICIES = ("abort", "fixed_retry", "periodic_retry", "history_rule", "finite_model_optimal")
EXACT_FINITE_VALUE = Fraction(12417893326807, 1280000000000)
EXACT_CLAIRVOYANT_VALUE = Fraction(23, 2)


def _hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _require(condition: bool, name: str) -> None:
    if not condition:
        raise ValueError(name)


def _equal(actual: object, expected: object, name: str) -> None:
    """Exact structure/types, with a small tolerance only for displayed floats."""
    _require(type(actual) is type(expected), f"{name}: type mismatch")
    if isinstance(expected, dict):
        _require(set(actual) == set(expected), f"{name}: fields mismatch")
        for key, value in expected.items():
            _equal(actual[key], value, f"{name}.{key}")
    elif isinstance(expected, list):
        _require(len(actual) == len(expected), f"{name}: length mismatch")
        for index, (first, second) in enumerate(zip(actual, expected)):
            _equal(first, second, f"{name}[{index}]")
    elif isinstance(expected, float):
        _require(
            math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12),
            f"{name}: numeric mismatch",
        )
    else:
        _require(actual == expected, f"{name}: value mismatch")


def _list(value: object, name: str) -> list:
    _require(type(value) is list, f"{name}: expected list")
    return value


def _object(value: object, name: str) -> dict:
    _require(type(value) is dict, f"{name}: expected object")
    return value


def _fraction(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator, "value": float(value)}


def _world(split: str, group: str, seed: int) -> dict:
    """Rebuild absolute-time exogenous input from the declared stream contract."""

    def stream(kind: str) -> random.Random:
        encoded = json.dumps([split, group, seed, kind], separators=(",", ":")).encode()
        return random.Random(int.from_bytes(sha256(encoded).digest(), "big"))

    support = {"short": (4, 8), "late": (14, 18), "never": (None,)}[group]
    recovery = stream("recovery").choice(support)
    sensor = stream("sensor")
    tape = []
    for tick in range(13):
        ready = recovery is not None and recovery <= 2 * tick
        tape.append(
            {
                "time_ticks": tick,
                "time_s": tick * 2,
                "healthy": sensor.randrange(20) < (16 if ready else 3),
            }
        )
    world = {
        "world_id": f"synthetic-dispenser-{split}-{group}-{seed}",
        "split": split,
        "group": group,
        "seed": seed,
        "recovery_time_s": recovery,
        "sensor_tape": tape,
        "generation_scope": "executor/verifier only; never supplied to a policy",
    }
    return {**world, "tape_sha256": _hash(world)}


def _terminal_reason(time: int, attempts: int, released: int) -> str | None:
    if released == 3:
        return "all_fixture_payloads_released"
    if time == 12:
        return "return_deadline"
    if attempts == 5:
        return "retry_budget_exhausted"
    return None


def _policy_action(
    policy: str,
    parameters: dict,
    observations: list,
    retries: list,
    budget: dict,
    solver: FiniteModelSolver,
    recovery: int | None,
) -> str:
    if policy != "clairvoyant_reference" and budget["time_ticks"] + 2 > 12:
        return "abort"
    if policy == "abort":
        return "abort"
    if policy == "fixed_retry":
        return "retry" if budget["attempts"] < parameters["limit"] else "abort"
    if policy == "periodic_retry":
        last_end = retries[-1]["end_ticks"] if retries else 0
        return "wait" if budget["time_ticks"] < last_end + parameters["wait_ticks"] else "retry"
    # These typed objects contain neither world identity nor unobserved tape.
    history = PublicHistory(
        tuple(SensorObservation(**row) for row in observations),
        tuple(RetryObservation(**row) for row in retries),
    )
    if policy == "clairvoyant_reference":
        index = [4, 8, 14, 18, None].index(recovery)
        belief = tuple(int(i == index) for i in range(5))
    else:
        belief = solver.posterior(history)
    if policy == "history_rule":
        threshold = Fraction(str(parameters["posterior_ready_threshold"]))
        return (
            "retry"
            if solver.ready_probability(belief, budget["time_ticks"]) >= threshold
            else "wait"
        )
    return solver.best_action(PublicBudget(**budget), belief)


def _walk_run(
    run: dict,
    world: dict,
    policy: str,
    parameters: dict,
    config_hash: str,
    solver: FiniteModelSolver,
) -> dict:
    """Validate the state machine and receipts without calling the executor."""
    _object(run, "run")
    expected_keys = {
        "schema",
        "world_id",
        "split",
        "policy",
        "parameters",
        "tape_sha256",
        "scope",
        "initial_observation",
        "steps",
        "terminal",
    }
    _require(set(run) == expected_keys, "run: fields mismatch")
    for key, expected in {
        "schema": "missionos.synthetic_dispenser_run.v1",
        "world_id": world["world_id"],
        "split": world["split"],
        "policy": policy,
        "parameters": parameters,
        "tape_sha256": world["tape_sha256"],
    }.items():
        _equal(run[key], expected, f"run.{key}")
    scope = {
        "schema": "missionos.synthetic_dispenser_scope.v1",
        "kind": "bounded_synthetic_subsystem",
        "simulation_approved": True,
        "authenticated_operator_identity": False,
        "config_sha256": config_hash,
        "physical_execution_authorized": False,
    }
    _equal(run["scope"], scope, "run.scope")
    observed = [{"time_ticks": 0, "healthy": world["sensor_tape"][0]["healthy"]}]
    _equal(run["initial_observation"], {**observed[0], "time_s": 0}, "run.initial_observation")
    retries = []
    time = attempts = released = 0
    termination = None
    steps = _list(run["steps"], "run.steps")
    _require(1 <= len(steps) <= 13, "run.steps: invalid bounded record count")
    reasons = {
        "abort": "predeclared immediate abort comparator",
        "fixed_retry": "training-selected consecutive retry limit",
        "periodic_retry": "training-selected fixed wait before every retry",
        "history_rule": "training-selected posterior-ready threshold",
        "finite_model_optimal": "exact finite-model Bellman expected utility",
        "clairvoyant_reference": "clairvoyant reference; realized recovery time known",
    }
    for index, record in enumerate(steps):
        _require(
            termination is None and _terminal_reason(time, attempts, released) is None,
            "run.steps: record after termination",
        )
        _object(record, "step")
        _require(
            set(record)
            == {
                "index",
                "public_budget",
                "public_history",
                "proposal",
                "rules",
                "receipt",
                "observation",
            },
            "step: fields mismatch",
        )
        _equal(record["index"], index, "step.index")
        budget = {
            "time_ticks": time,
            "attempts": attempts,
            "released": released,
            "deadline_ticks": 12,
            "max_attempts": 5,
            "payload_count": 3,
            "retry_ticks": 2,
            "wait_ticks": 1,
            "tick_s": 2,
        }
        _equal(record["public_budget"], budget, "step.public_budget")
        _equal(
            record["public_history"],
            {"observations": observed, "retry_results": retries},
            "step.public_history",
        )
        action = _policy_action(
            policy,
            parameters,
            observed,
            retries,
            budget,
            solver,
            world["recovery_time_s"] if policy == "clairvoyant_reference" else None,
        )
        proposal_reason = (
            "insufficient remaining time to complete another retry"
            if policy != "clairvoyant_reference" and time + 2 > 12
            else reasons[policy]
        )
        _equal(
            record["proposal"],
            {
                "action": action,
                "reason": proposal_reason,
                "schema": "missionos.synthetic_dispenser_proposal.v1",
            },
            "step.proposal",
        )
        duration = {"abort": 0, "wait": 1, "retry": 2}[action]
        blocked = ["completion_exceeds_return_deadline"] if time + duration > 12 else []
        rules = {
            "schema": "missionos.synthetic_dispenser_rules.v1",
            "allowed": not blocked,
            "reasons": blocked,
            "requested_action": action,
            "duration_ticks": duration,
            "time_ticks": time,
            "deadline_ticks": 12,
        }
        _equal(record["rules"], rules, "step.rules")
        if blocked:
            _equal(record["receipt"], None, "step.blocked_receipt")
            _equal(record["observation"], None, "step.blocked_observation")
            termination = "rules_blocked"
            continue
        end = time + duration
        attempted = action == "retry"
        success = (
            attempted
            and world["recovery_time_s"] is not None
            and world["recovery_time_s"] <= time * 2
        )
        next_attempts = attempts + int(attempted)
        next_released = released + int(success)
        receipt = {
            "schema": "missionos.synthetic_dispenser_receipt.v1",
            "action": action,
            "start_ticks": time,
            "end_ticks": end,
            "start_s": 2 * time,
            "end_s": 2 * end,
            "attempted": attempted,
            "success": bool(success) if attempted else None,
            "released_delta": int(success),
            "release_count_after": next_released,
            "attempt_count_after": next_attempts,
            "execution_kind": "synthetic_subsystem",
            "physical_execution": False,
        }
        _equal(record["receipt"], receipt, "step.receipt")
        if action == "abort":
            _equal(record["observation"], None, "step.abort_observation")
            termination = "aborted"
            continue
        if attempted:
            retries = retries + [{"start_ticks": time, "end_ticks": end, "success": bool(success)}]
        latest = {"time_ticks": end, "healthy": world["sensor_tape"][end]["healthy"]}
        _equal(record["observation"], {**latest, "time_s": 2 * end}, "step.observation")
        observed = observed + [latest]
        time, attempts, released = end, next_attempts, next_released
    termination = termination or _terminal_reason(time, attempts, released)
    _require(termination is not None, "run: stopped before a terminal condition")
    utility = Fraction(10 * released - attempts) - Fraction(3, 20) * time * 2
    terminal = {
        "release_count": released,
        "payload_count": 3,
        "elapsed_s": time * 2,
        "deadline_s": 24,
        "margin_s": 24 - time * 2,
        "attempt_count": attempts,
        "utility": float(utility),
        "utility_exact": _fraction(utility),
        "reason": termination,
        "all_fixture_payloads_released": released == 3,
        "fuel_unmodeled": True,
        "physical_execution": False,
        "starlink_service_verified": False,
    }
    _equal(run["terminal"], terminal, "run.terminal")
    return terminal


def _summary(runs: list[dict], group_by_id: dict[str, str]) -> dict:
    groups = {
        group: [run for run in runs if group_by_id[run["world_id"]] == group] for group in GROUPS
    }
    _require(all(groups.values()), "summary: empty stratum")
    output = {
        "run_count": len(runs),
        "stratum_counts": {key: len(rows) for key, rows in groups.items()},
        "weighting": "equal 1/3 per declared stratum",
    }
    fields = {
        "mean_utility": "utility",
        "mean_release_count": "release_count",
        "full_release_fraction": "all_fixture_payloads_released",
        "mean_elapsed_s": "elapsed_s",
        "mean_attempt_count": "attempt_count",
        "mean_margin_s": "margin_s",
    }
    for name, field in fields.items():
        means = []
        for rows in groups.values():
            numbers = [
                Fraction(
                    run["terminal"]["utility_exact"]["numerator"],
                    run["terminal"]["utility_exact"]["denominator"],
                )
                if field == "utility"
                else Fraction(int(run["terminal"][field]))
                for run in rows
            ]
            means.append(sum(numbers, Fraction(0)) / len(rows))
        output[name] = float(sum(means, Fraction(0)) / 3)
    return output


def _group_utility(runs: list[dict], groups: dict[str, str]) -> Fraction:
    total = Fraction(0)
    for group in GROUPS:
        rows = [run for run in runs if groups[run["world_id"]] == group]
        total += sum(
            (
                Fraction(
                    run["terminal"]["utility_exact"]["numerator"],
                    run["terminal"]["utility_exact"]["denominator"],
                )
                for run in rows
            ),
            Fraction(0),
        ) / (3 * len(rows))
    return total


def _expected_comparator(policy: str, parameters: dict) -> Fraction:
    """Independent rational expectation of the four non-Bellman comparators."""
    recovery = (2, 4, 7, 9, None)

    def ready(index: int, tick: int) -> bool:
        return recovery[index] is not None and recovery[index] <= tick

    @lru_cache(maxsize=None)
    def future(tick: int, attempts: int, released: int, weights: tuple, last_end: int) -> Fraction:
        if _terminal_reason(tick, attempts, released) is not None or tick + 2 > 12:
            return Fraction(0)
        if policy == "abort" or (policy == "fixed_retry" and attempts >= parameters["limit"]):
            return Fraction(0)
        if policy == "fixed_retry":
            retry = True
        elif policy == "periodic_retry":
            retry = tick >= last_end + parameters["wait_ticks"]
        else:
            chance = Fraction(sum(w for i, w in enumerate(weights) if ready(i, tick)), sum(weights))
            retry = chance >= Fraction(str(parameters["posterior_ready_threshold"]))
        end = tick + (2 if retry else 1)
        value = -Fraction(3, 10) * (end - tick) - int(retry)
        for success in (False, True) if retry else (False,):
            subset = [
                w if not retry or ready(i, tick) == success else 0 for i, w in enumerate(weights)
            ]
            for signal in (False, True):
                products = []
                for i, weight in enumerate(subset):
                    numerator = 16 if ready(i, end) else 3
                    products.append(weight * (numerator if signal else 20 - numerator))
                if not sum(products):
                    continue
                divisor = math.gcd(*products)
                belief = tuple(weight // divisor for weight in products)
                probability = Fraction(sum(products), 20 * sum(weights))
                value += probability * (
                    10 * int(success)
                    + future(
                        end,
                        attempts + int(retry),
                        released + int(success),
                        belief,
                        end if retry else last_end,
                    )
                )
        return value

    return future(0, 0, 0, (1, 1, 1, 1, 2), 0)


def verify_dispenser_experiment(study: dict) -> dict:
    """Fail closed on changed worlds, hidden information, receipts, or accounting."""
    checked = 0
    try:
        _object(study, "study")
        _equal(study["schema"], "missionos.synthetic_dispenser_experiment.v1", "study.schema")
        config = _object(study["config"], "study.config")
        _require(_hash(config) == FROZEN_CONFIG_SHA256, "study.config: not the frozen experiment")
        _equal(study["config_sha256"], FROZEN_CONFIG_SHA256, "study.config_sha256")
        claims = {
            "bounded_synthetic_subsystem": True,
            "flight_physics_changed": False,
            "full_flight_rerun": False,
            "fuel_unmodeled": True,
            "llm_invoked": False,
            "physical_execution": False,
            "starship_vehicle_validated": False,
            "starlink_service_verified": False,
            "missionos_gateway_integration": False,
        }
        _equal(study["claim_boundary"], claims, "study.claim_boundary")
        if "provenance" in study:
            provenance = _object(study["provenance"], "study.provenance")
            for key, expected in {
                "schema": "missionos.synthetic_dispenser_provenance.v1",
                "backend": "local_in_process_synthetic_fixture",
                "source_hashes_bind_working_files": True,
                "llm_invoked": False,
                "physical_execution": False,
            }.items():
                _equal(provenance[key], expected, f"study.provenance.{key}")
        expected_worlds = [
            _world(split, group, seed)
            for split, seeds in (("train", range(101, 111)), ("eval", range(1001, 1021)))
            for group in GROUPS
            for seed in seeds
        ]
        _equal(study["worlds"], expected_worlds, "study.worlds")
        worlds = {world["world_id"]: world for world in expected_worlds}
        groups = {key: value["group"] for key, value in worlds.items()}
        solver = FiniteModelSolver(config)
        bounds = _object(study["model_bounds"], "study.model_bounds")
        _equal(
            bounds["finite_model_optimal_expected_utility"],
            _fraction(EXACT_FINITE_VALUE),
            "model_bounds.finite_model_optimal_expected_utility",
        )
        _equal(
            bounds["clairvoyant_expected_utility"],
            _fraction(EXACT_CLAIRVOYANT_VALUE),
            "model_bounds.clairvoyant_expected_utility",
        )
        for name, label in {
            "scope": "Expected utility within the declared finite law, observations, actions and objective; empirical evaluation averages are not oracle bounds",
            "clairvoyant_information": "Realized recovery time known; separate reference, not an admissible policy",
            "solver": "rational Bellman enumeration with exact integer posterior weights",
            "initial_condition": "known jam at t=0; initial sensor likelihood identical for all recovery hypotheses",
        }.items():
            _equal(bounds[name], label, f"model_bounds.{name}")
        selected = {"abort": {}, "finite_model_optimal": {}}
        training_runs = _list(study["training_candidate_runs"], "training_candidate_runs")
        expected_training = [
            (policy, parameters, world)
            for policy, parameters_list in config["tuning_candidates"].items()
            for parameters in parameters_list
            for world in expected_worlds
            if world["split"] == "train"
        ]
        _require(
            len(training_runs) == len(expected_training), "training_candidate_runs: count mismatch"
        )
        training_groups = {}
        for run, (policy, parameters, world) in zip(training_runs, expected_training):
            _walk_run(run, world, policy, parameters, FROZEN_CONFIG_SHA256, solver)
            checked += 1
            training_groups.setdefault((policy, _hash(parameters)), []).append(run)
        expected_tuning = {}
        for policy, candidates in config["tuning_candidates"].items():
            exact_scores = [
                _group_utility(training_groups[policy, _hash(parameters)], groups)
                for parameters in candidates
            ]
            scores = [
                {
                    "parameters": parameters,
                    "train_mean_utility": float(score),
                    "train_mean_utility_exact": _fraction(score),
                }
                for parameters, score in zip(candidates, exact_scores)
            ]
            winner = max(range(len(scores)), key=lambda index: exact_scores[index])
            selected[policy] = candidates[winner]
            expected_tuning[policy] = {
                "candidates": scores,
                "selected_parameters": candidates[winner],
                "selection_split": "train",
                "tie_break": "enumeration_order",
            }
        _equal(study["tuning"], expected_tuning, "study.tuning")
        expected_values = {
            policy: _expected_comparator(policy, selected[policy]) for policy in POLICIES[:-1]
        }
        expected_values["finite_model_optimal"] = EXACT_FINITE_VALUE
        best = max(POLICIES[:-1], key=lambda policy: expected_values[policy])
        _equal(
            bounds["policy_expected_utilities"],
            {policy: _fraction(value) for policy, value in expected_values.items()},
            "model_bounds.policy_expected_utilities",
        )
        _equal(bounds["best_nonoptimal_policy"], best, "model_bounds.best_nonoptimal_policy")
        _equal(
            bounds["optimal_minus_best_nonoptimal_expected_utility"],
            _fraction(EXACT_FINITE_VALUE - expected_values[best]),
            "model_bounds.comparator_gap",
        )
        for key, policies in (
            ("policy_runs", POLICIES),
            ("clairvoyant_runs", ("clairvoyant_reference",)),
        ):
            rows = _list(study[key], key)
            expected_runs = [(world, policy) for world in expected_worlds for policy in policies]
            _require(len(rows) == len(expected_runs), f"{key}: count mismatch")
            for run, (world, policy) in zip(rows, expected_runs):
                _walk_run(
                    run, world, policy, selected.get(policy, {}), FROZEN_CONFIG_SHA256, solver
                )
                checked += 1
        expected_summary = {
            split: {
                policy: _summary(
                    [
                        run
                        for run in study["policy_runs"]
                        if run["split"] == split and run["policy"] == policy
                    ],
                    groups,
                )
                for policy in POLICIES
            }
            for split in ("train", "eval")
        }
        expected_clairvoyant = {
            split: _summary(
                [run for run in study["clairvoyant_runs"] if run["split"] == split], groups
            )
            for split in ("train", "eval")
        }
        _equal(study["summary"], expected_summary, "study.summary")
        _equal(study["clairvoyant_summary"], expected_clairvoyant, "study.clairvoyant_summary")
        return {
            "verified": True,
            "reasons": [],
            "runs_checked": checked,
            "scope": "independent record walk, input bindings and terminal accounting; separate public-policy reproduction",
            "policy_reproduction_uses_frozen_bellman_solver": True,
            "physical_execution_verified": False,
            "input_authentication_performed": False,
        }
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError, OverflowError) as exc:
        return {
            "verified": False,
            "reasons": [str(exc)],
            "runs_checked": checked,
            "scope": "bounded synthetic record verification only",
            "physical_execution_verified": False,
            "input_authentication_performed": False,
        }
