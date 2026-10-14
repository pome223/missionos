"""Event-response latency counts evidenced comparison work, never a free batch."""

from copy import deepcopy
from dataclasses import replace

import pytest

from src.runtime.starship_replanning_events import EventMonitor, verify_events
from src.runtime.starship_replanning import digest, notices
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate
from tests.contract.test_starship_replanning_events import envelope, notice, observation


def comparison_event_study(*, request_time=154, intervals=((102, 142),), updates=(102,)):
    """Continuous 2 s observations with a fully bound finite forecast wait.

    These rows isolate the event-audit boundary. Full candidate preparation and
    physical admission belong to the independent comparison verifier tests.
    """
    authority = envelope()
    authority["batch_timeout_s"] = 600.0
    authority["comparison_preparation"] = {"schema": "test_timing_boundary"}
    times = {"nominal": 10000.0, "next_orbit": 16000.0}
    monitor = EventMonitor(authority, times)
    current = notice()
    for now in range(100, request_time + 1, 2):
        if now in updates:
            current = notice(
                sequence=current["sequence"] + 1,
                issued=float(now),
                text=f"Update delivered at {now}",
            )
        monitor.poll(observation(float(now)), current, None)
    trigger_ids = [row["id"] for row in monitor.drain()]
    later = float(request_time + 2)
    monitor.poll(observation(later), current, None)
    preparations, operations = [], []
    for index, (start, end) in enumerate(intervals):
        preparations.append(
            {
                "work_performed": True,
                "pending_operation_index": index,
                "start_time_s": float(start),
                "end_time_s": float(end),
                "budget": {"cutoff_time_s": 5000.0},
            }
        )
        operations.append(
            {
                "operation": "return_forecasts",
                "start_time_s": float(start),
                "end_time_s": float(end),
                "wall_time_s": float(end - start),
                "expired": False,
            }
        )
    return {
        "opportunities": times,
        "profile": {"integration": {"coast_dt_s": 2.0}},
        "execution_origin": {"time_s": 100.0},
        "execution": {"final_state": {"time_s": later}},
        "dispatch": None,
        "supervision_observations": monitor.observations,
        "supervision_events": monitor.events,
        "pending_operations": operations,
        "comparison_preparations": preparations,
        "decisions": [
            {
                "request": {
                    "time_s": float(request_time),
                    "context_generation": monitor.generation,
                    "trigger_event_ids": trigger_ids,
                    "notice": current,
                },
                "later_time_s": later,
                "later_generation": monitor.generation,
                "accepted_action": "keep_plan",
                "interrupted_by_events": False,
            }
        ],
    }, authority


def test_bound_forecast_work_extends_response_time_by_actual_duration_only():
    study, authority = comparison_event_study()
    # Detection 102 -> request 154 = 40 s forecast + 10 s model + 2 s coast.
    assert verify_events(study, authority) == []
    late, authority = comparison_event_study(request_time=156)
    assert verify_events(late, authority) == ["supervision_event_response_late"]


def test_one_preparation_binds_screen_and_admission_operation_spans():
    study, authority = comparison_event_study(intervals=((102, 122), (122, 142)))
    prep = study["comparison_preparations"][0]
    prep.update(end_time_s=142.0, pending_operation_end_index=2)
    study["comparison_preparations"] = [prep]
    assert verify_events(study, authority) == []
    # A missing admission operation cannot borrow its elapsed span as work.
    prep["pending_operation_end_index"] = 1
    issues = verify_events(study, authority)
    assert "supervision_preparation_operation_binding" in issues
    assert "supervision_event_response_late" in issues


def test_gap_between_screen_and_admission_does_not_earn_latency_credit():
    study, authority = comparison_event_study(intervals=((102, 122), (124, 142)))
    prep = study["comparison_preparations"][0]
    prep.update(end_time_s=142.0, pending_operation_end_index=2)
    study["comparison_preparations"] = [prep]
    assert verify_events(study, authority) == ["supervision_event_response_late"]


def test_empty_or_nonperformed_preparation_does_not_grant_600_second_credit():
    for missing in (True, False):
        study, authority = comparison_event_study()
        if missing:
            study["comparison_preparations"] = []
        else:
            study["comparison_preparations"][0]["work_performed"] = False
        assert verify_events(study, authority) == ["supervision_event_response_late"]


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_operation",
        "wrong_start",
        "wrong_end",
        "missing_index",
        "negative_index",
        "boolean_index",
        "past_cutoff",
        "past_batch",
        "reverse_interval",
    ],
)
def test_forecast_latency_requires_exact_pending_operation_and_budget_binding(fault):
    study, authority = comparison_event_study()
    prep, operation = study["comparison_preparations"][0], study["pending_operations"][0]
    if fault == "wrong_operation":
        operation["operation"] = "model:orbital_event"
    elif fault == "wrong_start":
        operation["start_time_s"] -= 2
    elif fault == "wrong_end":
        operation["end_time_s"] += 2
    elif fault == "missing_index":
        prep["pending_operation_index"] = 1
    elif fault == "negative_index":
        prep["pending_operation_index"] = -1
    elif fault == "boolean_index":
        prep["pending_operation_index"] = False
    elif fault == "past_cutoff":
        prep["budget"]["cutoff_time_s"] = prep["end_time_s"] - 0.1
    elif fault == "past_batch":
        authority["batch_timeout_s"] = 10.0
    else:
        prep["end_time_s"] = operation["end_time_s"] = prep["start_time_s"] - 2
    issues = verify_events(study, authority)
    assert "supervision_preparation_operation_binding" in issues
    assert "supervision_event_response_late" in issues


