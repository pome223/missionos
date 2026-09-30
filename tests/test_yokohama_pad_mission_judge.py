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
    with pytest.raises(ValueError, match="wall-clock deadline"):
        exchange(c, gate, 2, 32)
    assert gate.pending is None and gate.hold is None
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


@pytest.mark.parametrize("elapsed,status", [(19, "hold"), (20, "hold"), (21, "unavailable")])
def test_existing_reply_respects_deadline(tmp_path, elapsed, status):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    answer(gate, tmp_path / "pad-judge/000", "wait", 1)
    _, response, action = exchange(c, gate, 1, elapsed)
    assert response["mission_judge"]["status"] == status
    assert action == (ENTER if status == "unavailable" else WAIT)


@pytest.mark.parametrize("elapsed,passed", [(29, True), (30, True), (31, False)])
def test_verifier_includes_release_time(tmp_path, elapsed, passed):
    from scripts.verify_yokohama_pad_queue import judge_checks

    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    request, response, _ = exchange(c, gate, 0, 0)
    exchanges = [(request, response)]
    answer(gate, tmp_path / "pad-judge/000", "wait", 30)
    request, response, _ = exchange(c, gate, 1, 1)
    exchanges.append((request, response))
    request = make_request(c, 2, [observation(t) for t in range(elapsed, elapsed + 6)])
    response = gate.respond(request, propose(c, request))
    exchanges.append((request, response))
    checks, metrics = judge_checks(c["world"]["pad_queue"]["mission_judge"], tmp_path, exchanges)
    assert checks["mission_judge_within_budget"] is passed
    assert metrics["judge_added_wait_s"] == elapsed
    if elapsed >= 30:
        assert response["proposed_action"] == ENTER
        assert response["mission_judge"]["status"] == "budget_exhausted"


def test_canceled_episode_does_not_reuse_its_answer(tmp_path):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    answer(gate, tmp_path / "pad-judge/000", "wait", 30)
    request = make_request(c, 1, [observation(6, occupied=True)])
    response = gate.respond(request, propose(c, request))
    assert response["mission_judge"]["status"] == "not_consulted"
    _, response, _ = exchange(c, gate, 2, 7)
    assert response["mission_judge"]["status"] == "pending"
    assert gate.pending["folder"].name == "001"


