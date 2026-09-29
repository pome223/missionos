"""A pad mission judge is asked only for a Rules entry and can only add a bounded wait."""

import copy
import json
import time

import pytest

from src.runtime.yokohama_pad_queue import (
    MissionJudgeGate,
    PadSupervisor,
    atomic_json,
    digest,
    make_request,
    propose,
    require_response,
)
from test_yokohama_pad_queue import config as base_config, observation

WAIT, ENTER = "wait_at_current_hold", "enter_delivery_approach"


def config(mode="gateway"):
    c = base_config()
    c["world"]["pad_queue"]["mission_judge"] = dict(
        mode=mode, max_decisions=2, max_added_wait_s=30, judge_timeout_s=20, authority="test"
    )
    return c


def exchange(c, gate, seq, start):
    """One clear-window request at wall time start+5, answered through the gate."""
    request = make_request(c, seq, [observation(t) for t in range(start, start + 6)])
    response = gate.respond(request, propose(c, request))
    action = require_response(c, request, response, observation(start + 5))
    return request, response, action


def answer(gate, folder, action="enter", seconds=0, **changes):
    request = json.loads((folder / "request.json").read_text())
    value = dict(
        judge_request_id=request["judge_request_id"],
        judge_status="valid",
        decision=dict(
            observation_id=request["observation_id"],
            action=action,
            wait_seconds=seconds,
            rationale="テスト",
        ),
    )
    value.update(changes)
    atomic_json(folder / "response.json", value)
    return value


def test_occupied_pad_never_consults_the_judge(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    request = make_request(c, 0, [observation(0, occupied=True)])
    response = gate.respond(request, propose(c, request))
    assert response["mission_judge"]["status"] == "not_consulted"
    assert require_response(c, request, response, observation(0)) == WAIT
    assert not (tmp_path / "pad-judge").exists()


def test_rules_entry_waits_for_the_judge_then_passes_on_no_objection(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    request, response, action = exchange(c, gate, 0, 0)
    assert action == WAIT and response["mission_judge"]["status"] == "pending"
    judge_request = json.loads((tmp_path / "pad-judge/000/request.json").read_text())
    assert judge_request["pad_request_id"] == request["request_id"]
    assert judge_request["situation"]["rules_action"] == ENTER
    assert judge_request["remaining_wait_seconds"] == 30
    reply = answer(gate, tmp_path / "pad-judge/000")
    _, response, action = exchange(c, gate, 1, 2)
    assert action == ENTER
    assert response["mission_judge"]["status"] == "no_objection"
    assert response["mission_judge"]["judgment_sha256"] == digest(reply)


def test_judged_wait_holds_then_a_second_judgment_is_asked(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    answer(gate, tmp_path / "pad-judge/000", "wait", 6)
    _, response, action = exchange(c, gate, 1, 2)  # hold starts at wall 7 for 6 s
    assert action == WAIT and response["mission_judge"]["status"] == "hold"
    _, response, action = exchange(c, gate, 2, 5)  # wall 10, still held
    assert action == WAIT and response["mission_judge"]["status"] == "hold"
    _, response, action = exchange(c, gate, 3, 9)  # wall 14: second request
    assert response["mission_judge"]["status"] == "pending"
    assert (tmp_path / "pad-judge/001/request.json").exists()
    answer(gate, tmp_path / "pad-judge/001")
    _, response, action = exchange(c, gate, 4, 10)
    assert action == ENTER and response["mission_judge"]["added_wait_s"] == 10


@pytest.mark.parametrize(
    "reply",
    [
        dict(action="wait", seconds=31),
        dict(action="enter", seconds=3),
        dict(action="land", seconds=0),
        dict(judge_status="invalid"),
        dict(judge_request_id="0" * 64),
    ],
)
def test_invalid_answers_leave_the_rules_entry(tmp_path, reply):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    action = reply.pop("action", "enter")
    seconds = reply.pop("seconds", 0)
    answer(gate, tmp_path / "pad-judge/000", action, seconds, **reply)
    _, response, result = exchange(c, gate, 1, 2)
    assert result == ENTER and response["mission_judge"]["status"] == "invalid"


def test_silent_judge_times_out_to_the_rules_entry(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    _, response, action = exchange(c, gate, 1, 10)
    assert action == WAIT and response["mission_judge"]["status"] == "pending"
    _, response, action = exchange(c, gate, 2, 21)
    assert action == ENTER and response["mission_judge"]["status"] == "unavailable"


def test_budget_and_decision_limits_end_judge_waiting(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    answer(gate, tmp_path / "pad-judge/000", "wait", 30)
    _, response, action = exchange(c, gate, 1, 1)
    assert response["mission_judge"]["status"] == "hold"
    _, response, action = exchange(c, gate, 2, 32)
    assert action == ENTER and response["mission_judge"]["status"] == "budget_exhausted"
    gate = MissionJudgeGate(tmp_path / "second", c)
    for seq in range(2):
        exchange(c, gate, 2 * seq, 4 * seq)
        answer(gate, tmp_path / f"second/pad-judge/{seq:03d}", "wait", 1)
        exchange(c, gate, 2 * seq + 1, 4 * seq + 1)
    _, response, action = exchange(c, gate, 4, 9)
    assert action == ENTER and response["mission_judge"]["status"] == "decisions_exhausted"


def test_reoccupation_discards_a_pending_judgment(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    request = make_request(c, 1, [observation(6, occupied=True)])
    response = gate.respond(request, propose(c, request))
    assert response["mission_judge"]["status"] == "not_consulted" and gate.pending is None


@pytest.mark.parametrize(
    "fault",
    ["grant_over_rules_wait", "judge_on_wait", "missing", "over_budget", "enter_while_pending"],
)
def test_executor_rejects_a_receipt_that_exceeds_the_judge(tmp_path, fault):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    request, response, _ = exchange(c, gate, 0, 0)
    current = observation(5)
    if fault in ("grant_over_rules_wait", "judge_on_wait"):
        request = make_request(c, 1, [observation(5, occupied=True)])
        response = gate.respond(request, propose(c, request))
        response = copy.deepcopy(response)
        if fault == "grant_over_rules_wait":
            response["proposed_action"] = ENTER
        response["mission_judge"]["status"] = (
            "no_objection" if fault == "judge_on_wait" else "pending"
        )
    elif fault == "missing":
        response = copy.deepcopy(response)
        del response["mission_judge"]
    elif fault == "over_budget":
        response = copy.deepcopy(response)
        response["mission_judge"]["added_wait_s"] = 31
    else:
        response = dict(copy.deepcopy(response), proposed_action=ENTER)
    with pytest.raises(ValueError):
        require_response(c, request, response, current)


def test_host_mailbox_fixture_judge_answers_before_entry(tmp_path):
    c = config("fixture")
    host = PadSupervisor(tmp_path, c)
    try:
        actions = []
        for seq, start in [(0, 0), (1, 1)]:
            folder = tmp_path / "pad-decisions" / f"{seq:03d}"
            folder.mkdir(parents=True)
            request = make_request(c, seq, [observation(t) for t in range(start, start + 6)])
            atomic_json(folder / "request.json", request)
            deadline = time.monotonic() + 3
            while not (folder / "response.json").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            response = json.loads((folder / "response.json").read_text())
            actions.append(require_response(c, request, response, observation(start + 5)))
        assert actions == [WAIT, ENTER]
        record = json.loads((tmp_path / "pad-judge/000/response.json").read_text())
        assert record["invocation"]["invocation_kind"] == "deterministic_fixture"
    finally:
        host.close()
