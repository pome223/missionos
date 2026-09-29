"""DeepSeek planner/judge outputs are validated; failures never become entry."""

import pytest

from src.intelligence import yokohama_delivery_agents as agents

CONFIG = dict(judge_timeout_seconds=20)
REQUEST = dict(
    judge_request_id="r" * 64,
    observation_id="pad_judge_1",
    situation={},
    remaining_wait_seconds=30,
    decisions_remaining=1,
    allowed_actions=["enter", "wait"],
)


@pytest.mark.parametrize(
    "output, valid",
    [
        (dict(supported=True, destination_id=agents.DESTINATION, summary="計画", reason=""), True),
        (dict(supported=False, destination_id="", summary="", reason="対象外"), True),
        (dict(supported=True, destination_id="tokyo", summary="計画", reason=""), False),
        (dict(supported=True, destination_id=agents.DESTINATION, summary="", reason=""), False),
        (
            dict(supported="yes", destination_id=agents.DESTINATION, summary="計画", reason=""),
            False,
        ),
        (dict(supported=True, destination_id=agents.DESTINATION, summary="計画"), False),
    ],
)
def test_planner_output_must_match_the_catalog(output, valid):
    assert agents.valid_plan(output) is valid


@pytest.mark.parametrize(
    "output, valid",
    [
        (dict(observation_id="pad_judge_1", action="enter", wait_seconds=0, rationale="空"), True),
        (dict(observation_id="pad_judge_1", action="wait", wait_seconds=30, rationale="待"), True),
        (dict(observation_id="pad_judge_1", action="wait", wait_seconds=31, rationale="待"), False),
        (
            dict(observation_id="pad_judge_1", action="wait", wait_seconds=True, rationale="待"),
            False,
        ),
        (dict(observation_id="pad_judge_1", action="enter", wait_seconds=2, rationale="空"), False),
        (dict(observation_id="pad_judge_2", action="enter", wait_seconds=0, rationale="空"), False),
        (dict(observation_id="pad_judge_1", action="land", wait_seconds=0, rationale="降"), False),
        (dict(observation_id="pad_judge_1", action="enter", wait_seconds=0, rationale=" "), False),
    ],
)
def test_judge_output_is_bounded(output, valid):
    assert agents.valid_decision(REQUEST, output) is valid


def test_provider_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(agents, "configuration", lambda: dict(CONFIG))

    def fail(*args):
        raise TimeoutError

    monkeypatch.setattr(agents, "_run", fail)
    answer = agents.judge(REQUEST, dict(CONFIG))
    assert answer["judge_status"] == "unavailable" and "decision" not in answer


def test_invalid_model_output_is_marked_invalid(monkeypatch):
    monkeypatch.setattr(agents, "configuration", lambda: dict(CONFIG))
    monkeypatch.setattr(
        agents,
        "_run",
        lambda *args: dict(
            guardrail_result=dict(
                guardrail_passed=True,
                validated_output=dict(observation_id="pad_judge_1", action="go"),
            )
        ),
    )
    assert agents.judge(REQUEST, dict(CONFIG))["judge_status"] == "invalid"


def test_configuration_needs_opt_in_and_key(monkeypatch):
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_AGENTS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="接続情報"):
        agents.configuration()