@pytest.mark.parametrize("elapsed,status", [(20.0, "hold"), (20.001, "unavailable")])
@pytest.mark.parametrize("during_read", [False, True])
def test_real_acceptance_clock_including_read_delay(
    tmp_path, monkeypatch, elapsed, status, during_read
):
    from pathlib import Path

    clock = [0.0]
    c = config()
    gate = MissionJudgeGate(tmp_path, c, clock=lambda: clock[0])
    exchange(c, gate, 0, 0)
    answer(gate, tmp_path / "pad-judge/000", "wait", 1)
    if during_read:
        original = Path.read_text

        def slow_read(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path.name == "response.json":
                clock[0] = elapsed
            return value

        monkeypatch.setattr(Path, "read_text", slow_read)
    else:
        clock[0] = elapsed
    _, response, action = exchange(c, gate, 1, 19)  # sample is still before deadline
    assert response["mission_judge"]["status"] == status
    assert action == (WAIT if status == "hold" else ENTER)


@pytest.mark.parametrize("value", ["broken", "null", "[]"])
def test_malformed_answer_leaves_rules_entry(tmp_path, value):
    c = config()
    gate = MissionJudgeGate(tmp_path, c)
    exchange(c, gate, 0, 0)
    (tmp_path / "pad-judge/000/response.json").write_text(value)
    _, response, action = exchange(c, gate, 1, 1)
    assert action == ENTER and response["mission_judge"]["status"] == "invalid"


def test_release_reserves_mailbox_time_and_stops_clock(tmp_path):
    from scripts.verify_yokohama_pad_queue import judge_checks

    c = config()
    gate = MissionJudgeGate(tmp_path, c, clock=lambda: 0)
    request, response, _ = exchange(c, gate, 0, 0)
    exchanges = [(request, response)]
    answer(gate, tmp_path / "pad-judge/000", "wait", 30)
    request, response, _ = exchange(c, gate, 1, 1)
    exchanges.append((request, response))
    request, response, action = exchange(c, gate, 2, 28)
    exchanges.append((request, response))
    assert action == ENTER and response["mission_judge"]["status"] == "budget_exhausted"
    # The maximum permitted mailbox delay still releases at exactly 30 seconds.
    assert require_response(c, request, response, observation(35)) == ENTER
    with pytest.raises(ValueError):
        require_response(c, request, response, observation(35.001))
    request, response, action = exchange(c, gate, 3, 80)
    exchanges.append((request, response))
    assert action == ENTER and response["mission_judge"]["added_wait_s"] == 28
    checks, summary = judge_checks(c["world"]["pad_queue"]["mission_judge"], tmp_path, exchanges)
    assert checks["mission_judge_within_budget"] and summary["judge_added_wait_s"] == 28


def test_aircraft_rejects_wait_at_deadline(tmp_path):
    from src.runtime.yokohama_pad_queue import judge_overlay

    c = config()
    request = make_request(c, 1, [observation(t) for t in range(29, 35)])
    response = propose(c, request)
    response.update(
        judge_overlay(
            c["world"]["pad_queue"]["mission_judge"],
            ENTER,
            dict(prior_action=ENTER, status="hold", added_wait_s=29, wait_deadline_wall_s=35),
        )
    )
    with pytest.raises(ValueError, match="wall-clock deadline"):
        require_response(c, request, response, observation(35))


def test_executor_near_deadline_bypasses_camera_cadence(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src/runtime"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts import yokohama_pad_worker as worker

    c = config()
    c["world"]["pad_queue"].update(maximum_wait_wall_s=180, maximum_wait_sim_s=90)
    q = worker.PadQueue(tmp_path, c, None, lambda *a, **k: None)
    q.camera = SimpleNamespace()
    rows = iter([observation(24, True), observation(25), observation(26)])
    asked = []
    monkeypatch.setattr(worker, "clear_window", lambda *a: True)
    monkeypatch.setattr(q, "_permit", lambda *a: None)

    def ask(sample, evidence, row):
        asked.append(row["wall_s"])
        if len(asked) == 1:
            q.judge_deadline = 30
            return None, None, row, WAIT
        assert row["wall_s"] == 26  # one sim second; normal camera cadence would skip it
        return None, None, row, ENTER

    monkeypatch.setattr(q, "_ask", ask)
    q._await_clearance(lambda: next(rows))
    assert q.entered and asked == [25, 26]


def test_executor_deadline_without_host_response_stops(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src/runtime"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts.yokohama_pad_worker import PadQueue

    c = config()
    q = PadQueue(tmp_path, c, None, lambda *a, **k: None)
    q.judge_deadline = 35
    with pytest.raises(TimeoutError, match="budget exhausted"):
        q._ask(lambda: observation(35), [observation(t) for t in range(29, 35)], observation(34))


def test_rules_wait_after_a_judge_wait_is_not_charged_to_the_judge(tmp_path):
    """The lead returns after a judged wait: the Rules wait is not judge time, and the
    later Rules entry is neither refused nor forced to abort on the judge deadline."""
    from scripts.verify_yokohama_pad_queue import judge_checks

    c = config()
    clock = [0.0]
    gate = MissionJudgeGate(tmp_path, c, clock=lambda: clock[0])

    def ask(seq, rows):
        clock[0] = rows[-1]["wall_s"]
        request = make_request(c, seq, rows)
        response = gate.respond(request, propose(c, request))
        action = require_response(c, request, response, rows[-1])
        return request, response, action

    exchanges = [ask(0, [observation(t) for t in range(0, 6)])[:2]]
    answer(gate, tmp_path / "pad-judge/000", "wait", 6)
    exchanges.append(ask(1, [observation(t) for t in range(1, 7)])[:2])
    for seq, t in [(2, 12), (3, 40)]:  # reoccupied for longer than the judge budget
        request, response, action = ask(seq, [observation(t, occupied=True)])
        assert action == WAIT and response["mission_judge"]["status"] == "not_consulted"
        assert response["mission_judge"]["added_wait_s"] == 7
        assert "wait_deadline_wall_s" not in response["mission_judge"]
        exchanges.append((request, response))
    request, response, action = ask(4, [observation(t) for t in range(50, 56)])
    judge_request = json.loads((tmp_path / "pad-judge/001/request.json").read_text())
    assert response["mission_judge"]["status"] == "pending"
    assert judge_request["remaining_wait_seconds"] == 23
    exchanges.append((request, response))
    answer(gate, tmp_path / "pad-judge/001")
    request, response, action = ask(5, [observation(t) for t in range(51, 57)])
    assert action == ENTER and response["mission_judge"]["status"] == "no_objection"
    exchanges.append((request, response))
    checks, metrics = judge_checks(c["world"]["pad_queue"]["mission_judge"], tmp_path, exchanges)
    assert checks["mission_judge_within_budget"] and metrics["judge_added_wait_s"] == 8


def test_executor_rules_wait_clears_the_judge_deadline(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src/runtime"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts.yokohama_pad_worker import PadQueue

    c = config()
    q = PadQueue(tmp_path, c, None, lambda *a, **k: None)
    q.judge_deadline = 35
    rows = [observation(12, occupied=True)]
    folder = tmp_path / "pad-decisions" / "000"
    request = make_request(c, 0, rows)
    response = dict(
        propose(c, request),
        mission_judge=dict(prior_action=WAIT, mode="gateway", status="not_consulted"),
    )
    import threading

    def host():  # the aircraft creates the mailbox folder; answer once it exists
        deadline = time.monotonic() + 2
        while not folder.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        atomic_json(folder / "response.json", response)

    monkeypatch.setattr("scripts.yokohama_pad_worker.make_request", lambda *a: request)
    thread = threading.Thread(target=host)
    thread.start()
    q._ask(lambda: observation(12, occupied=True), rows, rows[-1])
    thread.join()
    assert q.judge_deadline is None
