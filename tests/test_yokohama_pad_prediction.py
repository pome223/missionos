"""Candidate selection and evidence rejection, without native or AP execution."""

from copy import deepcopy
from dataclasses import replace

import pytest

from scripts.check_yokohama_pad_prediction import FixtureJudge, fixture, run_cases
from src.intelligence.mission_assurance_agent import ModelJudgment
from src.runtime.yokohama_pad_prediction import evaluate


def test_forecast_changes_selection_with_identical_current_state_and_vla_candidate():
    choices = []
    sources = []
    for conflict in (True, False):
        situation, envelope = fixture(predicted_step_conflict=conflict)
        original = deepcopy(envelope)
        result = evaluate(situation, envelope, FixtureJudge(), now=101)
        assert result["selection_valid_for_review"]
        assert result["proposal"]["model_inference_invoked"] is False
        assert envelope == original
        assert not any(
            result[k]
            for k in (
                "approval_recorded",
                "dispatch_authority_created",
                "dispatch_request_sent",
                "physical_execution_invoked",
                "completion_claimed",
            )
        )
        choices.append(result["selected_candidate"]["option_id"])
        sources.append((situation.observations, envelope["request"]))
    assert sources[0] == sources[1]
    assert choices == ["hold", "vla"]


def test_runtime_scenarios():
    records = run_cases()
    assert len(records) == 8
    assert all(r["passed"] for r in records)


class AlwaysForward:
    def judge(self, prompt):
        output = dict(FixtureJudge().judge(prompt).output)
        output.update(proposed_response_kind="continue", parameters={"candidate_id": "vla"})
        return ModelJudgment(output, {"invocation_kind": "deterministic_fixture"}, False)


@pytest.mark.parametrize(
    "conflict,clear,reason",
    [
        (True, True, "selected_forecast_contains_conflict"),
        (False, False, "current_pad_or_approach_not_clear"),
    ],
)
def test_judge_cannot_ignore_conflict_or_current_occupancy(conflict, clear, reason):
    situation, envelope = fixture(predicted_step_conflict=conflict, current_clear=clear)
    result = evaluate(situation, envelope, AlwaysForward(), now=101)
    assert not result["selection_valid_for_review"]
    assert result["reason"] == reason


def test_judge_cannot_rewrite_vla_action():
    class Rewrite:
        def judge(self, prompt):
            original = AlwaysForward().judge(prompt)
            original.output["parameters"]["delta_body_frd"] = [3, 0, 0, 0]
            return original

    situation, envelope = fixture()
    result = evaluate(situation, envelope, Rewrite(), now=101)
    assert result["reason"] == "judge_modified_or_mismatched_candidate"
    assert result["selected_candidate"] is None


def test_positive_vla_choice_is_not_rewritten_when_wam_suggests_wait():
    situation, envelope = fixture(predicted_step_conflict=True)
    result = evaluate(situation, envelope, FixtureJudge(), now=101)
    assert result["selected_candidate"]["parameters"] == {"delta_body_frd": [0, 0, 0, 0]}
    assert situation.constraints["vla_candidate"]["delta_body_frd"] == [2, 0, 0, 0]


def test_uncertain_semantics_cannot_support_step():
    situation, envelope = fixture()
    envelope["forecast"]["forecasts"][1]["future_state"]["pad_state_at_horizon"] = "unknown"
    result = evaluate(situation, envelope, AlwaysForward(), now=101)
    assert result["reason"] == "selected_forecast_does_not_support_clear_pad"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda s, e: s.constraints["vla_candidate"].update(delta_body_frd=[2.5, 0, 0, 0]),
            "candidate_changed_before_forecast",
        ),
        (
            lambda s, e: e["forecast"]["forecasts"][1].update(
                future_state={"pad_state_at_horizon": "clear"}
            ),
            "missing_dynamic_pad_semantics",
        ),
        (
            lambda s, e: s.constraints["pad_forecast_profile"].update(semantic_validation_ref=""),
            "unqualified_pad_forecast_profile",
        ),
        (
            lambda s, e: s.observations.update(observed_sim_s=99),
            "current_hold_or_wait_budget_unavailable",
        ),
    ],
)
def test_ineligible_evidence_never_reaches_judge(mutation, reason):
    situation, envelope = fixture()
    mutation(situation, envelope)
    result = evaluate(situation, envelope, None, now=101)
    assert result["reason"] == reason
    assert not result["judge_invoked"]


def test_fixture_capabilities_cannot_be_relabelled_as_simulation():
    situation, envelope = fixture()
    result = evaluate(replace(situation, execution_scope="simulation"), envelope, None, now=101)
    assert result["reason"] == "unqualified_pad_forecast_profile"


def test_judge_unavailable_does_not_silently_select_a_fallback():
    class Unavailable:
        def judge(self, prompt):
            raise RuntimeError("provider failed")

    situation, envelope = fixture()
    result = evaluate(situation, envelope, Unavailable(), now=101)
    assert result["reason"] == "mission_judgment_unavailable"
    assert not result["selection_valid_for_review"]
    assert result["selected_candidate"] is None
