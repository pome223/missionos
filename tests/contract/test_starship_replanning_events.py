"""Orbital event detection and independently replayed authority evidence."""

from copy import deepcopy

import pytest

from src.runtime.starship_replanning_events import EventMonitor, verify_events


def envelope():
    return {
        "event_supervision": {"maximum_coast_body_rate_rad_s": 0.02, "deadline_margin_s": 60.0},
        "batch_timeout_s": 100.0,
        "decision_timeout_s": 10.0,
    }


def observation(time_s, *, engines=(True, True, True), rate=(0.0, 0.0, 0.0)):
    return {
        "state": {
            "time_s": time_s,
            "engine_states": [{"available": a} for a in engines],
            "omega_body_rad_s": list(rate),
        }
    }


def notice(*, sequence=1, issued=90.0, expires=5000.0, text="Original advisory"):
    return {
        "sequence": sequence,
        "issued_at_s": issued,
        "expires_at_s": expires,
        "areas": {"east": [4500.0, 5600.0]},
        "text": text,
    }


def test_observed_content_change_same_sequence_and_duplicate_polls():
    m = EventMonitor(envelope(), {"nominal": 1000.0})
    first = notice()
    assert m.poll(observation(100.0), first, None) == []
    first["text"] = "The sender mutated its own object"
    assert m.observations[0]["notice"]["text"] == "Original advisory"
    changed = notice(text="Corrected advisory, unchanged clearance")
    events = m.poll(observation(102.0), changed, None)
    assert [e["kind"] for e in events] == ["notice_updated"]
    assert m.generation == 1 and len(m.pending) == 1
    assert m.poll(observation(102.0), changed, None) == []
    assert m.poll(observation(104.0), changed, None) == []
    assert m.observations[-1]["notice"] is None
    assert m.generation == 1 and len(m.events) == 1
    drained = m.drain()
    drained[0]["urgent"] = True
    assert not m.pending and not m.events[0]["urgent"]


def test_events_coalesce_and_health_inhibit_is_latched():
    m = EventMonitor(envelope(), {"nominal": 1000.0, "next_orbit": 2000.0})
    m.poll(observation(828.0), notice(), None)
    n = notice(sequence=2, issued=829.0)
    new = m.poll(observation(830.0, engines=(True, False, True)), n, "nominal")
    assert [e["kind"] for e in new] == ["notice_updated", "health_changed", "candidate_deadline"]
    assert {e["generation"] for e in new} == {1}
    assert [e["id"] for e in new] == ["event-1", "event-2", "event-3"]
    assert m.inhibited
    assert m.poll(observation(832.0, engines=(True, False, True)), n, None) == []
    recovered = m.poll(observation(834.0), n, None)
    assert len(recovered) == 1 and not recovered[0]["urgent"]
    assert m.inhibited  # No unapproved automatic re-enable.
    m.poll(observation(1830.0), n, None)
    deadlines = [e for e in m.events if e["kind"] == "candidate_deadline"]
    assert [e["candidate_id"] for e in deadlines] == ["nominal", "next_orbit"]
    assert m.poll(observation(1832.0), n, None) == []


def test_expiry_without_replacement_and_initial_invalid_health():
    m = EventMonitor(envelope(), {"nominal": 1000.0})
    n = notice(expires=104.0)
    m.poll(observation(100.0, rate=(0.02, 0.0, 0.0)), n, None)
    assert not m.inhibited
    events = m.poll(observation(104.0, rate=(0.020001, 0.0, 0.0)), n, None)
    assert [e["kind"] for e in events] == ["notice_expired", "health_changed"]
    assert m.inhibited
    assert m.poll(observation(106.0, rate=(0.020001, 0.0, 0.0)), n, None) == []
    initial = EventMonitor(envelope(), {"nominal": 1000.0})
    assert initial.poll(observation(100.0, engines=(False,)), n, None)[0]["urgent"]
    assert initial.inhibited


@pytest.mark.parametrize("fault", ["future_notice", "nan_rate", "fake_engine", "reverse_time"])
def test_invalid_observed_input_is_not_treated_as_normal(fault):
    m = EventMonitor(envelope(), {"nominal": 1000.0})
    m.poll(observation(100.0), notice(), None)
    o, n = observation(102.0), notice()
    if fault == "future_notice":
        n["issued_at_s"] = 104.0
    if fault == "nan_rate":
        o["state"]["omega_body_rad_s"][0] = float("nan")
    if fault == "fake_engine":
        o["state"]["engine_states"][0]["available"] = 1
    if fault == "reverse_time":
        o["state"]["time_s"] = 99.0
    with pytest.raises(ValueError):
        m.poll(o, n, None)


