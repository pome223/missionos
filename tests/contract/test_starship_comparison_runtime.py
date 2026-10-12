"""Bounded comparison preparation through the production orbital actor.

Finite plant and worker doubles test deadlines, cancellations and observed-only
records; these tests do not establish numerical flight or AI decision quality.
"""

from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

from src.runtime import starship_replanning_executor as runtime
from src.runtime.starship_replanning import digest, notices


@pytest.fixture
def harness():
    path = Path(__file__).with_name("test_starship_replanning_event_runtime.py")
    spec = importlib.util.spec_from_file_location("_comparison_runtime_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def comparison_actor(monkeypatch, tmp_path, harness, *, now=1000.0):
    actor, clock = harness.actor_harness(monkeypatch, tmp_path, now=now, case="event_tradeoff")
    actor.envelope["comparison_preparation"] = {
        "minimum_start_budget_s": 180.0,
    }
    return actor, clock


def successful_calculator(actor, harness, calls):
    def calculate(names, **budget):
        calls.append((list(names), budget))
        actor.forecast_count += 4 * len(names)
        actor.step()
        actor.candidates.update({name: harness.make_candidate(actor, name) for name in names})
        actor.last_calculation_result = {"status": "completed", "reason": "predictions_available"}
        return True

    return calculate


def test_initial_prepares_nominal_then_update_reuses_it_and_adds_alternative(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness)
    calls = []
    actor.calculate = successful_calculator(actor, harness, calls)
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    nominal = deepcopy(actor.candidates["nominal"])
    assert calls[0][0] == ["nominal"]
    actor.selected = "nominal"
    actor.s = replace(actor.s, time_s=1500.0)
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    row = actor.comparison_preparations[-1]
    assert calls[1][0] == ["next_orbit"]
    assert row["reused_candidate_ids"] == ["nominal"]
    assert actor.candidates["nominal"] == nominal
    assert actor.selected == "nominal"
    assert row["selected_before"] == row["selected_after"]
    assert row["candidate_snapshots_after"][digest(nominal)] == nominal
    assert actor.comparison_context["ready"] is True
    assert row["budget"]["opportunity_cutoff_time_s"] == 2183.0
    assert row["budget"]["cutoff_time_s"] == 2100.0


def test_sequence_and_issue_time_only_update_does_not_start_more_work(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness)
    calls = []
    actor.calculate = successful_calculator(actor, harness, calls)
    notice = notices(actor.case, actor.s.time_s)
    actor.prepare_comparison(notice)
    notice.update(sequence=99, issued_at_s=1100.0)
    actor.prepare_comparison(notice)
    assert len(calls) == len(actor.comparison_preparations) == 1


@pytest.mark.parametrize(
    "changed_field", ["areas", "text", "recovery_service_forecast", "expires_at_s"]
)
def test_operational_content_change_has_new_material_generation(changed_field):
    first = notices("event_tradeoff", 1000.0)
    changed = deepcopy(first)
    changed[changed_field] = "changed-observed-content"
    assert runtime.ReplanningExecutor.comparison_generation(
        first
    ) != runtime.ReplanningExecutor.comparison_generation(changed)


def test_late_update_skips_optional_computation_and_preserves_booking(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness, now=2100.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    actor.comparison_generations = {actor.comparison_generation(notices(actor.case, 1000.0))}
    actor.comparison_preparations = []
    actor.calculate = lambda *args, **kwargs: pytest.fail("insufficient protected time")
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    row = actor.comparison_preparations[-1]
    assert row["status"] == "not_started"
    assert row["reason"] == "booking_or_candidate_deadline"
    assert row["budget"]["available_sim_s"] == 83.0
    assert row["selected_before"] == row["selected_after"]
    assert actor.selected == "nominal"


def test_failed_batch_is_not_retried_without_new_material_notice(monkeypatch, tmp_path, harness):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    actor.comparison_generations = {actor.comparison_generation(notices(actor.case, 1000.0))}
    actor.comparison_preparations = []
    actor.envelope["comparison_preparation"]["synthetic_tool_failure"] = "cancel_on_material_update"
    # Update envelope hash to the actual approved opt-in scope.
    actor.candidates["nominal"]["envelope_sha256"] = digest(actor.envelope)
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=1000.0)
    notice = notices(actor.case, actor.s.time_s)
    actor.prepare_comparison(notice)
    first = deepcopy(actor.comparison_preparations[-1])
    actor.prepare_comparison(notice)
    assert len(pools) == 1
    assert pools[0].terminated and pools[0].closed
    assert len(actor.comparison_preparations) == 1
    assert first["reason"] == "synthetic_tool_cancellation"
    assert first["selected_after"]["candidate_id"] == "nominal"
    assert actor.forecast_count == 4
    assert actor.pending[-1]["error_type"] == "RuntimeError"


def test_wait_cutoff_terminates_unfinished_worker_before_booked_burn(
    monkeypatch, tmp_path, harness
):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=2100.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=1000.0)
    assert not actor.calculate(["next_orbit"], budget_s=500.0, cutoff_time_s=2183.0)
    assert actor.s.time_s <= 2183.0
    assert actor.s.time_s + actor.envelope["decision_timeout_s"] + 60.0 < 2280.0
    assert actor.selected == "nominal"
    assert set(actor.candidates) == {"nominal"}
    assert pools[0].terminated and pools[0].closed
    assert actor.pending[-1]["cutoff_reached"] is True
    operation = actor.pending[-1]
    assert operation["end_time_s"] - operation["start_time_s"] >= operation["wall_time_s"]


