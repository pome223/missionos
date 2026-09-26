"""Fixtures for the local guard; walking and contacts require the opt-in smoke."""

import copy
from types import SimpleNamespace

import pytest

from simulators.go2_delivery.dynamic_obstacles import closest_approach, velocity_guard
from simulators.go2_delivery.mujoco_backend import MuJoCoDeliveryClient


def state():
    return dict(
        sim_time_s=10.0,
        ground_truth_xy=[0.0, 0.0],
        yaw_rad=0.0,
        measured_velocity_xy_mps=[0.0, 0.0],
        telemetry_fresh=True,
    )


def track():
    return dict(
        xy=[1.0, 1.0],
        velocity_xy_mps=[0.0, -0.25],
        radius_m=0.24,
        observed_sim_time_s=10.0,
        velocity_observed=True,
    )


def test_crossing_track_requires_yield_before_contact():
    decision = velocity_guard(state(), [0.25, 0, 0], track())
    assert decision["hold"] and decision["current_separation_m"] > 1.4
    assert decision["predicted_separation_m"] == pytest.approx(0)


def test_receding_track_allows_resume():
    observation = dict(track(), xy=[1.0, -1.2])
    assert not velocity_guard(state(), [0.25, 0, 0], observation)["hold"]


def test_stopping_does_not_make_the_intended_crossing_safe():
    assert not velocity_guard(state(), [0, 0, 0], track())["hold"]
    assert velocity_guard(state(), [0, 0, 0], track(), [0.25, 0])["hold"]


def test_actual_inertia_can_block_a_zero_velocity_request():
    current = dict(state(), measured_velocity_xy_mps=[0.4, 0])
    observation = dict(track(), xy=[1.0, 0], velocity_xy_mps=[0, 0])
    assert velocity_guard(current, [0, 0, 0], observation)["hold"]


@pytest.mark.parametrize(
    "change",
    [
        {"observed_sim_time_s": 9.8},
        {"observed_sim_time_s": 10.1},
        {"velocity_observed": False},
        {"velocity_xy_mps": [float("nan"), 0]},
        {"xy": [float("inf"), 0]},
        {"radius_m": -0.1},
        {"xy": [1]},
    ],
)
def test_stale_or_malformed_track_fails_closed(change):
    decision = velocity_guard(state(), [0.25, 0, 0], dict(track(), **change))
    assert decision["hold"] and decision["reason"] == "dynamic_observation_unavailable"


def test_stale_robot_state_also_fails_closed():
    assert velocity_guard(dict(state(), telemetry_fresh=False), [0.25, 0, 0], track())["hold"]


def test_closest_approach_is_bounded_and_handles_stationary_tracks():
    assert closest_approach([2, 0], [0, 0]) == 2
    assert closest_approach([2, 0], [1, 0]) == 2
    assert closest_approach([2, 0], [-0.1, 0]) == pytest.approx(1.6)


class PhysicsFixture:
    def __init__(self):
        self.data = SimpleNamespace(time=0.0)
        self.commands = []

    def enable_moving_obstacle(self):
        pass

    def moving_position(self):
        return [-1.25, 1.25 - self.data.time * 0.2]

    def step(self, command):
        self.commands.append(copy.deepcopy(command))
        self.data.time += 0.005

    def state(self):
        return dict(
            state(),
            sim_time_s=self.data.time,
            ground_truth_xy=[-2.5, 0],
            measured_speed_mps=0.0,
            base_stable=True,
            geofence_satisfied=True,
            obstacle_contacts=[],
            heartbeat_alive=True,
        )


def test_observed_velocity_comes_from_past_positions_without_static_map_mutation():
    physics = PhysicsFixture()
    client = MuJoCoDeliveryClient(
        physics, emit=lambda _: None, observe=lambda *_: None, scenario="moving_obstacle"
    )
    client.wait(0.2)
    observation = client.read_state()["moving_obstacle"]
    assert observation["velocity_xy_mps"] == pytest.approx([0, -0.2])
    assert observation["velocity_observed"]
    assert client.map_revision == 0
    assert client.dynamic_summary()["obstacle_travel_m"] > 0


def test_operator_cancel_during_yield_never_resumes_planar_motion():
    physics, events = PhysicsFixture(), []
    client = MuJoCoDeliveryClient(
        physics, emit=events.append, observe=lambda *_: None, scenario="moving_obstacle"
    )
    # The first track has no velocity sample yet, so navigation must hold.
    client.cancel_requested = lambda: physics.data.time >= 0.1
    goal = SimpleNamespace(
        label="outbound", x_m=2.5, y_m=0, yaw_rad=0, tolerance_m=0.25, max_speed_mps=0.25
    )
    client.send_goal_pose(goal)
    assert client.status == "canceled"
    assert any(e["event"] == "dynamic_yield_requested" for e in events)
    assert not any(e["event"] == "dynamic_path_clear_observed" for e in events)
    assert all(command[:2] == [0.0, 0.0] for command in physics.commands)
