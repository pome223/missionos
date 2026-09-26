"""Local velocity guard using current observations, never a scenario trajectory.

This is a conservative yield-and-resume controller for the known-geometry demo,
not a perception system or a general collision-avoidance guarantee.
"""

import math


ROBOT_RADIUS_M = 0.45
MARGIN_M = 0.18
LOOKAHEAD_S = 4.0
MAX_OBSERVATION_AGE_S = 0.15
CLEAR_HOLD_S = 0.75
MAX_YIELD_S = 20.0


def closest_approach(relative_xy, relative_velocity, horizon=LOOKAHEAD_S):
    speed2 = sum(v * v for v in relative_velocity)
    t = (
        min(horizon, max(0.0, -sum(p * v for p, v in zip(relative_xy, relative_velocity)) / speed2))
        if speed2 > 1e-10
        else 0.0
    )
    return math.hypot(*(p + t * v for p, v in zip(relative_xy, relative_velocity)))


def velocity_guard(state, command, observation, intent_velocity=None):
    """Revalidate a candidate robot command against a recent measured track."""
    try:
        now, observed = state["sim_time_s"], observation["observed_sim_time_s"]
        xy, obstacle = state["ground_truth_xy"], observation["xy"]
        velocity = observation["velocity_xy_mps"]
        measured_velocity = state["measured_velocity_xy_mps"]
        yaw, radius = state["yaw_rad"], observation["radius_m"]
        values = [
            now,
            observed,
            yaw,
            radius,
            *xy,
            *obstacle,
            *velocity,
            *measured_velocity,
            *command,
        ]
        if intent_velocity is not None:
            values += list(intent_velocity)
        if (
            any(isinstance(v, bool) or not math.isfinite(v) for v in values)
            or any(len(v) != 2 for v in (xy, obstacle, velocity, measured_velocity))
            or len(command) != 3
            or (intent_velocity is not None and len(intent_velocity) != 2)
            or radius <= 0
            or not 0 <= now - observed <= MAX_OBSERVATION_AGE_S
            or observation.get("velocity_observed") is not True
            or not state.get("telemetry_fresh")
        ):
            raise ValueError("unusable track")
    except (KeyError, TypeError, ValueError):
        return dict(
            hold=True, reason="dynamic_observation_unavailable", predicted_separation_m=None
        )
    requested = (
        command[0] * math.cos(yaw) - command[1] * math.sin(yaw),
        command[0] * math.sin(yaw) + command[1] * math.cos(yaw),
    )
    relative = [p - q for p, q in zip(obstacle, xy)]
    # Check requested motion as well as inertia visible in the current state.
    separation = min(
        closest_approach(relative, [v - r for v, r in zip(velocity, robot_velocity)], horizon)
        for robot_velocity, horizon in (
            (requested, LOOKAHEAD_S),
            (intent_velocity if intent_velocity is not None else requested, LOOKAHEAD_S),
            # Gait velocity oscillates within a stride. Extrapolate inertia for
            # a bounded braking horizon, not four seconds of constant speed.
            (measured_velocity, 0.75),
        )
    )
    return dict(
        hold=separation < ROBOT_RADIUS_M + radius + MARGIN_M,
        reason="moving_obstacle_conflict",
        predicted_separation_m=separation,
        current_separation_m=math.hypot(*relative),
    )