def test_forecast_exception_leaves_booking_intact(monkeypatch, tmp_path, harness):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=0.0)
    original = runtime.ProcessPoolExecutor

    class BrokenPool(original):
        def submit(self, function, job):
            future = super().submit(function, job)

            def failed():
                raise ArithmeticError("synthetic numerical failure")

            future.result = failed
            return future

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", BrokenPool)
    assert not actor.calculate(["next_orbit"], budget_s=600.0, cutoff_time_s=2183.0)
    assert actor.last_calculation_result["reason"] == "forecast_worker_exception"
    assert actor.pending[-1]["error_type"] == "ArithmeticError"
    assert actor.selected == "nominal"
    assert pools[0].terminated and pools[0].closed


def test_notice_during_preparation_discards_work_and_refreshes_generation(
    monkeypatch, tmp_path, harness
):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1448.0)
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=100.0)
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    row = actor.comparison_preparations[-1]
    assert row["status"] == "interrupted"
    assert row["reason"] == "material_notice_changed"
    assert pools[0].terminated and pools[0].closed
    assert actor.comparison_context["ready"] is False
    assert actor.candidates == {}
    calls = []
    actor.calculate = successful_calculator(actor, harness, calls)
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    assert calls[0][0] == ["nominal", "next_orbit"]
    assert actor.comparison_context["ready"] is True
    # The event remains available for choose() to bind exactly once.
    assert any(event["kind"] == "notice_updated" for event in actor.monitor.pending)


def test_forecast_budget_is_accounted_even_when_optional_work_fails(monkeypatch, tmp_path, harness):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    actor.comparison_generations = {actor.comparison_generation(notices(actor.case, 1000.0))}
    actor.comparison_preparations = []
    actor.forecast_count = actor.envelope["maximum_forecasts"] - 3
    actor.calculate = lambda *args, **kwargs: pytest.fail("forecast count cap")
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    assert actor.comparison_preparations[-1]["reason"] == "forecast_budget_exhausted"
    assert not actor.comparison_preparations[-1]["work_performed"]


def test_whole_orbital_loop_prepares_before_model_and_drains_events_once(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness)
    calls = []
    actor.calculate = successful_calculator(actor, harness, calls)
    result = actor.run_event_supervision()
    assert result["candidate_id"] == "next_orbit"
    assert result["time_s"] == actor.times["next_orbit"]
    assert [names for names, _ in calls] == [["nominal"], ["next_orbit"]]
    assert actor.forecast_count == 8
    assert len(actor.comparison_preparations) == 2
    corrected = [r for r in actor.decisions if r["request"]["notice"]["sequence"] == 2]
    assert corrected
    comparison = corrected[0]["request"]
    assert comparison["comparison_context"]["ready"] is True
    assert set(comparison["candidates"]) == {"nominal", "next_orbit"}
    for name, candidate in comparison["candidates"].items():
        cost = candidate["comparison_material"]
        assert cost["return_time_s"] == actor.times[name]
        assert cost["waiting_at_burn"]["elapsed_wait_s"] == actor.times[name] - actor.scheduled
    assert corrected[0]["request_observation"]["state"]["time_s"] == comparison["time_s"]
    assert len(actor.decisions) < 12
    handled = [event for row in actor.decisions for event in row["request"]["trigger_event_ids"]]
    assert len(handled) == len(set(handled))


