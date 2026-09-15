"""Experience changes a bounded simulator trial; receipts decide the result."""

from __future__ import annotations

import pytest

from missionos_core import canonical_sha256
from src.runtime.empirical_repair_trials import (
    MatchedExperience,
    MeasuredArm,
    adjudicate_counterfactual_trials,
    plan_counterfactual_trials,
)


def _record(name: str, x: float, *, near: bool, far: bool) -> MatchedExperience:
    before = {"object_x_m": x}
    digest = canonical_sha256(before)
    return MatchedExperience(
        name,
        before,
        {
            program: MeasuredArm(
                program,
                f"{name}:{program}",
                digest,
                "completed",
                complete,
                True,
                True,
            )
            for program, complete in (
                ("parent", False),
                ("near-program", near),
                ("far-program", far),
            )
        },
    )


def _records() -> list[MatchedExperience]:
    return [_record("near", 0.0, near=True, far=False), _record("far", 10.0, near=False, far=True)]


def _plan(before: dict):
    return plan_counterfactual_trials(
        _records(),
        before,
        parent_program_id="parent",
        alternatives=["far-program", "near-program"],
        fact_scales={"object_x_m": 1.0},
        maximum_alternatives=1,
    )


def test_experience_changes_the_one_alternative_budget_choice():
    plan = _plan({"object_x_m": 0.1})
    assert plan.ordered_program_ids == ("parent", "near-program")
    assert plan.witness_episode_ids == ("near:near-program",)
    assert len(plan.digest) == 64
    assert _plan({"object_x_m": 9.9}).ordered_program_ids == ("parent", "far-program")


def test_same_start_verifier_receipts_are_required_before_selection():
    before = {"object_x_m": 0.1}
    plan = _plan(before)
    digest = canonical_sha256(before)
    parent = MeasuredArm("parent", "direct:parent", digest, "completed", False, True, True)
    near = MeasuredArm("near-program", "direct:near", digest, "completed", True, True, True)
    assert adjudicate_counterfactual_trials(plan, [parent])["status"] == "pending"
    selected = adjudicate_counterfactual_trials(plan, [parent, near])
    assert selected["status"] == "proposed"
    assert selected["program_id"] == "near-program"
    assert selected["selection_basis"] == "same_start_verified_counterfactual"
    assert selected["plan_digest"] == plan.digest

    parent_success = MeasuredArm(
        "parent", "direct:parent-success", digest, "completed", True, True, True
    )
    assert adjudicate_counterfactual_trials(plan, [parent_success])["program_id"] == "parent"
    with pytest.raises(ValueError, match="release or start mismatch"):
        adjudicate_counterfactual_trials(
            plan,
            [parent, MeasuredArm("near-program", "wrong", "0" * 64, "completed", True, True, True)],
        )


def test_unknown_unprotected_or_untried_experience_never_selects_a_recovery():
    before = {"object_x_m": 0.1}
    plan = _plan(before)
    digest = canonical_sha256(before)
    parent = MeasuredArm("parent", "direct:parent", digest, "completed", False, True, True)
    unsafe = MeasuredArm("near-program", "unsafe", digest, "completed", False, False, True)
    unknown = MeasuredArm("near-program", "unknown", digest, "unknown", None, None, None)
    assert (
        adjudicate_counterfactual_trials(plan, [parent, unsafe])["reason"]
        == "trial_lost_protection"
    )
    assert (
        adjudicate_counterfactual_trials(plan, [parent, unknown])["reason"]
        == "unknown_trial_outcome"
    )

    untried = _records()
    record = untried[0]
    untried[0] = MatchedExperience(
        record.record_id, record.before, {**record.arms, "near-program": None}
    )
    limited = plan_counterfactual_trials(
        untried,
        before,
        parent_program_id="parent",
        alternatives=["far-program", "near-program"],
        fact_scales={"object_x_m": 1.0},
    )
    assert limited.ordered_program_ids == ("parent", "far-program")


def test_entry_facts_and_episode_bindings_must_be_known_and_matched():
    with pytest.raises(ValueError, match="unknown counterfactual entry fact"):
        _plan({})
    with pytest.raises(ValueError, match="invalid finite fact scale"):
        plan_counterfactual_trials(
            _records(),
            {"object_x_m": 0.1},
            parent_program_id="parent",
            alternatives=["far-program", "near-program"],
            fact_scales={"object_x_m": 0.0},
        )
    record = _records()[0]
    near = record.arms["near-program"]
    assert near is not None
    wrong = MatchedExperience(
        record.record_id,
        record.before,
        {
            **record.arms,
            "near-program": MeasuredArm(
                near.program_id,
                near.episode_id,
                "0" * 64,
                near.status,
                near.complete,
                near.protected,
                near.achieved,
            ),
        },
    )
    with pytest.raises(ValueError, match="episode binding mismatch"):
        plan_counterfactual_trials(
            [wrong, _records()[1]],
            {"object_x_m": 0.1},
            parent_program_id="parent",
            alternatives=["far-program", "near-program"],
            fact_scales={"object_x_m": 1.0},
        )