def test_overlapping_intervals_cannot_double_count_comparison_credit():
    study, authority = comparison_event_study(intervals=((102, 126), (114, 142)))
    assert verify_events(study, authority) == []  # Union length 40, not sum 52.
    late, authority = comparison_event_study(request_time=166, intervals=((102, 126), (114, 142)))
    assert verify_events(late, authority) == ["supervision_event_response_late"]
    # A duplicated row cannot increase the event response allowance either.
    study["comparison_preparations"].append(deepcopy(study["comparison_preparations"][0]))
    assert verify_events(study, authority) == []


def test_idle_gap_between_jobs_still_spends_response_budget():
    study, authority = comparison_event_study(request_time=164, intervals=((102, 122), (140, 160)))
    # 62 s elapsed, 40 s evidenced work: the unexplained 22 s exceeds 12 s.
    assert verify_events(study, authority) == ["supervision_event_response_late"]


def test_coalesced_update_counts_only_work_after_each_event_detection():
    study, authority = comparison_event_study(updates=(102, 130))
    assert [row["detected_at_s"] for row in study["supervision_events"]] == [102.0, 130.0]
    assert verify_events(study, authority) == []
    late, authority = comparison_event_study(request_time=156, updates=(102, 130))
    assert verify_events(late, authority) == ["supervision_event_response_late"]


def test_work_before_event_is_not_reused_as_response_latency_credit():
    study, authority = comparison_event_study(
        request_time=152, intervals=((100, 142),), updates=(130,)
    )
    # Only 12 s of the 42 s job happened after the event; 22 s latency fits 24 s.
    assert verify_events(study, authority) == []
    late, authority = comparison_event_study(
        request_time=156, intervals=((100, 142),), updates=(130,)
    )
    assert verify_events(late, authority) == ["supervision_event_response_late"]


def failed_preparation_study(monkeypatch, tmp_path, *, pending=True, candidate=False):
    # Crossing the registered update creates an undrained event; an initial
    # failure before that update exercises the equally valid no-event stop.
    before, after = (1348.0, 1350.0) if pending else (1300.0, 1302.0)
    actor, _ = actor_harness(monkeypatch, tmp_path, now=before, case="comparison_update")
    actor.s = replace(actor.s, time_s=after)
    actor.monitor.poll(actor.observation(), notices(actor.case, after), None)
    observation = actor.observation()
    candidates = {"nominal": make_candidate(actor, "nominal")} if candidate else {}
    preparation = {
        "preparation_id": "failed-preparation",
        "status": "failed",
        "end_time_s": after,
        "later_observation": observation,
        "candidate_sha256_after": {name: digest(value) for name, value in candidates.items()},
    }
    events = [
        {
            "event": "m1_comparison_stopped",
            "time_s": after,
            "reason": "comparison_preparation_unavailable",
            "preparation_id": "failed-preparation",
        }
    ]
    if pending:
        events.append(
            {
                "event": "m1_supervision_terminated",
                "time_s": after,
                "reason": "comparison_preparation_unavailable",
                "termination": "return_unresolved",
                "event_ids": [event["id"] for event in actor.monitor.events],
            }
        )
    return actor, {
        "opportunities": actor.times,
        "profile": actor.p,
        "execution_origin": {"time_s": before},
        "scheduled_s": actor.scheduled,
        "execution": {
            "final_state": {"time_s": after},
            "events": events,
            "outcome": {
                "termination": "return_unresolved",
                "contact_receipt": None,
                "stop_reason": "comparison_preparation_unavailable",
            },
        },
        "dispatch": None,
        "supervision_observations": actor.monitor.observations,
        "supervision_events": actor.monitor.events,
        "decisions": [],
        "comparison_preparations": [preparation],
        "candidates": candidates,
    }


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("status", ["failed", "incomplete", "not_started"])
def test_failed_preparation_stop_is_audited_with_and_without_events(
    monkeypatch, tmp_path, pending, status
):
    actor, study = failed_preparation_study(monkeypatch, tmp_path, pending=pending)
    study["comparison_preparations"][-1]["status"] = status
    assert verify_events(study, actor.envelope, expected_case=actor.case) == []
    assert study["execution"]["outcome"]["termination"] == "return_unresolved"


@pytest.mark.parametrize("pending", [False, True])
def test_comparison_stop_rejected_while_an_admissible_return_remains(
    monkeypatch, tmp_path, pending
):
    actor, study = failed_preparation_study(monkeypatch, tmp_path, pending=pending, candidate=True)
    assert "supervision_comparison_stop_invalid" in verify_events(
        study, actor.envelope, expected_case=actor.case
    )


@pytest.mark.parametrize("mutation", ["reason", "id", "time", "status", "missing", "scope", "old"])
def test_forged_comparison_stop_is_not_accepted_even_without_pending_events(
    monkeypatch, tmp_path, mutation
):
    actor, study = failed_preparation_study(monkeypatch, tmp_path, pending=False)
    if mutation == "reason":
        study["execution"]["outcome"]["stop_reason"] = "other"
    elif mutation == "id":
        study["execution"]["events"][0]["preparation_id"] = "different"
    elif mutation == "time":
        study["comparison_preparations"][-1]["end_time_s"] -= 2
    elif mutation == "status":
        study["comparison_preparations"][-1]["status"] = "completed"
    elif mutation == "missing":
        study["execution"]["events"] = []
    elif mutation == "scope":
        actor.envelope.pop("comparison_preparation")
    else:
        later = deepcopy(study["comparison_preparations"][-1])
        later["preparation_id"] = "latest-attempt"
        study["comparison_preparations"].append(later)
    assert "supervision_comparison_stop_invalid" in verify_events(
        study, actor.envelope, expected_case=actor.case
    )
