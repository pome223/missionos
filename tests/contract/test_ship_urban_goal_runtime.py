"""Goal-loop admission and stopping contracts with in-process CPU doubles."""

from copy import deepcopy
from dataclasses import replace
import math
import threading

import pytest

from scripts.ship_urban_loop_fixture_worker import goal_candidates, goal_forecast
from src.runtime.ship_urban_loop import LoopRejected, UrbanLoopPlan, UrbanLoopRuntime, digest
from src.runtime.ship_urban_loop_fixture import FixtureAutopilot, FixtureRules


def goal_plan(**changes):
    plan = UrbanLoopPlan(
        "goal-runtime-test", "operator:goal-fixture", "fixture",
        fixture_goal_policy=True, goal_ned_m=(1027.0, 0.0, -30.0),
        updates=3, max_leg_m=5.0, clearance_m=0.25,
        settle_s=0.003, startup_timeout_s=1, inference_timeout_s=1,
        segment_timeout_s=1, total_timeout_s=5,
    )
    return replace(plan, **changes)


class InProcessModels:
    """Bound fixture replies; no worker, socket or native model is created."""

    def __init__(self, fault=None):
        self.fault = fault
        self.requests = []
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1
        return {"execution_scope": "fixture"}

    def infer(self, request):
        self.requests.append(deepcopy(request))
        response = {
            "model": request["model"], "session_id": request["session_id"],
            "request_sha256": digest(request), "dispatch_allowed": False, "fixture": True,
        }
        if request["model"] == "vla":
            response["candidates"] = goal_candidates(request)
            if self.fault == "oversized_leg":
                start = request["observation"]["position_ned_m"]
                response["candidates"] = [
                    {"id": "too-far", "target_ned_m": [start[0] + 6, start[1], start[2]]}
                ]
        else:
            response.update(goal_forecast(request))
            if self.fault == "missing_forecast":
                response.pop("forecasts")
            elif self.fault == "missing_candidate_forecast":
                response["forecasts"].pop()
            elif self.fault == "malformed_forecast":
                response["forecasts"][0]["predicted_clear"] = 1
            elif self.fault == "blocked_forecast":
                for forecast in response["forecasts"]:
                    forecast["predicted_clear"] = False
            elif self.fault == "unbound_forecast":
                response["observation_context_sha256"] = "0" * 64
        return response

    def stop(self):
        self.stops += 1
        return {"fixture_stopped": True}

    def stopped(self):
        return self.stops == 1


class SensorAutopilot(FixtureAutopilot):
    def __init__(self, plan):
        super().__init__(plan)
        self.hidden_field = None
        self.stale_context = False

    def observe(self):
        row = super().observe()
        if self.hidden_field:
            row[self.hidden_field] = {"unobserved": "future-state"}
        if self.stale_context:
            row["goal_context"]["observed_at_s"] -= self.plan.observation_age_s + 1
        return row


