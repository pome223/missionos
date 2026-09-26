from datetime import datetime, timedelta, timezone
import pytest

from src.runtime.go2_delivery_mission import Go2DeliveryMission, Go2DeliveryPlan, now


class Navigation:
    metadata = {"simulator": "fixture", "physics_invoked": False}

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self.state = dict(
            telemetry_fresh=True,
            heartbeat_alive=True,
            base_stable=True,
            geofence_satisfied=True,
            ground_truth_xy=[-2.5, 0.0],
            state_observed=True,
            robot_motion_observed=False,
            navigation_status="idle",
        )

    def send_goal_pose(self, goal):
        self.calls.append(goal)
        status, xy = self.outcomes.pop(0) if self.outcomes else ("succeeded", [goal.x_m, goal.y_m])
        self.state.update(navigation_status=status, ground_truth_xy=xy, robot_motion_observed=True)
        return dict(ack_status="accepted", ack_source="fixture_goal_executor")

    def read_state(self):
        return dict(self.state)

    def read_progress(self):
        return self.read_state()

    def wait(self, seconds):
        pass


def mission(client=None, **kwargs):
    plan = Go2DeliveryPlan(mission_id="test-delivery", **kwargs)
    return Go2DeliveryMission(
        plan,
        client or Navigation(),
        operator_approval_ref="test-operator",
        approved_plan_sha256=plan.digest,
        emit=lambda _: None,
    )


def receipt(m):
    return dict(
        mission_id=m.plan.mission_id,
        parcel_id=m.plan.parcel_id,
        destination=m.plan.destination,
        received=True,
        source="simulation_recipient",
        issued_at=now(),
    )


def test_mission_approval_covers_two_navigation_goals_and_receipt_is_separate():
    m = mission()
    assert m.start()
    assert len(m.client.calls) == 1
    assert m.status == "awaiting_receipt"
    assert m.summary()["completion_claimed"] is False
    assert m.confirm_receipt(receipt(m))
    assert len(m.client.calls) == 2
    assert m.summary()["completion_scope"] == "simulated_delivery_and_return"
    assert m.summary()["physical_execution_invoked"] is False
    assert m.summary()["llm_judgment_invoked"] is False


def test_goal_success_without_actual_arrival_does_not_complete_or_return():
    m = mission(Navigation([("succeeded", [0.0, 0.0])]))
    assert not m.start()
    assert m.status == "needs_attention"
    assert len(m.client.calls) == 1
    with pytest.raises(ValueError):
        m.confirm_receipt(receipt(m))


def test_robot_must_start_at_pickup():
    client = Navigation()
    client.state["ground_truth_xy"] = [1.0, 0.0]
    m = mission(client)
    assert not m.start()
    assert not client.calls


def test_stale_telemetry_prevents_dispatch():
    client = Navigation()
    client.state["telemetry_fresh"] = False
    assert not mission(client).start()
    assert not client.calls


def test_receipt_cannot_be_replayed_or_applied_to_another_parcel():
    m = mission()
    assert m.start()
    bad = receipt(m)
    bad["parcel_id"] = "another-parcel"
    with pytest.raises(ValueError):
        m.confirm_receipt(bad)
    bad = receipt(m)
    bad["issued_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError):
        m.confirm_receipt(bad)
    assert len(m.client.calls) == 1


def test_receipt_requires_robot_still_at_destination():
    m = mission()
    m.start()
    m.client.state["ground_truth_xy"] = [0.0, 0.0]
    with pytest.raises(ValueError):
        m.confirm_receipt(receipt(m))
    assert len(m.client.calls) == 1


def test_one_abort_retry_is_bounded_by_mission_approval():
    m = mission(Navigation([("aborted", [-2.0, 0.0]), ("aborted", [-1.0, 0.0])]))
    assert not m.start()
    assert len(m.client.calls) == 2
    assert sum(e["event"] == "bounded_retry_selected" for e in m.events) == 1
    assert m.summary()["completion_claimed"] is False


def test_cancel_does_not_automatically_restart_movement():
    m = mission(Navigation([("canceled", [-2.0, 0.0])]))
    assert not m.start()
    assert len(m.client.calls) == 1


def test_approval_is_bound_to_exact_plan():
    plan = Go2DeliveryPlan(mission_id="test-delivery")
    with pytest.raises(ValueError):
        Go2DeliveryMission(
            plan,
            Navigation(),
            operator_approval_ref="test-operator",
            approved_plan_sha256="0" * 64,
            emit=lambda _: None,
        )


@pytest.mark.parametrize("xy", [(float("nan"), 0.0), (5.0, 0.0)])
def test_unapproved_destination_is_rejected(xy):
    with pytest.raises(ValueError):
        mission(destination_xy=xy)


def test_return_is_not_complete_until_stability_hold_passes():
    class FallsDuringHold(Navigation):
        def wait(self, seconds):
            if len(self.calls) == 2:
                self.state["base_stable"] = False

    m = mission(FallsDuringHold())
    assert m.start()
    assert not m.confirm_receipt(receipt(m))
    assert m.status == "needs_attention"
    assert not m.summary()["completion_claimed"]
    assert "mission_completed" not in [e["event"] for e in m.events]


def test_completion_event_follows_hold_verification():
    m = mission()
    m.start()
    m.confirm_receipt(receipt(m))
    events = [e["event"] for e in m.events]
    assert events.index("terminal_hold_verified") < events.index("mission_completed")
    with pytest.raises(ValueError):
        m.confirm_receipt(receipt(m))


def test_runtime_safety_violation_prevents_a_new_dispatch():
    client = Navigation()
    client.state["safety_violation_observed"] = True
    m = mission(client)
    assert not m.start()
    assert not client.calls


def test_arbitrary_waypoint_cannot_reuse_mission_approval():
    m = mission()
    with pytest.raises(ValueError):
        m._navigate("outbound", (1.0, 1.0), 0.0)
    assert not m.client.calls
