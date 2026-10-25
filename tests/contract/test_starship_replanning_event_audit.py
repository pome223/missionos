"""Audit notices against registered input and use the approved deadline margin."""

from copy import deepcopy

import pytest

from src.runtime.starship_replanning import contract, notices
from src.runtime.starship_replanning_events import EventMonitor, verify_events


def observation(now):
    return {
        "state": {
            "time_s": now,
            "engine_states": [{"available": True}],
            "omega_body_rad_s": [0.0, 0.0, 0.0],
        }
    }


def registered_study(*, suppress_update=False):
    e = contract("fixture", "event_normal")
    times = {"nominal": 2280.7, "next_orbit": 7731.35}
    m = EventMonitor(e, times)
    old = notices("event_normal", 1448.0)
    current = old if suppress_update else notices("event_normal", 1450.0)
    m.poll(observation(1448.0), old, None)
    m.poll(observation(1450.0), current, None)
    ids = [event["id"] for event in m.drain()]
    m.poll(observation(1452.0), current, None)
    s = {
        "opportunities": times,
        "profile": {"integration": {"coast_dt_s": 2.0}},
        "execution_origin": {"time_s": 1448.0},
        "execution": {"final_state": {"time_s": 1452.0}},
        "dispatch": None,
        "supervision_observations": m.observations,
        "supervision_events": m.events,
        "decisions": [
            {
                "request": {
                    "time_s": 1450.0,
                    "context_generation": m.generation,
                    "trigger_event_ids": ids,
                    "notice": current,
                },
                "later_time_s": 1452.0,
                "later_generation": m.generation,
                "accepted_action": "keep_plan",
                "interrupted_by_events": False,
            }
        ],
    }
    return s, e


def test_registered_notice_delivery_passes_independent_audit():
    s, e = registered_study()
    assert verify_events(s, e, expected_case="event_normal") == []


def test_self_consistent_omitted_registered_update_is_rejected():
    s, e = registered_study(suppress_update=True)
    # The self-reported stream is internally consistent but omits real scenario input.
    assert verify_events(s, e) == []
    assert verify_events(s, e, expected_case="event_normal") == [
        "supervision_registered_notice_mismatch"
    ]


def test_expected_case_is_external_and_cannot_be_replaced_by_study_case():
    s, e = registered_study()
    s["case"] = "event_normal"
    assert "supervision_registered_notice_mismatch" in verify_events(
        s, e, expected_case="event_tradeoff"
    )
    assert verify_events(s, e, expected_case="unregistered") == [
        "supervision_unknown_registered_case"
    ]


@pytest.mark.parametrize("margin", [0.0, 15.0, 120.0])
def test_approved_margin_changes_detection_and_audit_together(margin):
    e = contract("fixture", "event_normal")
    e["event_supervision"]["deadline_margin_s"] = margin
    when = 2280.0
    due = when - e["batch_timeout_s"] - e["decision_timeout_s"] - margin
    m = EventMonitor(e, {"nominal": when})
    for now in (due - 2.0, due, due + 2.0):
        m.poll(observation(now), notices("event_normal", now), None)
    assert len(m.events) == 1
    event = m.events[0]
    assert event["kind"] == "candidate_deadline" and event["due_s"] == due
    assert event["detected_at_s"] == due
    current = notices("event_normal", due)
    s = {
        "opportunities": {"nominal": when},
        "profile": {"integration": {"coast_dt_s": 2.0}},
        "execution_origin": {"time_s": due - 2.0},
        "execution": {"final_state": {"time_s": due + 2.0}},
        "dispatch": None,
        "supervision_observations": m.observations,
        "supervision_events": m.events,
        "decisions": [
            {
                "request": {
                    "time_s": due,
                    "context_generation": 1,
                    "trigger_event_ids": [event["id"]],
                    "notice": current,
                },
                "later_time_s": due + 2.0,
                "later_generation": 1,
                "accepted_action": "keep_plan",
                "interrupted_by_events": False,
            }
        ],
    }
    assert verify_events(s, e, expected_case="event_normal") == []
    changed = deepcopy(e)
    changed["event_supervision"]["deadline_margin_s"] += 10.0
    assert "supervision_event_replay" in verify_events(s, changed, expected_case="event_normal")


@pytest.mark.parametrize("margin", [-1.0, float("nan"), float("inf"), True, None])
def test_invalid_deadline_margin_fails_closed(margin):
    s, e = registered_study()
    e["event_supervision"]["deadline_margin_s"] = margin
    with pytest.raises(ValueError, match="invalid_supervision_deadline_margin"):
        EventMonitor(e, s["opportunities"])
    assert verify_events(s, e) == ["supervision_deadline_margin_invalid"]


