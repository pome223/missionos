"""A bounded synthetic dispenser decision experiment, separate from flight physics.

Three fictional payloads, an irreversible hidden recovery time, and a noisy
status bit define the entire model. There is no spacecraft, fuel, satellite
service, LLM, or Flight 14 execution here. Policy proposals, scope, Rules,
executor receipts, and terminal accounting are recorded as separate facts.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
import json
from math import gcd
import random


POLICIES = ("abort", "fixed_retry", "periodic_retry", "history_rule", "finite_model_optimal")
GROUPS = ("short", "late", "never")
ACTION_ORDER = ("abort", "wait", "retry")


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def default_experiment_config() -> dict:
    """The preregistered finite study; baseline tuning uses training tapes only."""
    return {
        "schema": "missionos.synthetic_dispenser_config.v1",
        "tick_s": 2,
        "deadline_ticks": 12,
        "wait_ticks": 1,
        "retry_ticks": 2,
        "max_attempts": 5,
        "payload_count": 3,
        "initial_release_count": 0,
        "recovery_times_s": [4, 8, 14, 18, None],
        "prior_weights": [1, 1, 1, 1, 2],
        "sensor_denominator": 20,
        "healthy_likelihood_ready": 16,
        "healthy_likelihood_jammed": 3,
        "reward_per_payload": 10,
        "time_cost_numerator": 3,
        "time_cost_denominator": 20,
        "retry_cost": 1,
        "train_seeds": list(range(101, 111)),
        "eval_seeds": list(range(1001, 1021)),
        "stratum_weights": {"short": [1, 3], "late": [1, 3], "never": [1, 3]},
        "tuning_candidates": {
            "fixed_retry": [{"limit": n} for n in range(1, 6)],
            "periodic_retry": [{"wait_ticks": n} for n in (1, 2, 3)],
            "history_rule": [{"posterior_ready_threshold": p} for p in (0.25, 0.5, 0.75)],
        },
        "tuning_tie_break": "enumeration_order",
        "action_tie_break": list(ACTION_ORDER),
        "observation_schedule": "initial tick 0 and each executed action end; intermediate ticks are not observed",
        "retry_success_rule": "ready at retry start releases exactly one fictional payload at action end",
        "seed_rule": "sha256(split,group,seed,stream); separate recovery and sensor streams, never action-indexed",
    }


def validate_config(config: dict, *, frozen: bool = False) -> None:
    base = default_experiment_config()
    if not isinstance(config, dict) or set(config) != set(base):
        raise ValueError("Use the declared synthetic dispenser configuration schema")
    try:
        config_hash = digest(config)
    except (TypeError, ValueError) as exc:
        raise ValueError("Configuration must contain finite JSON-compatible values") from exc
    if frozen and config_hash != digest(base):
        raise ValueError("Full experiments use the frozen preregistered configuration")
    variable = {"deadline_ticks": 12, "payload_count": 3, "max_attempts": 5}
    for key, maximum in variable.items():
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f"{key} must be a positive integer no larger than {maximum}")
    if digest({key: config[key] for key in base if key not in variable}) != digest(
        {key: base[key] for key in base if key not in variable}
    ):
        raise ValueError(
            "Only smaller bounded horizons/payload/attempt counts are supported by solver helpers"
        )


def _canonical(weights: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    divisor = 0
    for weight in weights:
        if type(weight) is not int or weight < 0:
            raise ValueError("Belief weights must be nonnegative integers")
        divisor = gcd(divisor, weight)
    if not divisor:
        raise ValueError("An impossible history cannot define a posterior")
    return tuple(weight // divisor for weight in weights)


def _validate_parameters(config: dict, policy: str, parameters: dict | None) -> dict:
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict):
        raise ValueError("Policy parameters must be a JSON object")
    try:
        parameter_hash = digest(parameters)
    except (TypeError, ValueError) as exc:
        raise ValueError("Policy parameters must contain finite JSON-compatible values") from exc
    candidates = config["tuning_candidates"].get(policy, [{}])
    if parameter_hash not in {digest(candidate) for candidate in candidates}:
        raise ValueError(
            "Policy parameters must exactly match a preregistered candidate; untuned policies require {}"
        )
    return deepcopy(parameters)


@dataclass(frozen=True)
class SensorObservation:
    time_ticks: int
    healthy: bool


@dataclass(frozen=True)
class RetryObservation:
    start_ticks: int
    end_ticks: int
    success: bool


@dataclass(frozen=True)
class PublicHistory:
    observations: tuple[SensorObservation, ...]
    retry_results: tuple[RetryObservation, ...] = ()


@dataclass(frozen=True)
class PublicBudget:
    time_ticks: int
    attempts: int
    released: int
    deadline_ticks: int
    max_attempts: int
    payload_count: int
    retry_ticks: int = 2
    wait_ticks: int = 1
    tick_s: int = 2


@dataclass(frozen=True)
class ActionProposal:
    action: str
    reason: str
    schema: str = "missionos.synthetic_dispenser_proposal.v1"

    def __post_init__(self) -> None:
        if self.action not in ACTION_ORDER:
            raise ValueError("Proposal action must be abort, wait, or retry")


class FiniteModelSolver:
    """Exact rational finite Bellman solver; never receives a realized world.

    Helpers permit only a smaller horizon, attempt budget or payload count for
    independent enumeration tests. Full experiments require the frozen config.
    Beliefs are integer likelihood weights reduced by gcd; values are Fractions.
    """

    def __init__(self, config: dict):
        validate_config(config)
        # Experimental identifiers and seeds are deliberately not stored here.
        keys = (
            "tick_s",
            "deadline_ticks",
            "wait_ticks",
            "retry_ticks",
            "max_attempts",
            "payload_count",
            "recovery_times_s",
            "prior_weights",
            "sensor_denominator",
            "healthy_likelihood_ready",
            "healthy_likelihood_jammed",
            "reward_per_payload",
            "time_cost_numerator",
            "time_cost_denominator",
            "retry_cost",
        )
        self.model = {key: deepcopy(config[key]) for key in keys}
        self.prior = _canonical(tuple(config["prior_weights"]))
        self.time_cost = Fraction(config["time_cost_numerator"], config["time_cost_denominator"])
        self._values = lru_cache(maxsize=None)(self._uncached_action_values)

    def ready(self, recovery_index: int, time_ticks: int) -> bool:
        recovery = self.model["recovery_times_s"][recovery_index]
        return recovery is not None and recovery <= time_ticks * self.model["tick_s"]

    def observe(self, weights: tuple[int, ...], time_ticks: int, healthy: bool) -> tuple[int, ...]:
        nums = []
        for index, weight in enumerate(weights):
            p = (
                self.model["healthy_likelihood_ready"]
                if self.ready(index, time_ticks)
                else self.model["healthy_likelihood_jammed"]
            )
            nums.append(weight * (p if healthy else self.model["sensor_denominator"] - p))
        return _canonical(nums)

    def posterior(self, history: PublicHistory) -> tuple[int, ...]:
        weights = self.prior
        events = [(obs.time_ticks, 1, obs) for obs in history.observations]
        events.extend((result.end_ticks, 0, result) for result in history.retry_results)
        for _, kind, event in sorted(events, key=lambda row: row[:2]):
            if kind == 0:
                weights = _canonical(
                    tuple(
                        w if self.ready(i, event.start_ticks) == event.success else 0
                        for i, w in enumerate(weights)
                    )
                )
            else:
                weights = self.observe(weights, event.time_ticks, event.healthy)
        return weights

    def ready_probability(self, weights: tuple[int, ...], time_ticks: int) -> Fraction:
        return Fraction(
            sum(w for i, w in enumerate(weights) if self.ready(i, time_ticks)), sum(weights)
        )

    def _terminal(self, time_ticks: int, attempts: int, released: int) -> bool:
        return (
            time_ticks >= self.model["deadline_ticks"]
            or attempts >= self.model["max_attempts"]
            or released >= self.model["payload_count"]
        )

    def _transitions(self, time_ticks: int, weights: tuple[int, ...], action: str):
        duration = self.model["retry_ticks"] if action == "retry" else self.model["wait_ticks"]
        end = time_ticks + duration
        denom = sum(weights) * self.model["sensor_denominator"]
        outcomes = (False, True) if action == "retry" else (False,)
        for success in outcomes:
            for healthy in (False, True):
                numerators = []
                for index, weight in enumerate(weights):
                    included = action != "retry" or self.ready(index, time_ticks) == success
                    p = (
                        self.model["healthy_likelihood_ready"]
                        if self.ready(index, end)
                        else self.model["healthy_likelihood_jammed"]
                    )
                    likelihood = p if healthy else self.model["sensor_denominator"] - p
                    numerators.append(weight * likelihood if included else 0)
                total = sum(numerators)
                if total:
                    yield Fraction(total, denom), _canonical(numerators), success

    def _uncached_action_values(
        self, time_ticks: int, attempts: int, released: int, weights: tuple[int, ...]
    ) -> tuple[tuple[str, Fraction], ...]:
        values = [("abort", Fraction(0))]
        if self._terminal(time_ticks, attempts, released):
            return tuple(values)
        for action in ("wait", "retry"):
            duration = self.model["retry_ticks"] if action == "retry" else self.model["wait_ticks"]
            if time_ticks + duration > self.model["deadline_ticks"]:
                continue
            cost = duration * self.model["tick_s"] * self.time_cost
            if action == "retry":
                cost += self.model["retry_cost"]
            expected = -cost
            for probability, posterior, success in self._transitions(time_ticks, weights, action):
                reward = self.model["reward_per_payload"] if success else 0
                future = self.value(
                    time_ticks + duration,
                    attempts + (action == "retry"),
                    released + success,
                    posterior,
                )
                expected += probability * (reward + future)
            values.append((action, expected))
        return tuple(values)

    def action_values(
        self, time_ticks: int, attempts: int, released: int, weights: tuple[int, ...]
    ) -> dict[str, Fraction]:
        if len(weights) != len(self.prior):
            raise ValueError("Belief dimension differs from the five declared recovery hypotheses")
        if any(type(v) is not int or v < 0 for v in (time_ticks, attempts, released)):
            raise ValueError("Finite-state indices must be nonnegative integers")
        if (
            time_ticks > self.model["deadline_ticks"]
            or attempts > self.model["max_attempts"]
            or released > self.model["payload_count"]
        ):
            raise ValueError("State exceeds the declared finite budget")
        return dict(self._values(time_ticks, attempts, released, _canonical(weights)))

    def value(
        self, time_ticks: int, attempts: int, released: int, weights: tuple[int, ...]
    ) -> Fraction:
        return max(self.action_values(time_ticks, attempts, released, weights).values())

    def best_action(self, budget: PublicBudget, weights: tuple[int, ...]) -> str:
        values = self.action_values(budget.time_ticks, budget.attempts, budget.released, weights)
        return max(ACTION_ORDER, key=lambda action: values.get(action, Fraction(-(10**9))))

    def initial_value(self) -> Fraction:
        # All hypotheses are jammed at t=0; either initial signal has the same posterior.
        return self.value(0, 0, 0, self.prior)

    def clairvoyant_expected_value(self) -> Fraction:
        return sum(
            (
                Fraction(weight, sum(self.prior))
                * self.value(0, 0, 0, tuple(int(i == index) for i in range(len(self.prior))))
                for index, weight in enumerate(self.prior)
            ),
            Fraction(0),
        )


def propose_action(
    policy: str,
    history: PublicHistory,
    budget: PublicBudget,
    parameters: dict,
    solver: FiniteModelSolver,
) -> ActionProposal:
    """Only public history/budget and the declared prior enter this boundary."""
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    if budget.time_ticks + budget.retry_ticks > budget.deadline_ticks:
        return ActionProposal("abort", "insufficient remaining time to complete another retry")
    if policy == "abort":
        return ActionProposal("abort", "predeclared immediate abort comparator")
    if policy == "fixed_retry":
        return ActionProposal(
            "retry" if budget.attempts < parameters["limit"] else "abort",
            "training-selected consecutive retry limit",
        )
    if policy == "periodic_retry":
        last_end = history.retry_results[-1].end_ticks if history.retry_results else 0
        return ActionProposal(
            "wait" if budget.time_ticks < last_end + parameters["wait_ticks"] else "retry",
            "training-selected fixed wait before every retry",
        )
    weights = solver.posterior(history)
    if policy == "history_rule":
        threshold = Fraction(str(parameters["posterior_ready_threshold"]))
        action = (
            "retry" if solver.ready_probability(weights, budget.time_ticks) >= threshold else "wait"
        )
        return ActionProposal(action, "training-selected posterior-ready threshold")
    return ActionProposal(
        solver.best_action(budget, weights), "exact finite-model Bellman expected utility"
    )


def constrain_proposal(proposal: ActionProposal, budget: PublicBudget, scope: dict) -> dict:
    """Rules check scope/time/attempt/release budgets, independently of policy reason."""
    reasons = []
    if (
        scope.get("kind") != "bounded_synthetic_subsystem"
        or scope.get("simulation_approved") is not True
    ):
        reasons.append("simulation_scope_not_approved")
    duration = (
        budget.retry_ticks
        if proposal.action == "retry"
        else (budget.wait_ticks if proposal.action == "wait" else 0)
    )
    if budget.time_ticks + duration > budget.deadline_ticks:
        reasons.append("completion_exceeds_return_deadline")
    if proposal.action == "retry" and budget.attempts >= budget.max_attempts:
        reasons.append("retry_budget_exhausted")
    if proposal.action != "abort" and budget.released >= budget.payload_count:
        reasons.append("all_fixture_payloads_released")
    return {
        "schema": "missionos.synthetic_dispenser_rules.v1",
        "allowed": not reasons,
        "reasons": reasons,
        "requested_action": proposal.action,
        "duration_ticks": duration,
        "time_ticks": budget.time_ticks,
        "deadline_ticks": budget.deadline_ticks,
    }


def _stream(split: str, group: str, seed: int, kind: str) -> random.Random:
    material = json.dumps([split, group, seed, kind], separators=(",", ":")).encode()
    return random.Random(int.from_bytes(sha256(material).digest(), "big"))


def make_world(config: dict, split: str, group: str, seed: int) -> dict:
    validate_config(config)
    if split not in ("train", "eval") or group not in GROUPS or type(seed) is not int:
        raise ValueError(
            "World generation requires an explicit declared split, group and integer seed"
        )
    recoveries = {"short": (4, 8), "late": (14, 18), "never": (None,)}[group]
    recovery = _stream(split, group, seed, "recovery").choice(recoveries)
    sensor_random = _stream(split, group, seed, "sensor")
    tape = []
    for tick in range(config["deadline_ticks"] + 1):
        ready = recovery is not None and tick * config["tick_s"] >= recovery
        numerator = (
            config["healthy_likelihood_ready"] if ready else config["healthy_likelihood_jammed"]
        )
        tape.append(
            {
                "time_ticks": tick,
                "time_s": tick * config["tick_s"],
                "healthy": sensor_random.randrange(config["sensor_denominator"]) < numerator,
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
    world["tape_sha256"] = digest(world)
    return world


def _fraction_record(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator, "value": float(value)}


def run_policy_tape(
    config: dict,
    tape: dict,
    policy_name: str,
    parameters: dict | None = None,
    solver: FiniteModelSolver | None = None,
    *,
    approved: bool = False,
    clairvoyant: bool = False,
    observation_callback=None,
) -> dict:
    """Execute one finite fixture; world truth stays outside the proposal function.

    The optional observer receives isolated public history/budget copies before
    a proposal. Its return value is ignored; it has no execution authority.
    Observer errors propagate, so an incomplete observation cannot look complete.
    """
    validate_config(config)
    if approved is not True:
        raise PermissionError("Synthetic simulation requires explicit approval")
    if policy_name not in POLICIES and not (clairvoyant and policy_name == "clairvoyant_reference"):
        raise ValueError("Unknown policy")
    if clairvoyant != (policy_name == "clairvoyant_reference"):
        raise ValueError(
            "Clairvoyant information must have the separate clairvoyant_reference label"
        )
    if observation_callback is not None and not callable(observation_callback):
        raise ValueError("The observation callback must be callable")
    if tape.get("tape_sha256") != digest({k: v for k, v in tape.items() if k != "tape_sha256"}):
        raise ValueError("World tape hash does not match the supplied tape")
    parameters = _validate_parameters(config, policy_name, parameters)
    solver = solver or FiniteModelSolver(config)
    scope = {
        "schema": "missionos.synthetic_dispenser_scope.v1",
        "kind": "bounded_synthetic_subsystem",
        "simulation_approved": True,
        "authenticated_operator_identity": False,
        "config_sha256": digest(config),
        "physical_execution_authorized": False,
    }
    history = PublicHistory((SensorObservation(0, tape["sensor_tape"][0]["healthy"]),))
    budget = PublicBudget(
        0, 0, 0, config["deadline_ticks"], config["max_attempts"], config["payload_count"]
    )
    initial = {**asdict(history.observations[0]), "time_s": 0}
    steps = []
    reason = "aborted"
    while True:
        if budget.released == budget.payload_count:
            reason = "all_fixture_payloads_released"
            break
        if budget.time_ticks == budget.deadline_ticks:
            reason = "return_deadline"
            break
        if budget.attempts == budget.max_attempts:
            reason = "retry_budget_exhausted"
            break
        if observation_callback is not None:
            observation_callback(deepcopy(history), deepcopy(budget), len(steps))
        if clairvoyant:
            index = config["recovery_times_s"].index(tape["recovery_time_s"])
            known = tuple(int(i == index) for i in range(len(config["prior_weights"])))
            proposal = ActionProposal(
                solver.best_action(budget, known),
                "clairvoyant reference; realized recovery time known",
            )
        else:
            proposal = propose_action(policy_name, history, budget, parameters, solver)
        rules = constrain_proposal(proposal, budget, scope)
        step = {
            "index": len(steps),
            "public_budget": asdict(budget),
            "public_history": {
                "observations": [asdict(observation) for observation in history.observations],
                "retry_results": [asdict(result) for result in history.retry_results],
            },
            "proposal": asdict(proposal),
            "rules": rules,
            "receipt": None,
            "observation": None,
        }
        if not rules["allowed"]:
            steps.append(step)
            reason = "rules_blocked"
            break
        start = budget.time_ticks
        end = start + rules["duration_ticks"]
        success = (
            proposal.action == "retry"
            and tape["recovery_time_s"] is not None
            and tape["recovery_time_s"] <= start * config["tick_s"]
        )
        attempts = budget.attempts + (proposal.action == "retry")
        released = budget.released + success
        receipt = {
            "schema": "missionos.synthetic_dispenser_receipt.v1",
            "action": proposal.action,
            "start_ticks": start,
            "end_ticks": end,
            "start_s": start * config["tick_s"],
            "end_s": end * config["tick_s"],
            "attempted": proposal.action == "retry",
            "success": success if proposal.action == "retry" else None,
            "released_delta": int(success),
            "release_count_after": released,
            "attempt_count_after": attempts,
            "execution_kind": "synthetic_subsystem",
            "physical_execution": False,
        }
        step["receipt"] = receipt
        if proposal.action == "abort":
            steps.append(step)
            reason = "aborted"
            break
        observation = SensorObservation(end, tape["sensor_tape"][end]["healthy"])
        step["observation"] = {**asdict(observation), "time_s": end * config["tick_s"]}
        results = history.retry_results
        if proposal.action == "retry":
            results += (RetryObservation(start, end, bool(success)),)
        history = PublicHistory(history.observations + (observation,), results)
        budget = PublicBudget(
            end,
            attempts,
            released,
            budget.deadline_ticks,
            budget.max_attempts,
            budget.payload_count,
        )
        steps.append(step)
    elapsed = budget.time_ticks * config["tick_s"]
    utility = (
        Fraction(
            config["reward_per_payload"] * budget.released - config["retry_cost"] * budget.attempts
        )
        - Fraction(config["time_cost_numerator"], config["time_cost_denominator"]) * elapsed
    )
    return {
        "schema": "missionos.synthetic_dispenser_run.v1",
        "world_id": tape["world_id"],
        "split": tape["split"],
        "policy": policy_name,
        "parameters": parameters,
        "tape_sha256": tape["tape_sha256"],
        "scope": scope,
        "initial_observation": initial,
        "steps": steps,
        "terminal": {
            "release_count": budget.released,
            "payload_count": config["payload_count"],
            "elapsed_s": elapsed,
            "deadline_s": config["deadline_ticks"] * config["tick_s"],
            "margin_s": (config["deadline_ticks"] - budget.time_ticks) * config["tick_s"],
            "attempt_count": budget.attempts,
            "utility": float(utility),
            "utility_exact": _fraction_record(utility),
            "reason": reason,
            "all_fixture_payloads_released": budget.released == config["payload_count"],
            "fuel_unmodeled": True,
            "physical_execution": False,
            "starlink_service_verified": False,
        },
    }


def expected_policy_value(
    config: dict,
    policy: str,
    parameters: dict | None = None,
    solver: FiniteModelSolver | None = None,
) -> Fraction:
    """Exact prior expectation of a frozen policy, without evaluation tapes.

    This enumerates the same legal endpoint observations and retry outcomes.
    Periodic policies additionally remember their last retry completion time;
    the other fixed policies need only the sufficient posterior/budget state.
    """
    validate_config(config)
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    parameters = _validate_parameters(config, policy, parameters)
    solver = solver or FiniteModelSolver(config)
    if policy == "finite_model_optimal":
        return solver.initial_value()

    @lru_cache(maxsize=None)
    def future(
        tick: int, attempts: int, released: int, weights: tuple[int, ...], last_end: int
    ) -> Fraction:
        if (
            solver._terminal(tick, attempts, released)
            or tick + config["retry_ticks"] > config["deadline_ticks"]
        ):
            return Fraction(0)
        if policy == "abort":
            return Fraction(0)
        if policy == "fixed_retry":
            if attempts >= parameters["limit"]:
                return Fraction(0)
            action = "retry"
        elif policy == "periodic_retry":
            action = "wait" if tick < last_end + parameters["wait_ticks"] else "retry"
        else:
            threshold = Fraction(str(parameters["posterior_ready_threshold"]))
            action = "retry" if solver.ready_probability(weights, tick) >= threshold else "wait"
        duration = config["retry_ticks"] if action == "retry" else config["wait_ticks"]
        if tick + duration > config["deadline_ticks"]:
            return Fraction(0)
        result = -duration * config["tick_s"] * solver.time_cost
        if action == "retry":
            result -= config["retry_cost"]
        for probability, posterior, success in solver._transitions(tick, weights, action):
            reward = config["reward_per_payload"] if success else 0
            result += probability * (
                reward
                + future(
                    tick + duration,
                    attempts + (action == "retry"),
                    released + success,
                    posterior,
                    tick + duration if action == "retry" else last_end,
                )
            )
        return result

    return future(0, 0, 0, solver.prior, 0)


def _summary(runs: list[dict], worlds: list[dict], split: str, policy: str) -> dict:
    group_by_id = {world["world_id"]: world["group"] for world in worlds}
    selected = [run for run in runs if run["split"] == split and run["policy"] == policy]
    fields = {
        "mean_utility": "utility",
        "mean_release_count": "release_count",
        "full_release_fraction": "all_fixture_payloads_released",
        "mean_elapsed_s": "elapsed_s",
        "mean_attempt_count": "attempt_count",
        "mean_margin_s": "margin_s",
    }
    summary = {
        "run_count": len(selected),
        "stratum_counts": {},
        "weighting": "equal 1/3 per declared stratum",
    }
    grouped = {
        group: [run for run in selected if group_by_id[run["world_id"]] == group]
        for group in GROUPS
    }
    summary["stratum_counts"] = {group: len(rows) for group, rows in grouped.items()}
    for label, field in fields.items():
        summary[label] = sum(
            sum(run["terminal"][field] for run in rows) / len(rows) / 3 for rows in grouped.values()
        )
    return summary


def run_dispenser_experiment(config: dict | None = None, *, approved: bool = False) -> dict:
    """Tune only on training tapes, freeze winners, and evaluate common held-out tapes."""
    config = deepcopy(default_experiment_config() if config is None else config)
    validate_config(config, frozen=True)
    if approved is not True:
        raise PermissionError("Use explicit simulation approval for this synthetic experiment")
    solver = FiniteModelSolver(config)
    worlds = [
        make_world(config, split, group, seed)
        for split in ("train", "eval")
        for group in GROUPS
        for seed in config[f"{split}_seeds"]
    ]
    tuning = {}
    training_candidate_runs = []
    selected_parameters = {"abort": {}, "finite_model_optimal": {}}
    train_worlds = [world for world in worlds if world["split"] == "train"]
    train_groups = {world["world_id"]: world["group"] for world in train_worlds}
    for policy, candidates in config["tuning_candidates"].items():
        scores = []
        exact_scores = []
        for parameters in candidates:
            runs = [
                run_policy_tape(config, world, policy, parameters, solver, approved=True)
                for world in train_worlds
            ]
            training_candidate_runs.extend(runs)
            score = Fraction(0)
            for group in GROUPS:
                grouped = [run for run in runs if train_groups[run["world_id"]] == group]
                score += sum(
                    (
                        Fraction(
                            run["terminal"]["utility_exact"]["numerator"],
                            run["terminal"]["utility_exact"]["denominator"],
                        )
                        for run in grouped
                    ),
                    Fraction(0),
                ) / (3 * len(grouped))
            exact_scores.append(score)
            scores.append(
                {
                    "parameters": deepcopy(parameters),
                    "train_mean_utility": float(score),
                    "train_mean_utility_exact": _fraction_record(score),
                }
            )
        winner = max(range(len(scores)), key=lambda index: exact_scores[index])
        selected_parameters[policy] = deepcopy(candidates[winner])
        tuning[policy] = {
            "candidates": scores,
            "selected_parameters": deepcopy(candidates[winner]),
            "selection_split": "train",
            "tie_break": "enumeration_order",
        }
    policy_runs = [
        run_policy_tape(config, world, policy, selected_parameters[policy], solver, approved=True)
        for world in worlds
        for policy in POLICIES
    ]
    clairvoyant_runs = [
        run_policy_tape(
            config, world, "clairvoyant_reference", {}, solver, approved=True, clairvoyant=True
        )
        for world in worlds
    ]
    expected = {
        policy: expected_policy_value(config, policy, selected_parameters[policy], solver)
        for policy in POLICIES
    }
    comparator = max(POLICIES[:-1], key=lambda policy: expected[policy])
    return {
        "schema": "missionos.synthetic_dispenser_experiment.v1",
        "config": config,
        "config_sha256": digest(config),
        "claim_boundary": {
            "bounded_synthetic_subsystem": True,
            "flight_physics_changed": False,
            "full_flight_rerun": False,
            "fuel_unmodeled": True,
            "llm_invoked": False,
            "physical_execution": False,
            "starship_vehicle_validated": False,
            "starlink_service_verified": False,
            "missionos_gateway_integration": False,
        },
        "model_bounds": {
            "finite_model_optimal_expected_utility": _fraction_record(solver.initial_value()),
            "clairvoyant_expected_utility": _fraction_record(solver.clairvoyant_expected_value()),
            "policy_expected_utilities": {
                policy: _fraction_record(value) for policy, value in expected.items()
            },
            "best_nonoptimal_policy": comparator,
            "optimal_minus_best_nonoptimal_expected_utility": _fraction_record(
                expected["finite_model_optimal"] - expected[comparator]
            ),
            "scope": "Expected utility within the declared finite law, observations, actions and objective; empirical evaluation averages are not oracle bounds",
            "clairvoyant_information": "Realized recovery time known; separate reference, not an admissible policy",
            "solver": "rational Bellman enumeration with exact integer posterior weights",
            "initial_condition": "known jam at t=0; initial sensor likelihood identical for all recovery hypotheses",
        },
        "tuning": tuning,
        "training_candidate_runs": training_candidate_runs,
        "worlds": worlds,
        "policy_runs": policy_runs,
        "clairvoyant_runs": clairvoyant_runs,
        "summary": {
            split: {policy: _summary(policy_runs, worlds, split, policy) for policy in POLICIES}
            for split in ("train", "eval")
        },
        "clairvoyant_summary": {
            split: _summary(clairvoyant_runs, worlds, split, "clairvoyant_reference")
            for split in ("train", "eval")
        },
        "limitations": [
            "Three fictional payloads; unrelated to Flight 14's payload manifest or dispenser timing.",
            "Time and attempt costs are synthetic research utility, not measured fuel or damage.",
            "An exact finite-model policy is only optimal under this declared law and objective.",
            "Common pre-generated sensor tapes align exogenous randomness; different action durations expose different sampled observation times.",
            "Baseline grids are selected using training only; held-out evaluation cannot retune them.",
        ],
    }