def test_whole_loop_optional_failure_still_dispatches_existing_nominal(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness)
    calls = []
    successful = successful_calculator(actor, harness, calls)

    def sometimes_fails(names, **kwargs):
        if names == ["next_orbit"]:
            calls.append((list(names), kwargs))
            actor.forecast_count += 4
            actor.step()
            actor.last_calculation_result = {
                "status": "failed",
                "reason": "forecast_worker_exception",
            }
            return False
        return successful(names, **kwargs)

    actor.calculate = sometimes_fails
    result = actor.run_event_supervision()
    assert result["candidate_id"] == "nominal"
    assert result["time_s"] == actor.times["nominal"]
    assert [names for names, _ in calls] == [["nominal"], ["next_orbit"]]
    assert actor.comparison_preparations[-1]["status"] == "failed"
    assert actor.contact == {"contact": True}
    assert all("evaluate_returns" not in r["request"]["allowed_actions"] for r in actor.decisions)


def test_notice_reverting_to_prior_material_starts_new_generation_without_busy_retry(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness)
    calls = []
    actor.calculate = successful_calculator(actor, harness, calls)
    initial = notices(actor.case, actor.s.time_s)
    current_notice = [initial]
    monkeypatch.setattr(runtime, "notices", lambda *_: deepcopy(current_notice[0]))
    actor.prepare_comparison(initial)
    actor.s = replace(actor.s, time_s=1500.0)
    current_notice[0] = notices(actor.case, actor.s.time_s)
    actor.prepare_comparison(current_notice[0])
    current_notice[0] = {**initial, "sequence": 3, "issued_at_s": 1505.0}
    actor.s = replace(actor.s, time_s=1506.0)
    actor.prepare_comparison(current_notice[0])
    actor.prepare_comparison(current_notice[0])
    assert len(actor.comparison_preparations) == 3
    assert (
        actor.comparison_preparations[0]["generation_sha256"]
        == actor.comparison_preparations[2]["generation_sha256"]
    )
    assert actor.comparison_context["generation_sha256"] == actor.comparison_generation(initial)
    assert actor.comparison_context["ready"] is True
    assert len(calls) == 2  # Both predictions remain fresh on the retraction.


@pytest.mark.parametrize(
    "case",
    [
        "comparison_normal",
        "comparison_update",
        "comparison_late",
        "comparison_tool_failure",
        "comparison_timeout",
    ],
)
def test_registered_comparison_cases_run_finite_actor_with_approved_bounds(
    monkeypatch, tmp_path, harness, case
):
    actor, _ = harness.actor_harness(monkeypatch, tmp_path, now=1000.0, case=case)
    calls = []
    successful = successful_calculator(actor, harness, calls)

    def approved_calculate(names, **kwargs):
        if kwargs.get("synthetic_failure"):
            calls.append((list(names), kwargs))
            actor.forecast_count += 4 * len(names)
            actor.step()
            actor.last_calculation_result = {
                "status": "failed",
                "reason": "synthetic_tool_cancellation",
            }
            return False
        return successful(names, **kwargs)

    actor.calculate = approved_calculate
    result = actor.run_event_supervision()
    assert result["candidate_id"] == "nominal"
    assert result["time_s"] == actor.times["nominal"]
    assert actor.contact == {"contact": True}
    assert calls[0][0] == ["nominal"]
    if case == "comparison_normal":
        assert len(calls) == len(actor.comparison_preparations) == 1
    elif case == "comparison_late":
        assert len(calls) == 1
        assert actor.comparison_preparations[-1]["reason"] == "booking_or_candidate_deadline"
    elif case == "comparison_tool_failure":
        assert len(calls) == 2
        assert actor.comparison_preparations[-1]["reason"] == "synthetic_tool_cancellation"
    else:
        assert len(calls) == 2
        assert actor.comparison_context["ready"]
    if case == "comparison_timeout":
        later = [r for r in actor.decisions if r["request"]["notice"]["sequence"] >= 2]
        assert later and all(r["response"] is None for r in later)
        assert all(r["fallback"] for r in later)