def terminal_study():
    s, e = registered_study()
    s["decisions"] = []
    s["execution"]["outcome"] = {
        "termination": "return_unresolved",
        "contact_receipt": None,
    }
    s["execution"]["events"] = [
        {
            "event": "m1_supervision_terminated",
            "time_s": 1452.0,
            "event_ids": ["event-1"],
            "termination": "return_unresolved",
            "reason": "forecast_unavailable",
        }
    ]
    s["execution"]["events"].append({"event": "m1_tool_expired", "time_s": 1452.0})
    s["pending_operations"] = [
        {
            "operation": "return_forecasts",
            "start_time_s": 1448.0,
            "end_time_s": 1452.0,
            "expired": True,
        }
    ]
    return s, e


def test_unhandled_events_require_explicit_matching_termination_disposition():
    s, e = terminal_study()
    assert verify_events(s, e, expected_case="event_normal") == []
    s["execution"]["events"].clear()
    assert "supervision_event_unhandled" in verify_events(s, e, expected_case="event_normal")


@pytest.mark.parametrize(
    "fault",
    [
        "missing_reason",
        "wrong_time",
        "wrong_termination",
        "missing_event_id",
        "unknown_event_id",
        "duplicate_marker",
        "contact_claim",
        "invented_health",
    ],
)
def test_unresolved_disposition_cannot_hide_unaccounted_events(fault):
    s, e = terminal_study()
    marker = s["execution"]["events"][0]
    if fault == "missing_reason":
        marker["reason"] = " "
    if fault == "wrong_time":
        marker["time_s"] = 1450.0
    if fault == "wrong_termination":
        marker["termination"] = "health_inhibited_unresolved"
    if fault == "missing_event_id":
        marker["event_ids"] = []
    if fault == "unknown_event_id":
        marker["event_ids"] = ["event-2"]
    if fault == "duplicate_marker":
        s["execution"]["events"].append(deepcopy(marker))
    if fault == "contact_claim":
        s["execution"]["outcome"]["contact_receipt"] = {"claim": "success"}
    if fault == "invented_health":
        s["execution"]["outcome"]["termination"] = "health_inhibited_unresolved"
        marker["termination"] = "health_inhibited_unresolved"
        marker["reason"] = "observed_health_outside_delegated_domain"
    assert "supervision_event_unhandled" in verify_events(s, e, expected_case="event_normal")


def test_terminal_marker_cannot_replace_a_successful_dispatch_or_consumed_event():
    s, e = terminal_study()
    s["dispatch"] = {"time_s": 1452.0}
    assert "supervision_termination_disposition_invalid" in verify_events(s, e)
    s, e = registered_study()
    s["execution"]["events"] = terminal_study()[0]["execution"]["events"]
    assert "supervision_termination_disposition_invalid" in verify_events(s, e)


@pytest.mark.parametrize(
    "fault", ["unsupported_reason", "missing_tool_event", "not_expired", "stale_failure"]
)
def test_terminal_forecast_reason_requires_matching_failure_witness(fault):
    s, e = terminal_study()
    if fault == "unsupported_reason":
        s["execution"]["events"][0]["reason"] = "unexplained_early_stop"
    if fault == "missing_tool_event":
        s["execution"]["events"].pop()
    if fault == "not_expired":
        s["pending_operations"][0]["expired"] = False
    if fault == "stale_failure":
        s["pending_operations"][0]["end_time_s"] = 1450.0
    assert "supervision_event_unhandled" in verify_events(s, e, expected_case="event_normal")


def test_terminal_deadline_reason_requires_actual_last_opportunity_cutoff():
    e = contract("fixture", "event_normal")
    times = {"nominal": 2000.0, "next_orbit": 3000.0}
    for now in (2900.0, 2960.0):
        m = EventMonitor(e, times)
        m.poll(observation(now), notices("event_normal", now), None)
        s = {
            "opportunities": times,
            "profile": {"integration": {"coast_dt_s": 2.0}},
            "execution_origin": {"time_s": now},
            "execution": {
                "final_state": {"time_s": now},
                "outcome": {"termination": "return_unresolved", "contact_receipt": None},
                "events": [
                    {
                        "event": "m1_supervision_terminated",
                        "time_s": now,
                        "event_ids": [event["id"] for event in m.events],
                        "termination": "return_unresolved",
                        "reason": "return_decision_deadline_elapsed",
                    }
                ],
            },
            "dispatch": None,
            "supervision_observations": m.observations,
            "supervision_events": m.events,
            "decisions": [],
        }
        result = verify_events(s, e, expected_case="event_normal")
        assert ("supervision_event_unhandled" in result) == (now < 2960.0)
        if now == 2960.0:
            assert result == []


def test_same_time_guarded_deadline_fallback_can_drain_notice_without_model_wait():
    s, e = registered_study()
    s["supervision_observations"].pop()
    s["execution"]["final_state"]["time_s"] = 1450.0
    record = s["decisions"][0]
    record["later_time_s"] = 1450.0
    record["accepted_action"] = None
    assert "supervision_decision_context" in verify_events(s, e, expected_case="event_normal")
    record["request_disposition"] = "deadline_fallback"
    assert verify_events(s, e, expected_case="event_normal") == []
