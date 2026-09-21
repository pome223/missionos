"""Process smoke for experience-ordered, directly verified trial proposals."""

from __future__ import annotations

import json

from missionos_core import canonical_sha256
from src.runtime.empirical_repair_trials import (
    MatchedExperience,
    MeasuredArm,
    adjudicate_counterfactual_trials,
    plan_counterfactual_trials,
)


def _record(name: str, x: float, *, parent: bool, saved: bool) -> MatchedExperience:
    before = {"object_x_m": x}
    digest = canonical_sha256(before)
    return MatchedExperience(
        name,
        before,
        {
            program: MeasuredArm(
                program, f"{name}:{program}", digest, "completed", complete, True, True
            )
            for program, complete in (("parent", parent), ("saved", saved))
        },
    )


def main() -> None:
    records = [
        _record("needs-repair", 0.0, parent=False, saved=True),
        _record("keep-parent", 10.0, parent=True, saved=False),
    ]
    before = {"object_x_m": 0.1}
    plan = plan_counterfactual_trials(
        records,
        before,
        parent_program_id="parent",
        alternatives=["saved"],
        fact_scales={"object_x_m": 1.0},
    )
    digest = canonical_sha256(before)
    decision = adjudicate_counterfactual_trials(
        plan,
        [
            MeasuredArm("parent", "fixture:parent", digest, "completed", False, True, True),
            MeasuredArm("saved", "fixture:saved", digest, "completed", True, True, True),
        ],
    )
    assert plan.ordered_program_ids == ("parent", "saved")
    assert decision["status"] == "proposed" and decision["program_id"] == "saved"
    keep_before = {"object_x_m": 10.1}
    keep_plan = plan_counterfactual_trials(
        records,
        keep_before,
        parent_program_id="parent",
        alternatives=["saved"],
        fact_scales={"object_x_m": 1.0},
    )
    keep = adjudicate_counterfactual_trials(
        keep_plan,
        [
            MeasuredArm(
                "parent",
                "fixture:keep",
                canonical_sha256(keep_before),
                "completed",
                True,
                True,
                True,
            )
        ],
    )
    assert keep["program_id"] == "parent"
    try:
        plan_counterfactual_trials(
            records,
            {},
            parent_program_id="parent",
            alternatives=["saved"],
            fact_scales={"object_x_m": 1.0},
        )
    except ValueError as exc:
        assert "unknown counterfactual entry fact" in str(exc)
        missing_observation = "held"
    else:
        raise AssertionError("missing entry observation was not held")
    print(
        json.dumps(
            {
                "scenario": "fixture_parent_failure_measured_saved_recovery",
                "boundary": "matched experience -> bounded trial proposal -> matched Verifier receipt -> program proposal",
                "trial_programs": plan.ordered_program_ids,
                "recovery_proposal": decision["program_id"],
                "parent_preserved": keep["program_id"] == "parent",
                "missing_observation": missing_observation,
                "physical_execution": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