@pytest.mark.parametrize("fault", ["constructor", "shutdown"])
def test_tool_lifecycle_wall_time_advances_finite_plant(monkeypatch, tmp_path, harness, fault):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    harness.asynchronous_pool(monkeypatch, clock, completion_s=0.0)
    original = runtime.ProcessPoolExecutor

    class DelayedPool(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            if fault == "constructor":
                clock.now += 5.0
                raise RuntimeError("synthetic startup error")

        def shutdown(self, **kwargs):
            super().shutdown(**kwargs)
            if fault == "shutdown":
                clock.now += 5.0

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", DelayedPool)
    result = actor.calculate(["next_orbit"], budget_s=600.0, cutoff_time_s=2183.0)
    assert result is (fault == "shutdown")
    operation = actor.pending[-1]
    assert operation["wall_time_s"] == 5.0
    assert operation["end_time_s"] - operation["start_time_s"] >= 5.0
    assert operation["clock_accounting_overrun"] is False
    assert actor.selected == "nominal"


def test_cleanup_crossing_cutoff_is_explicit_failure_not_hidden_frozen_clock(
    monkeypatch, tmp_path, harness
):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=2178.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    harness.asynchronous_pool(monkeypatch, clock, completion_s=0.0)
    original = runtime.ProcessPoolExecutor

    class SlowShutdown(original):
        def shutdown(self, **kwargs):
            super().shutdown(**kwargs)
            clock.now += 7.0

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", SlowShutdown)
    assert not actor.calculate(["next_orbit"], budget_s=5.0, cutoff_time_s=2183.0)
    assert actor.pending[-1]["clock_accounting_overrun"] is True
    assert actor.pending[-1]["wall_time_s"] == 7.0
    assert actor.last_calculation_result["reason"] == "forecast_clock_accounting_overrun"
    assert actor.s.time_s <= 2183.0
    assert actor.selected == "nominal"
    assert set(actor.candidates) == {"nominal"}


@pytest.mark.parametrize("completion_s", [0.0, 3.0])
def test_initial_forecast_survives_metadata_only_notice_and_retains_event(
    monkeypatch, tmp_path, harness, completion_s
):
    actor, clock = harness.actor_harness(
        monkeypatch, tmp_path, now=1348.0, case="comparison_normal"
    )
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=completion_s)
    assert actor.calculate(["nominal"], budget_s=600.0, cutoff_time_s=2183.0)
    assert set(actor.candidates) == {"nominal"}
    assert actor.forecast_count == 4
    assert pools[0].closed and not pools[0].terminated
    assert actor.pending[-1]["expired"] is False
    assert [event["kind"] for event in actor.monitor.pending] == ["notice_updated"]
    assert actor.candidates["nominal"]["notice_sequence"] == 1
    assert notices(actor.case, actor.s.time_s)["sequence"] == 2


def test_unbooked_admissible_nominal_is_protected_from_optional_batch(
    monkeypatch, tmp_path, harness
):
    actor, _ = comparison_actor(monkeypatch, tmp_path, harness, now=2100.0)
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.comparison_generations = {actor.comparison_generation(notices(actor.case, 1000.0))}
    actor.comparison_preparations = []
    actor.selected = None
    actor.calculate = lambda *args, **kwargs: pytest.fail("must preserve available unbooked return")
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    row = actor.comparison_preparations[-1]
    assert row["selected_before"] is None and row["selected_after"] is None
    assert row["budget"]["protected_burn_s"] is None
    assert row["budget"]["admissible_candidate_ids"] == ["nominal"]
    assert row["budget"]["opportunity_cutoff_time_s"] == 2183.0
    assert row["budget"]["available_sim_s"] == 83.0
    assert row["reason"] == "booking_or_candidate_deadline"


def test_full_batch_timeout_and_cleanup_stay_inside_600_seconds(monkeypatch, tmp_path, harness):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    pools = harness.asynchronous_pool(monkeypatch, clock, completion_s=1000.0)
    original = runtime.ProcessPoolExecutor

    class CleanupCost(original):
        def shutdown(self, **kwargs):
            super().shutdown(**kwargs)
            clock.now += 1.0

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", CleanupCost)
    assert not actor.calculate(["next_orbit"], budget_s=600.0, cutoff_time_s=5000.0)
    operation = actor.pending[-1]
    assert operation["cutoff_time_s"] == 2100.0
    assert operation["end_time_s"] - operation["start_time_s"] <= 600.0
    assert operation["wall_time_s"] <= 600.0
    assert operation["end_time_s"] - operation["start_time_s"] >= operation["wall_time_s"]
    assert operation["worker_cleanup_wall_time_s"] == 1.0
    assert operation["cutoff_reached"]
    assert pools[0].terminated and pools[0].closed


