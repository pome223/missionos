"""Model-free urban choice screen; safe to copy into the isolated SITL worker.

Policy input is observed position history only. Scenario trajectories and the
offline oracle are evaluation inputs, never dispatchable policy arguments.
"""

from __future__ import annotations

import math


CASES = {"short_clear": 2.0, "long_block": 0.15}
POLICIES = ("always_wait", "always_detour", "constant_velocity")
CLEARANCE_X_M = 13.0
DETOUR_X_M = 85.0


def choose_urban_action(history, policy, *, airspeed_mps=12.0):
    if policy not in POLICIES:
        raise ValueError("Unsupported policy; oracle is evaluation-only")
    if not math.isfinite(airspeed_mps) or airspeed_mps <= 0:
        raise ValueError("Invalid airspeed")
    if len(history) < 3:
        raise ValueError("At least three observations required")
    previous = -math.inf
    for row in history:
        if set(row) != {"observed_at_s", "obstacle_x_m"}:
            raise ValueError("Only observed time and position belong in policy input")
        if not all(type(v) in (int, float) and math.isfinite(v) for v in row.values()):
            raise ValueError("Non-finite observation")
        if row["observed_at_s"] <= previous:
            raise ValueError("Observation clock must increase")
        previous = row["observed_at_s"]
    first, last = history[0], history[-1]
    dt = last["observed_at_s"] - first["observed_at_s"]
    if dt < 1 or any(
        b["observed_at_s"] - a["observed_at_s"] > 1.5 for a, b in zip(history, history[1:])
    ):
        raise ValueError("Insufficient or discontinuous observation history")
    velocity = (last["obstacle_x_m"] - first["obstacle_x_m"]) / dt
    remaining = max(0.0, CLEARANCE_X_M - last["obstacle_x_m"])
    wait_s = remaining / velocity if velocity > 0.01 else (0.0 if not remaining else None)
    # Same approved path for every policy; allowance accounts for four turns.
    extra_detour_s = 2 * DETOUR_X_M / airspeed_mps + 8
    action = "wait" if policy == "always_wait" else "detour"
    if policy == "constant_velocity":
        action = "wait" if wait_s is not None and wait_s <= extra_detour_s else "detour"
    return {
        "action": action,
        "policy": policy,
        "estimated_obstacle_vx_mps": velocity,
        "estimated_remaining_wait_s": wait_s,
        "estimated_extra_detour_s": extra_detour_s,
        "observation_source": "gazebo_pose_history",
        "vla_invoked": False,
        "wam_invoked": False,
    }


def screen_urban_decisions():
    """Cheap analytic headroom screen, explicitly not simulated flight evidence."""
    rows = []
    for case, speed in CASES.items():
        history = [{"observed_at_s": t, "obstacle_x_m": speed * t} for t in (0, 1, 2)]
        direct_s = 230 / 12
        costs = {
            "wait": max(0, CLEARANCE_X_M / speed - 2) + direct_s,
            "detour": direct_s + 2 * DETOUR_X_M / 12 + 8,
        }
        rows.append(
            {
                "case": case,
                "observations": history,
                "analytic_candidate_seconds": costs,
                "offline_oracle_action": min(costs, key=costs.get),
                "policies": {p: choose_urban_action(history, p) for p in POLICIES},
            }
        )
    means = {
        p: sum(r["analytic_candidate_seconds"][r["policies"][p]["action"]] for r in rows)
        / len(rows)
        for p in POLICIES
    }
    oracle = sum(min(r["analytic_candidate_seconds"].values()) for r in rows) / len(rows)
    return {
        "schema_version": "ship_urban_headroom_screen.v1",
        "status": "screened",
        "cases": rows,
        "mean_analytic_seconds": means,
        "offline_oracle_mean_seconds": oracle,
        "adaptive_gain_over_best_fixed_s": min(means[p] for p in POLICIES[:2])
        - means["constant_velocity"],
        "oracle_headroom_over_constant_velocity_s": means["constant_velocity"] - oracle,
        "learned_model_comparison_admitted": False,
        "px4_runtime_invoked": False,
        "gazebo_runtime_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "limitations": [
            "Analytic timing screen with exact synthetic position history, not a flight result.",
            "Two constant-motion cases discriminate wait from detour but give the strong rule no oracle gap.",
            "Gazebo pose access is privileged; camera-matched perception must precede any VLA/WAM comparison.",
        ],
    }
