"""Synthetic disturbances at arrival; real Go2 gait requires the opt-in smoke."""

from types import SimpleNamespace

import pytest

from simulators.go2_delivery.mujoco_backend import MuJoCoDeliveryClient
from src.runtime.go2_delivery_navigation import DeliveryGoal


class SettlingFixture:
    def __init__(
        self,
        *,
        drift=False,
        contact=False,
        initial_x=-1.6,
        fault_at=1.0,
        contact_until=float("inf"),
    ):
        self.data = SimpleNamespace(time=0.0)
        self.xy = [initial_x, 0.0]
        self.commands = []
        self.drift = drift
        self.contact = contact
        self.fault_at = fault_at
        self.contact_until = contact_until
        self.disturbed = False

    def step(self, command):
        self.commands.append((self.data.time, list(command)))
        self.data.time += 0.005
        self.xy[0] += command[0] * 0.005
        if self.drift and not self.disturbed and not command[0]:
            self.xy[0] -= 0.25
            self.disturbed = True

    def state(self):
        return dict(
            sim_time_s=self.data.time,
            ground_truth_xy=list(self.xy),
            yaw_rad=0.0,
            measured_velocity_xy_mps=[0.0, 0.0],
            measured_speed_mps=0.0,
            base_stable=True,
            geofence_satisfied=True,
            obstacle_contacts=[["office_fixture", "leg_fixture"]]
            if self.contact and self.fault_at <= self.data.time < self.contact_until
            else [],
            telemetry_fresh=True,
            heartbeat_alive=True,
        )


def run(
    *,
    drift=False,
    contact=False,
    cancel=False,
    timeout=10.0,
    initial_x=-1.6,
    fault_at=1.0,
    contact_until=float("inf"),
):
    physics = SettlingFixture(
        drift=drift,
        contact=contact,
        initial_x=initial_x,
        fault_at=fault_at,
        contact_until=contact_until,
    )
    events = []
    client = MuJoCoDeliveryClient(
        physics, emit=events.append, observe=lambda *_: None, timeout=timeout
    )
    if cancel:
        client.cancel_requested = lambda: physics.data.time >= fault_at
    goal = DeliveryGoal(
        x_m=-1.5,
        y_m=0.0,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        label="outbound",
    )
    client.send_goal_pose(goal)
    return physics, client, events


def test_settling_drift_is_reobserved_and_corrected_before_arrival():
    physics, client, events = run(drift=True)
    assert physics.disturbed
    assert any(t >= 2.0 and cmd[0] > 0 for t, cmd in physics.commands)
    assert client.status == "succeeded"
    assert abs(physics.xy[0] + 1.5) <= 0.25
    assert len([e for e in events if e["event"] == "goal_accepted"]) == 1


def test_settling_correction_does_not_reset_navigation_budget():
    physics, client, _ = run(drift=True, timeout=2.1)
    assert client.status == "aborted"
    assert physics.data.time < 4.12  # Original deadline plus the abort stop hold.


@pytest.mark.parametrize("condition", ["contact", "cancel"])
@pytest.mark.parametrize("drift", [False, True])
def test_stop_hold_safety_or_cancel_prevents_success_and_further_motion(condition, drift):
    physics, client, events = run(drift=drift, **{condition: True})
    if drift:
        assert physics.disturbed
        assert abs(physics.xy[0] + 1.5) > 0.25  # Correction would be needed without the stop.
    assert client.status == "canceled"
    assert all(cmd[:2] == [0.0, 0.0] for _, cmd in physics.commands)
    assert any(e["event"] == "navigation_canceled" for e in events)


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("contact", "operating_state_violation"),
        ("cancel", "operator_cancel"),
    ],
)
def test_stop_hold_crossing_deadline_preserves_cancel_cause(condition, reason):
    # Start outside the inner target: the hold begins after t=0 and ends after
    # the 2.05 s navigation budget. The contact is transient, ending before the
    # post-hold observation, so its latched safety evidence must be retained.
    physics, client, events = run(
        drift=True,
        initial_x=-1.70,
        timeout=2.05,
        fault_at=2.04,
        contact_until=2.10,
        **{condition: True},
    )
    assert any(cmd[0] > 0 for t, cmd in physics.commands if t < 2.04)
    assert physics.disturbed and abs(physics.xy[0] + 1.5) > 0.25
    assert physics.data.time > 2.05
    assert all(cmd[:2] == [0.0, 0.0] for t, cmd in physics.commands if t >= 2.04)
    assert client.status == "canceled"
    cancellations = [e for e in events if e["event"] == "navigation_canceled"]
    assert [e["reason"] for e in cancellations] == [reason]
    assert not any(e.get("reason") == "navigation_timeout" for e in events)
    if condition == "contact":
        assert cancellations[0]["state"]["obstacle_contacts"] == []
        assert cancellations[0]["state"]["safety_violation_observed"] is True