def test_initial_forecast_failure_stops_explicitly_without_model_or_automatic_retry(
    monkeypatch, tmp_path, harness
):
    from dataclasses import asdict

    actor, _ = harness.actor_harness(monkeypatch, tmp_path, now=1000.0, case="comparison_normal")
    actor.origin = asdict(actor.s)
    actor.samples, actor.launch, actor.v = [], {}, None
    actor.controller.to_dict = lambda: {"phase": "orbital_coast"}
    monkeypatch.setattr(runtime, "_sample", lambda *args: {})
    actor.finish = lambda: runtime.ReplanningExecutor.finish(actor)
    calls = []

    def failed(names, **kwargs):
        calls.append(list(names))
        actor.forecast_count += 4 * len(names)
        actor.step()
        actor.last_calculation_result = {"status": "failed", "reason": "forecast_worker_exception"}
        return False

    actor.calculate = failed
    study = actor.run_event_supervision()
    assert calls == [["nominal"]]
    assert study["decisions"] == [] and study["dispatch"] is None
    assert study["execution"]["outcome"]["termination"] == "return_unresolved"
    assert study["execution"]["outcome"]["stop_reason"] == "comparison_preparation_unavailable"
    stopped = [e for e in study["execution"]["events"] if e["event"] == "m1_comparison_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["preparation_id"] == study["comparison_preparations"][-1]["preparation_id"]


@pytest.mark.parametrize("cleanup_s", [1.0, 7.0])
def test_expired_batch_reason_is_distinct_from_successful_result_cleanup_overrun(
    monkeypatch, tmp_path, harness, cleanup_s
):
    actor, clock = comparison_actor(monkeypatch, tmp_path, harness, now=1500.0)
    harness.asynchronous_pool(monkeypatch, clock, completion_s=1000.0)
    original = runtime.ProcessPoolExecutor

    class BatchCleanup(original):
        def shutdown(self, **kwargs):
            super().shutdown(**kwargs)
            clock.now += cleanup_s

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", BatchCleanup)
    assert not actor.calculate(["next_orbit"], budget_s=600.0, cutoff_time_s=5000.0)
    assert actor.pending[-1]["cutoff_reached"] is True
    assert actor.last_calculation_result["reason"] == "forecast_batch_budget_exceeded"
    if cleanup_s == 7.0:
        # The failure cause is budget expiry, but measured cleanup/clock
        # overrun is still preserved and cannot become a passing record.
        assert actor.pending[-1]["clock_accounting_overrun"] is True
        assert actor.pending[-1]["wall_time_s"] > 600.0


def test_old_optional_failure_is_not_relabelled_as_new_preparation_stop(
    monkeypatch, tmp_path, harness
):
    actor, _ = harness.actor_harness(monkeypatch, tmp_path, now=1352.0, case="comparison_update")
    actor.candidates["nominal"] = harness.make_candidate(actor, "nominal")
    actor.selected = "nominal"
    material = actor.comparison_generation(notices(actor.case, actor.s.time_s))
    actor.comparison_generations = {material}
    actor.comparison_last_generation = material
    actor.comparison_preparations = [
        {
            "status": "failed",
            "end_time_s": 1352.0,
            "preparation_id": "old-optional-preparation",
        }
    ]
    actor.s = replace(actor.s, time_s=2284.0)
    assert not actor.checks(notices(actor.case, actor.s.time_s))["nominal"]["accepted"]

    class ReassessmentReached(Exception):
        pass

    def choose_current_state(stage, allowed):
        assert "wait_for_update" in allowed
        assert "select_nominal" not in allowed
        raise ReassessmentReached

    actor.choose = choose_current_state
    with pytest.raises(ReassessmentReached):
        actor.run_event_supervision()
    assert not hasattr(actor, "stop_reason")
    assert not any(e["event"] == "m1_comparison_stopped" for e in actor.events)