class EventFaultRuntime(UrbanLoopRuntime):
    """Faults modify the observed fixture world at an exact control boundary."""

    def __init__(self, *args, trigger=None, hidden_after_arrival=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.trigger = trigger
        self.triggered = False
        self.hidden_after_arrival = hidden_after_arrival

    def record(self, event, **fields):
        item = super().record(event, **fields)
        matches = event == self.trigger
        if self.trigger == "after_wam":
            matches = event == "model_received" and fields["response"]["model"] == "wam"
        if matches and not self.triggered:
            self.triggered = True
            # A distant observed box still invalidates the frozen context;
            # stopping is not contingent on a collision along this particular leg.
            self.ap.obstacles = [{
                "id": "new-observation", "center_ned_m": [1050, 10, -30],
                "half_size_m": [0.2, 0.2, 0.5],
            }]
        if event == "segment_arrived" and self.hidden_after_arrival:
            self.ap.hidden_field = self.hidden_after_arrival
        return item


def run_case(*, plan=None, forecast_fault=None, trigger=None, hidden=None,
             hidden_after_arrival=None, stale_context=False):
    plan = plan or goal_plan()
    ap = SensorAutopilot(plan)
    ap.hidden_field, ap.stale_context = hidden, stale_context
    models = InProcessModels(forecast_fault)
    runtime = EventFaultRuntime(
        plan, ap, models, FixtureRules(plan=plan), poll_s=0.001,
        trigger=trigger, hidden_after_arrival=hidden_after_arrival,
    )
    return runtime.run(), runtime, ap, models


def assert_stopped(result, runtime, ap, models):
    assert result["status"] == "blocked"
    assert result["goal_reached"] is False
    assert result["model_shutdown_verified"] is True
    assert models.stops == 1
    assert runtime.active is False and runtime.pending is None
    assert ap.target is None and ap.mode == "hold"
    assert ap.returned is False
    assert not any(event["event"] == "ap_return_handoff" for event in result["events"])


@pytest.mark.parametrize("changes", [
    {"execution_scope": "sim"},
    {"updates": 4},
    {"max_leg_m": 5.001},
    {"goal_ned_m": None},
    {"fixture_goal_policy": False},
    {"fixture_goal_policy": 1},
    {"goal_ned_m": (990, 0, -30)},
    {"goal_ned_m": (1015, 0, -30)},
    {"clearance_m": 0.249},
])
def test_goal_plan_rejects_unapproved_scope_or_unbounded_policy(changes):
    with pytest.raises(ValueError):
        goal_plan(**changes)


def test_goal_completion_requires_stable_arrival_and_stops_before_unused_budget():
    result, runtime, ap, models = run_case(plan=goal_plan(goal_ned_m=(1020, 0, -30)))
    assert result["status"] == "completed" and result["goal_reached"] is True
    assert result["completed_updates"] == 1
    assert len(ap.dispatched) == 1 and len(models.requests) == 2
    assert [request["model"] for request in models.requests] == ["vla", "wam"]
    assert models.starts == models.stops == 1 and ap.returned is True
    events = result["events"]
    dispatched = next(i for i, e in enumerate(events) if e["event"] == "segment_dispatched")
    arrival = next(e for e in events if e["event"] == "segment_arrived")["observation"]
    first_held = next(
        e["observation"] for e in events[dispatched + 1:]
        if e["event"] == "observation"
        and e["observation"]["ap_mode"] == "hold"
        and math.dist(e["observation"]["position_ned_m"], runtime.plan.goal_ned_m)
        <= runtime.plan.target_error_m
    )
    assert arrival["observed_at_s"] - first_held["observed_at_s"] >= runtime.plan.settle_s
    order = [e["event"] for e in events]
    assert order.index("segment_arrived") < order.index("goal_arrival_observed")
    assert order.index("goal_arrival_observed") < order.index("session_revoked")
    assert order.index("models_stopped") < order.index("ap_return_handoff")
    with pytest.raises(LoopRejected, match="single_use"):
        runtime.run()
    assert len(models.requests) == 2 and len(ap.dispatched) == 1


def test_exhausting_three_updates_is_blocked_not_goal_completion_or_a_fourth_action():
    result, runtime, ap, models = run_case(plan=goal_plan(goal_ned_m=(1039, 0, -30)))
    assert_stopped(result, runtime, ap, models)
    assert result["failure"].endswith("urban_goal_not_observed_within_update_budget")
    assert result["completed_updates"] == 3
    assert len(ap.dispatched) == 3 and len(models.requests) == 6
    assert [r["cycle"] for r in models.requests] == [1, 1, 2, 2, 3, 3]
    assert max(math.dist(p["start_ned_m"], p["target_ned_m"]) for p in ap.dispatched) <= 5
    assert not any(e["event"] == "goal_arrival_observed" for e in result["events"])


@pytest.mark.parametrize(("trigger", "dispatch_count", "failure"), [
    ("after_wam", 0, "goal_unbound_fixture_forecast"),
    ("segment_authorized", 0, "urban_goal_context_changed_after_forecast"),
    ("segment_dispatched", 1, "urban_goal_context_changed_after_forecast"),
])
def test_changed_geometry_revokes_the_forecast_at_each_dispatch_boundary(
    trigger, dispatch_count, failure
):
    result, runtime, ap, models = run_case(trigger=trigger)
    assert_stopped(result, runtime, ap, models)
    assert runtime.triggered and result["failure"].endswith(failure)
    assert len(ap.dispatched) == dispatch_count and len(models.requests) == 2
    assert result["completed_updates"] == 0
    if dispatch_count:
        assert ap.point != list(runtime.plan.entry_ned_m)
        assert math.dist(ap.point, ap.dispatched[0]["target_ned_m"]) > 0


@pytest.mark.parametrize("fault", [
    "missing_forecast", "missing_candidate_forecast", "malformed_forecast",
    "blocked_forecast", "unbound_forecast", "oversized_leg",
])
def test_invalid_forecast_or_candidate_stops_without_retry_or_dispatch(fault):
    result, runtime, ap, models = run_case(forecast_fault=fault)
    assert_stopped(result, runtime, ap, models)
    assert len(models.requests) == 2 and ap.dispatched == []
    assert result["completed_updates"] == 0


def test_stale_observed_geometry_blocks_before_model_start():
    result, runtime, ap, models = run_case(stale_context=True)
    assert_stopped(result, runtime, ap, models)
    assert "goal_stale_or_future_observation" in result["failure"]
    assert models.starts == 0 and models.requests == [] and ap.dispatched == []


@pytest.mark.parametrize("field", ["future_obstacles", "scenario", "expected_choice"])
def test_hidden_future_fields_never_reach_a_model_request(field):
    result, runtime, ap, models = run_case(hidden=field)
    assert_stopped(result, runtime, ap, models)
    assert "goal_invalid_fields:observation" in result["failure"]
    assert models.starts == 0 and models.requests == [] and ap.dispatched == []


def test_hidden_future_added_after_arrival_prevents_the_next_inference():
    result, runtime, ap, models = run_case(hidden_after_arrival="future_obstacles")
    assert_stopped(result, runtime, ap, models)
    assert result["completed_updates"] == 1
    assert len(models.requests) == 2 and len(ap.dispatched) == 1
    assert all("future_obstacles" not in request["observation"] for request in models.requests)


def test_transient_geometry_during_pending_wam_stops_even_when_restored_before_reply():
    pending = threading.Event()
    restored = threading.Event()
    release_reply = threading.Event()
    finished = threading.Event()

    class PendingWamModels(InProcessModels):
        wam_thread = None
        replied_after_restoration = False

        def infer(self, request):
            response = super().infer(request)
            if request["model"] == "wam":
                self.wam_thread = threading.current_thread()
                pending.set()
                try:
                    # Cancellation releases this barrier. Its timeout only
                    # prevents a broken test from leaving a worker behind.
                    if not release_reply.wait(timeout=2):
                        raise AssertionError("test WAM was not cancelled")
                    self.replied_after_restoration = restored.is_set()
                finally:
                    finished.set()
            return response

        def stop(self):
            release_reply.set()
            if self.wam_thread is not None:
                self.wam_thread.join(timeout=2)
            return super().stop()

        def stopped(self):
            return super().stopped() and (
                self.wam_thread is None or not self.wam_thread.is_alive()
            )

    class TransientGeometryAutopilot(SensorAutopilot):
        def observe(self):
            if pending.is_set() and not restored.is_set():
                previous = self.obstacles
                self.obstacles = [{
                    "id": "transient-box", "center_ned_m": [1050, 10, -30],
                    "half_size_m": [0.2, 0.2, 0.5],
                }]
                row = super().observe()
                # The returned observation records the change even though the
                # world returns to its prior geometry before any WAM reply.
                self.obstacles = previous
                restored.set()
                return row
            return super().observe()

    plan = goal_plan()
    ap = TransientGeometryAutopilot(plan)
    models = PendingWamModels()
    runtime = UrbanLoopRuntime(plan, ap, models, FixtureRules(plan=plan), poll_s=0.001)
    try:
        result = runtime.run()
    finally:
        release_reply.set()
        if models.wam_thread is not None:
            models.wam_thread.join(timeout=2)

    assert_stopped(result, runtime, ap, models)
    assert result["failure"].endswith("urban_goal_context_changed_during_inference")
    assert pending.is_set() and restored.is_set() and finished.is_set()
    assert models.replied_after_restoration and not models.wam_thread.is_alive()
    assert len(models.requests) == 2 and ap.dispatched == [] and ap.obstacles == []
    assert result["completed_updates"] == 0
    assert [
        event["response"]["model"] for event in result["events"]
        if event["event"] == "model_received"
    ] == ["vla"]