def study():
    e, times = envelope(), {"nominal": 1000.0, "next_orbit": 2000.0}
    m = EventMonitor(e, times)
    old, new = notice(), notice(sequence=2, issued=102.0)
    m.poll(observation(100.0), old, None)
    m.poll(observation(102.0), new, None)
    trigger_ids = [event["id"] for event in m.drain()]
    m.poll(observation(104.0), new, None)
    s = {
        "opportunities": times,
        "profile": {"integration": {"coast_dt_s": 2.0}},
        "execution_origin": {"time_s": 100.0},
        "execution": {"final_state": {"time_s": 104.0}},
        "dispatch": None,
        "supervision_observations": m.observations,
        "supervision_events": m.events,
        "decisions": [
            {
                "request": {
                    "time_s": 100.0,
                    "context_generation": 0,
                    "trigger_event_ids": [],
                    "notice": old,
                },
                "later_time_s": 102.0,
                "later_generation": 1,
                "accepted_action": None,
                "interrupted_by_events": True,
            },
            {
                "request": {
                    "time_s": 102.0,
                    "context_generation": 1,
                    "trigger_event_ids": trigger_ids,
                    "notice": new,
                },
                "later_time_s": 104.0,
                "later_generation": 1,
                "accepted_action": "keep_plan",
                "interrupted_by_events": False,
            },
        ],
    }
    return s, e


def test_independent_replay_accepts_observed_interruption_and_redecision():
    s, e = study()
    assert verify_events(s, e) == []


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("missing_event", "supervision_event_replay"),
        ("forged_event", "supervision_event_replay"),
        ("wrong_generation", "supervision_event_replay"),
        ("lost_trigger", "supervision_event_drain"),
        ("wrong_context", "supervision_decision_context"),
        ("wrong_later_context", "supervision_decision_context"),
        ("accepted_stale_response", "supervision_stale_response_accepted"),
        ("hidden_interruption", "supervision_stale_response_accepted"),
        ("stale_request_notice", "supervision_request_notice_binding"),
        ("mutated_notice", "supervision_notice_evidence"),
        ("missing_poll", "supervision_observation_coverage"),
        ("unobserved_end", "supervision_end_coverage"),
    ],
)
def test_independent_replay_rejects_mutated_audit_evidence(fault, expected):
    s, e = study()
    if fault == "missing_event":
        s["supervision_events"].clear()
    if fault == "forged_event":
        s["supervision_events"].append(deepcopy(s["supervision_events"][0]))
    if fault == "wrong_generation":
        s["supervision_events"][0]["generation"] += 1
    if fault == "lost_trigger":
        s["decisions"][1]["request"]["trigger_event_ids"] = []
    if fault == "wrong_context":
        s["decisions"][1]["request"]["context_generation"] = 0
    if fault == "wrong_later_context":
        s["decisions"][0]["later_generation"] = 0
    if fault == "accepted_stale_response":
        s["decisions"][0]["accepted_action"] = "select_nominal"
    if fault == "hidden_interruption":
        s["decisions"][0]["interrupted_by_events"] = False
    if fault == "stale_request_notice":
        s["decisions"][1]["request"]["notice"] = s["decisions"][0]["request"]["notice"]
    if fault == "mutated_notice":
        s["supervision_observations"][1]["notice"]["text"] = "Changed recorded text"
    if fault == "missing_poll":
        s["supervision_observations"].pop(1)
    if fault == "unobserved_end":
        s["execution"]["final_state"]["time_s"] = 110.0
    assert expected in verify_events(s, e)


