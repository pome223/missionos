from copy import deepcopy

import pytest

from src.runtime.go2_delivery_mission import Go2DeliveryMission, Go2DeliveryPlan, now
from src.runtime.go2_supervision import digest, guard_decision
from tests.contract.test_go2_delivery_mission import Navigation


class Office(Navigation):
    def __init__(self):
        super().__init__([("aborted", [-1.5, 0.0])])
        self.state.update(map_revision=1, heartbeat_alive=True, operator_cancel_requested=False)
        self.open = False
        self.waited = 0

    def supervision_state(self, plan, leg):
        return dict(
            self.state,
            delivery_route_available=self.open,
            home_route_available=True,
            closure_notice={"source": "fixture", "expected_clear_in_sim_s": 10},
        )

    def wait(self, seconds):
        self.waited += seconds
        if self.waited >= 10:
            self.open = True
            self.state["map_revision"] = 2


def response(request, action, wait=0, **extra):
    return {
        "request_sha256": digest(request),
        "judge_status": "valid",
        "proposal": {
            "observation_id": request["observation_id"],
            "action": action,
            "wait_seconds": wait,
            "rationale": "fixture judgment",
            **extra,
        },
    }


def mission(judge):
    plan = Go2DeliveryPlan(mission_id="test-supervised", supervision_mode="agent")
    return Go2DeliveryMission(
        plan,
        Office(),
        operator_approval_ref="fixture-operator",
        approved_plan_sha256=plan.digest,
        emit=lambda _: None,
        supervisor=judge,
    )


def test_wait_reobserves_before_judge_can_select_a_new_route():
    seen = []

    def judge(request, client):
        seen.append(deepcopy(request))
        return response(request, "wait", 10) if len(seen) == 1 else response(request, "reroute")

    m = mission(judge)
    assert m.start()
    assert not seen[0]["state"]["delivery_route_available"]
    assert seen[1]["state"]["delivery_route_available"]
    assert seen[1]["remaining_wait_sim_s"] == 10
    assert m.status == "awaiting_receipt" and len(m.client.calls) == 2
    assert m.confirm_receipt(
        dict(
            mission_id=m.plan.mission_id,
            parcel_id=m.plan.parcel_id,
            destination=m.plan.destination,
            received=True,
            source="simulation_recipient",
            issued_at=now(),
        )
    )
    assert m.status == "completed"
    assert m.summary()["llm_judgment_invoked"] is False  # Fixture is not a hosted invocation.


def test_return_home_does_not_claim_delivery_or_fabricate_a_receipt():
    m = mission(lambda request, _: response(request, "return_home"))
    assert not m.start()
    assert m.status == "returned_undelivered"
    assert m.receipt is None and not m.summary()["completion_claimed"]
    assert len(m.client.calls) == 2
    assert m.client.calls[-1].label == "returning"
    assert m.events[-1]["event"] == "undelivered_return_verified"


@pytest.mark.parametrize(
    "action,wait,extra",
    [
        ("new_destination", 0, {}),
        ("wait", 21, {}),
        ("wait", float("nan"), {}),
        ("return_home", 0, {"approved": True}),
        ("reroute", 0, {}),
    ],
)
def test_invalid_or_infeasible_judgment_never_retries(action, wait, extra):
    m = mission(lambda request, _: response(request, action, wait, **extra))
    assert not m.start()
    assert m.status == "needs_attention" and len(m.client.calls) == 1
    assert m.decisions[0]["rule_blocking_reasons"]


def test_judge_failure_does_not_fall_back_to_fixed_rule_dispatch():
    m = mission(
        lambda request, _: {"request_sha256": digest(request), "judge_status": "unavailable"}
    )
    assert not m.start() and len(m.client.calls) == 1


def test_cancellation_or_map_change_while_judging_blocks_execution():
    for key, value in (("operator_cancel_requested", True), ("map_revision", 8)):

        def judge(request, client):
            client.state[key] = value
            return response(request, "return_home")

        m = mission(judge)
        assert not m.start() and len(m.client.calls) == 1


def test_judge_call_and_wait_budgets_are_not_reset_by_reobservation():
    m = mission(lambda request, _: response(request, "wait", 1))
    assert not m.start()
    assert len(m.decisions) == 3 and m.waits_used == 3
    assert len(m.client.calls) == 1
    m = mission(lambda request, _: response(request, "wait", 11))
    assert not m.start()
    assert m.waits_used == 11 and len(m.decisions) == 2


def test_binding_cannot_be_replayed_at_the_next_observation():
    m = mission(lambda request, _: response(request, "request_operator"))
    m.start()
    request = m.decisions[0]["observation"]
    old = response(request, "return_home")
    revised = dict(request, observation_id="decision_2")
    reasons = guard_decision(revised, old, request["state"], waits_used=0)
    assert "observation_binding_mismatch" in reasons
    assert "observation_id_mismatch" in reasons
