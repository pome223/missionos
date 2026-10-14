"""Actual core ordering for ascent latch, without running the plant."""

from copy import deepcopy
import pytest
from src.runtime.starship_deployment_handoff_verifier import _sequence_evidence
from src.runtime.starship_m2_verifier import _verified_ascent_sequence


def record(before, later, action):
    return {
        "clock_id": "ship",
        "request": {"point": "deployment_start", "observation": deepcopy(before)},
        "dispatch": {"action": action, "time_s": before["time_s"], "observation": deepcopy(before)},
        "later_observation": deepcopy(later),
    }


@pytest.fixture
def prefix():
    rows = [
        {"time_s": t, "released_count": 0, "sequencer_state": seq}
        for t, seq in ((10.0, "running"), (12.0, "skipped"), (14.0, "skipped"))
    ]
    trace = [{"kind": "observation", "row": row} for row in rows]
    trace.insert(2, {"kind": "command", "action": "stop_deployment", "time_s": 12.0})
    run = {
        "events": [
            {"event": "supervised_deployment_withheld", "time_s": 12.0},
            {
                "event": "managed_mission_command",
                "time_s": 12.0,
                "point": "deployment_start",
                "action": "stop_deployment",
            },
        ],
        "mission_director": {"records": [record(rows[1], rows[2], "stop_deployment")]},
    }
    return {"prefix_run": run, "trace": trace, "handoff": {"identity": "same exact handoff"}}


def test_same_tick_ascent_latch_precedes_observation_and_stop_command(prefix):
    _sequence_evidence(prefix["prefix_run"], prefix["trace"], ascent_withhold_verified=True)


def test_legacy_m2_cannot_add_forged_ascent_event_to_close_sequence(prefix):
    with pytest.raises(ValueError, match="requires_verified_prefix"):
        _sequence_evidence(prefix["prefix_run"], prefix["trace"])


@pytest.mark.parametrize(
    "mutation",
    ["missing_event", "late_event", "duplicate_event", "false_hold_expiry", "false_resume"],
)
def test_false_latch_or_fabricated_hold_and_resume_remain_rejected(prefix, mutation):
    events = prefix["prefix_run"]["events"]
    if mutation == "missing_event":
        events.pop(0)
    elif mutation == "late_event":
        events[0]["time_s"] = 13.0
    elif mutation == "duplicate_event":
        events.insert(1, deepcopy(events[0]))
    elif mutation == "false_hold_expiry":
        events.append({"event": "managed_hold_expired", "time_s": 14.0})
    else:
        events.append({"event": "managed_deployment_resumed", "time_s": 14.0})
    with pytest.raises(ValueError):
        _sequence_evidence(prefix["prefix_run"], prefix["trace"], ascent_withhold_verified=True)


def test_continue_cannot_clear_latched_skip(prefix):
    prefix["prefix_run"]["events"][1]["action"] = "continue"
    prefix["prefix_run"]["mission_director"]["records"][0]["dispatch"]["action"] = "continue"
    prefix["trace"][2]["action"] = "continue"
    _sequence_evidence(prefix["prefix_run"], prefix["trace"], ascent_withhold_verified=True)
    prefix["trace"][-1]["row"]["sequencer_state"] = "running"
    with pytest.raises(ValueError, match="not_backed"):
        _sequence_evidence(prefix["prefix_run"], prefix["trace"], ascent_withhold_verified=True)


def study_from(prefix):
    return {
        "deployment_run": deepcopy(prefix["prefix_run"]),
        "trace": deepcopy(prefix["trace"]),
        "handoff": deepcopy(prefix["handoff"]),
        "concurrent_prefix": deepcopy(prefix),
    }


def test_m2_requires_independent_prefix_and_exact_cross_binding(prefix, monkeypatch):
    from src.runtime import starship_m3_prefix_verifier as verifier

    seen = []
    monkeypatch.setattr(
        verifier, "verify", lambda p: seen.append(p) or {"passed": True, "handoff_ready": True}
    )
    study = study_from(prefix)
    assert _verified_ascent_sequence(study) is True and seen == [prefix]
    for key in ("deployment_run", "trace", "handoff"):
        changed = study_from(prefix)
        changed[key] = {"different": True}
        with pytest.raises((ValueError, KeyError)):
            _verified_ascent_sequence(changed)
    for result in (
        {"passed": False, "handoff_ready": True},
        {"passed": True, "handoff_ready": False},
    ):
        monkeypatch.setattr(verifier, "verify", lambda _: result)
        with pytest.raises(ValueError, match="independent_verification"):
            _verified_ascent_sequence(study)


def test_m2_without_latch_does_not_need_an_ascent_producer_record(prefix):
    study = study_from(prefix)
    study["deployment_run"]["events"].pop(0)
    study.pop("concurrent_prefix")
    assert _verified_ascent_sequence(study) is False
