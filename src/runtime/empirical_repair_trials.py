"""Experience-ordered, directly verified repair trials for resettable Backends.

This module proposes immutable registered program IDs. Approval, Rules,
Executor action and Verifier judgment stay with the normal MissionOS runtime.
A simulated same-start result is not physical robot evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from missionos_core import canonical_sha256

MAX_STATES = 256
MAX_ALTERNATIVES = 4
MAX_PROBE_FACTS = 8


@dataclass(frozen=True)
class MeasuredArm:
    program_id: str
    episode_id: str
    before_digest: str
    status: str
    complete: bool | None
    protected: bool | None
    achieved: bool | None

    @property
    def success(self) -> bool | None:
        if self.status in {"unknown", "infrastructure_error", "untried"} or any(
            value is None for value in (self.complete, self.protected, self.achieved)
        ):
            return None
        return self.complete is True and self.protected is True and self.achieved is True


@dataclass(frozen=True)
class MatchedExperience:
    record_id: str
    before: Mapping[str, bool | float]
    arms: Mapping[str, MeasuredArm | None]


@dataclass(frozen=True)
class CounterfactualTrialPlan:
    """A bounded proposal for separate, resettable same-start Backend trials."""

    before_digest: str
    ordered_program_ids: tuple[str, ...]
    witness_episode_ids: tuple[str, ...]

    @property
    def digest(self) -> str:
        return canonical_sha256(
            {
                "before_digest": self.before_digest,
                "ordered_program_ids": list(self.ordered_program_ids),
                "witness_episode_ids": list(self.witness_episode_ids),
            }
        )


def plan_counterfactual_trials(
    records: Sequence[MatchedExperience],
    before: Mapping[str, bool | float],
    *,
    parent_program_id: str,
    alternatives: Sequence[str],
    fact_scales: Mapping[str, float],
    maximum_alternatives: int = 1,
) -> CounterfactualTrialPlan:
    """Order saved programs by nearby measured successes, with parent first.

    This proposes resettable simulator trials; it cannot predict a real robot's
    result or authorize any execution. A direct same-start Verifier result is
    required before a program can be selected for this start.
    """
    if (
        not 1 <= len(records) <= MAX_STATES
        or not 1 <= len(alternatives) <= MAX_ALTERNATIVES
        or not 1 <= maximum_alternatives <= len(alternatives)
        or not 1 <= len(fact_scales) <= MAX_PROBE_FACTS
        or len(set(alternatives)) != len(alternatives)
        or parent_program_id in alternatives
        or not parent_program_id
    ):
        raise ValueError("invalid bounded counterfactual proposal")
    scales = dict(fact_scales)
    if any(
        type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0
        for scale in scales.values()
    ):
        raise ValueError("invalid finite fact scale")

    def vector(facts: Mapping[str, bool | float]) -> tuple[float, ...]:
        values = []
        for name, scale in scales.items():
            value = facts.get(name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("unknown counterfactual entry fact")
            values.append(float(value) / scale)
        return tuple(values)

    entry = vector(before)
    required = {parent_program_id, *alternatives}
    seen_records = set()
    seen_episodes = set()
    nearby_successes: dict[str, list[tuple[float, str]]] = {name: [] for name in alternatives}
    for record in records:
        if record.record_id in seen_records or set(record.arms) != required:
            raise ValueError("unmatched counterfactual experience")
        seen_records.add(record.record_id)
        measured = vector(record.before)
        digest = canonical_sha256(dict(record.before))
        for name, arm in record.arms.items():
            if arm is None:
                continue
            if (
                arm.program_id != name
                or arm.before_digest != digest
                or not arm.episode_id
                or arm.episode_id in seen_episodes
            ):
                raise ValueError("counterfactual episode binding mismatch")
            seen_episodes.add(arm.episode_id)
            if name in nearby_successes and arm.success is True:
                distance = math.sqrt(
                    sum((a - b) ** 2 for a, b in zip(entry, measured, strict=True))
                )
                nearby_successes[name].append((distance, arm.episode_id))
    ranked = sorted(
        (min(neighbors), name) for name, neighbors in nearby_successes.items() if neighbors
    )
    selected = ranked[:maximum_alternatives]
    return CounterfactualTrialPlan(
        canonical_sha256(dict(before)),
        (parent_program_id, *(name for _, name in selected)),
        tuple(episode_id for (_, episode_id), _ in selected),
    )


def adjudicate_counterfactual_trials(
    plan: CounterfactualTrialPlan, attempts: Sequence[MeasuredArm]
) -> dict:
    """Select only a directly verified same-start success from proposed trials."""
    if not attempts or len(attempts) > len(plan.ordered_program_ids):
        raise ValueError("invalid counterfactual attempt population")
    seen = set()
    winner = None
    for index, attempt in enumerate(attempts):
        if (
            attempt.program_id != plan.ordered_program_ids[index]
            or attempt.before_digest != plan.before_digest
            or not attempt.episode_id
            or attempt.episode_id in seen
        ):
            raise ValueError("counterfactual trial release or start mismatch")
        seen.add(attempt.episode_id)
        if winner is not None:
            raise ValueError("counterfactual trial after verified success")
        if attempt.success is None:
            return {"status": "hold", "reason": "unknown_trial_outcome", "plan_digest": plan.digest}
        if attempt.protected is False or attempt.achieved is False:
            return {"status": "hold", "reason": "trial_lost_protection", "plan_digest": plan.digest}
        if attempt.success is True:
            winner = attempt
    if winner is not None:
        return {
            "status": "proposed",
            "program_id": winner.program_id,
            "episode_id": winner.episode_id,
            "selection_basis": "same_start_verified_counterfactual",
            "plan_digest": plan.digest,
        }
    if len(attempts) < len(plan.ordered_program_ids):
        return {"status": "pending", "plan_digest": plan.digest}
    return {"status": "no_verified_recovery", "plan_digest": plan.digest}
