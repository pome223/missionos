"""Transparent kinematic comparator, explicitly neither a learned WAM nor VLA."""

import math


CANDIDATES = {
    "direct": {"wait_s": 0.0, "waypoints": [[1.0, 0.0]]},
    "wait": {"wait_s": 4.0, "waypoints": [[1.0, 0.0]]},
    "detour": {"wait_s": 0.0, "waypoints": [[-0.55, -0.8], [0.65, -0.8], [1.0, 0.0]]},
}


def predict_choice(history, robot_xy, *, horizon_s=20.0):
    """Accept only observed {t_s, x_m, y_m}; scenario/future truth is forbidden.

    Candidate trajectories assume instant turns and 0.2 m/s. They are ranking
    approximations, not Nav2 trajectory forecasts or safety guarantees.
    """
    if len(history) < 2 or any(set(row) != {"t_s", "x_m", "y_m"} for row in history):
        raise ValueError("observation history must contain only timestamp and observed xy")
    for i, row in enumerate(history):
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError("nonfinite observation")
        if i and row["t_s"] <= history[i - 1]["t_s"]:
            raise ValueError("observation times must increase")
    first, last = history[0], history[-1]
    elapsed = last["t_s"] - first["t_s"]
    if elapsed < 1.0:
        raise ValueError("insufficient motion history")
    # The actor/observer streams update at different rates. Endpoint differencing
    # biases speed when the final pose has not advanced to the last sample time.
    # Fit all observed samples; retain the latest measured position as the anchor.
    vx, vy = estimate_velocity(history)
    ranking = []
    for name, candidate in CANDIDATES.items():
        points = [robot_xy, *candidate["waypoints"]]
        duration = candidate["wait_s"] + sum(
            math.dist(a, b) / 0.2 for a, b in zip(points, points[1:])
        )
        if duration > horizon_s:
            raise ValueError("candidate event outside prediction horizon")
        minimum = float("inf")
        for step in range(math.ceil(duration / 0.1) + 1):
            t = min(step * 0.1, duration)
            remaining = max(0.0, t - candidate["wait_s"])
            position = list(robot_xy)
            for a, b in zip(points, points[1:]):
                segment = math.dist(a, b) / 0.2
                if remaining <= segment:
                    fraction = remaining / segment if segment else 1.0
                    position = [a[j] + fraction * (b[j] - a[j]) for j in (0, 1)]
                    break
                remaining -= segment
                position = b
            obstacle = [last["x_m"] + vx * t, last["y_m"] + vy * t]
            minimum = min(minimum, math.dist(position, obstacle))
        ranking.append(
            {
                "candidate": name,
                "estimated_duration_s": duration,
                "predicted_min_center_distance_m": minimum,
                "clearance_screen_passed": minimum >= 0.60,
            }
        )
    passing = [row for row in ranking if row["clearance_screen_passed"]]
    choice = (
        min(passing, key=lambda row: row["estimated_duration_s"])["candidate"] if passing else None
    )
    return {
        "candidate": choice,
        "velocity_estimate_mps": [vx, vy],
        "velocity_estimator": "least_squares_observed_history",
        "ranking": ranking,
        "source": "constant_velocity_privileged_pose_baseline",
        "learned_wam_invoked": False,
        "vla_invoked": False,
    }


def estimate_velocity(history):
    """Return velocity from observation-only history after strict validation."""
    if len(history) < 2 or any(set(row) != {"t_s", "x_m", "y_m"} for row in history):
        raise ValueError("observation history must contain only timestamp and observed xy")
    for i, row in enumerate(history):
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError("nonfinite observation")
        if i and row["t_s"] <= history[i - 1]["t_s"]:
            raise ValueError("observation times must increase")
    if history[-1]["t_s"] - history[0]["t_s"] < 1.0:
        raise ValueError("insufficient motion history")
    mean_t = sum(row["t_s"] for row in history) / len(history)
    variance_t = sum((row["t_s"] - mean_t) ** 2 for row in history)
    return [
        sum((row["t_s"] - mean_t) * row[axis] for row in history) / variance_t
        for axis in ("x_m", "y_m")
    ]