def test_health_inhibit_cannot_be_ignored_at_dispatch():
    s, e = study()
    m = EventMonitor(e, s["opportunities"])
    m.poll(observation(100.0, engines=(False, True, True)), notice(), None)
    m.poll(observation(102.0), notice(), None)
    m.poll(observation(104.0), notice(), None)
    s["supervision_observations"], s["supervision_events"] = m.observations, m.events
    s["decisions"] = []
    s["dispatch"] = {"time_s": 104.0}
    assert "supervision_dispatch_after_health_inhibit" in verify_events(s, e)
    s["dispatch"] = None
    assert "supervision_event_unhandled" in verify_events(s, e)
    # Only an explicitly recorded immediate terminal disposition may consume
    # the urgent event without a model decision or physical recovery claim.
    s["supervision_observations"] = m.observations[:1]
    s["supervision_events"] = m.events[:1]
    s["execution"]["final_state"]["time_s"] = 100.0
    s["execution"]["outcome"] = {
        "termination": "health_inhibited_unresolved",
        "contact_receipt": None,
    }
    s["execution"]["events"] = [
        {"event": "m1_urgent_return_inhibited", "time_s": 100.0},
        {
            "event": "m1_supervision_terminated",
            "time_s": 100.0,
            "event_ids": ["event-1"],
            "termination": "health_inhibited_unresolved",
            "reason": "observed_health_outside_delegated_domain",
        },
    ]
    assert verify_events(s, e) == []


def test_unhandled_notice_event_cannot_be_followed_by_dispatch():
    s, e = study()
    s["decisions"] = s["decisions"][:1]
    s["dispatch"] = {"time_s": 104.0}
    assert "supervision_dispatch_with_pending_event" in verify_events(s, e)


def test_late_notice_response_fails_but_forecast_deadline_can_wait_for_batch():
    e = envelope()
    times = {"nominal": 1000.0}
    for kind in ("notice_updated", "candidate_deadline"):
        m = EventMonitor(e, times)
        old = notice()
        for now in range(828, 852, 2):
            n = notice(sequence=2, issued=830.0) if kind == "notice_updated" and now >= 830 else old
            m.poll(observation(float(now)), n, None)
        ids = [event["id"] for event in m.drain()]
        m.poll(observation(852.0), n, None)
        s = {
            "opportunities": times,
            "profile": {"integration": {"coast_dt_s": 2.0}},
            "execution_origin": {"time_s": 828.0},
            "execution": {"final_state": {"time_s": 852.0}},
            "dispatch": None,
            "supervision_observations": m.observations,
            "supervision_events": m.events,
            "decisions": [
                {
                    "request": {
                        "time_s": 850.0,
                        "context_generation": m.generation,
                        "trigger_event_ids": ids,
                        "notice": n,
                    },
                    "later_time_s": 852.0,
                    "later_generation": m.generation,
                    "accepted_action": "keep_plan",
                    "interrupted_by_events": False,
                }
            ],
        }
        issues = verify_events(s, e)
        assert ("supervision_event_response_late" in issues) == (kind == "notice_updated")
        if kind == "candidate_deadline":
            assert issues == []


def test_reaffirming_unchanged_booking_does_not_exhaust_plan_revisions():
    """Valid periodic reaffirmations must not terminate a later return opportunity."""
    from types import SimpleNamespace

    from src.runtime.starship_replanning_executor import ReplanningExecutor

    actor = ReplanningExecutor.__new__(ReplanningExecutor)
    actor.case = "event_normal"
    actor.s = SimpleNamespace(time_s=1500.0)
    actor.envelope = {"maximum_plan_revisions": 2}
    actor.candidates = {"next_orbit": {"version": 1}}
    actor.revisions = []
    actor.revision = 0
    actor.selected = None
    actor.decisions = [{"request": {"request_id": "first-actual-selection"}}]
    actor.checks = lambda notice: {"next_orbit": {"accepted": True}}
    actor.observation = lambda: {"state": {"time_s": actor.s.time_s}}
    assert actor.select("next_orbit", "ai")
    original_revision = deepcopy(actor.revisions[0])
    for i in range(8):
        actor.s.time_s += 2.0
        actor.decisions.append({"request": {"request_id": f"reaffirm-{i}"}})
        assert actor.select("next_orbit", "ai")
        assert actor.revision == 1 and actor.revisions == [original_revision]
    # New forecast evidence is a real revision, even at the same opportunity.
    actor.candidates["next_orbit"]["version"] = 2
    assert actor.select("next_orbit", "ai") and actor.revision == 2
    assert actor.select("next_orbit", "ai") and actor.revision == 2
    actor.candidates["next_orbit"]["version"] = 3
    assert not actor.select("next_orbit", "ai") and actor.revision == 2
    # Idempotency never bypasses a fresh admission failure.
    actor.candidates["next_orbit"]["version"] = 2
    actor.checks = lambda notice: {"next_orbit": {"accepted": False}}
    assert not actor.select("next_orbit", "ai")
