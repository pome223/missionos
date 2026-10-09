"""Separately approved, model-free return after a rejected inland candidate.

This is a new reconstruction. It does not recover the lost October 5 code.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

LIMITS = {
    "schema": "yokohama.candidate-recovery.v1",
    "recovery_timeout_s": 220,
    "return_tolerance_m": 0.25,
    "return_altitude_tolerance_m": 0.15,
    "landing_radius_m": 1.5,
    "landing_height_max_m": 0.5,
    "stable_sim_s": 2,
    "minimum_distinct_samples": 3,
    "maximum_speed_mps": 0.3,
    "maximum_sample_gap_sim_s": 2,
    "maximum_sample_gap_wall_s": 3,
}


def policy(config):
    value = config.get("candidate_recovery")
    if value is None:
        return None
    from_scripts = config["decisions"]["endpoint_feedback"]
    if (
        value != LIMITS
        or config["decisions"]["backend"] != "fixture"
        or config.get("fixture_reject_second_candidate") is not True
        or from_scripts["entry_world_xyz_m"][:2] != [0.0, 0.0]
        or from_scripts["exit_world_xyz_m"] != from_scripts["entry_world_xyz_m"]
        or not config.get("operator_approval_manifest_sha256")
    ):
        raise ValueError("Recovery requires separately approved fixed CPU authority")
    return value


def map_clearance(config, asset):
    """Qualify the complete tracking corridor and vertical launch return volume."""
    import numpy as np
    from shapely.geometry import LineString, Point, shape
    from src.runtime.yokohama_scene import to_source

    p = config["decisions"]["endpoint_feedback"]
    raw = Path(asset).read_bytes()
    if hashlib.sha256(raw).hexdigest() != p["map_sha256"]:
        raise ValueError("Recovery map changed")
    a, b, ground = to_source(
        np.array(
            [
                p["entry_world_xyz_m"],
                p["goal_world_xyz_m"],
                [0, 0, 0],
            ]
        ),
        config["world"]["frame"],
    )
    line = LineString([a[:2], b[:2]])
    radius = p["corridor_half_width_m"] + p["tracking_tube_m"]
    scale = float(
        np.linalg.norm(
            np.linalg.inv(np.array(config["world"]["frame"]["source_to_world_matrix"])), ord=2
        )
    )
    required = (radius + p["clearance_m"]) * scale
    relevant = [
        shape(f["geometry"])
        for f in json.loads(raw)["features"]
        if f["properties"]["zmax"] >= ground[2] - 2 and f["properties"]["zmin"] <= a[2] + 2
    ]
    if not relevant or min(line.distance(s) for s in relevant) <= required:
        raise ValueError("Recovery corridor/landing volume lacks mapped clearance")
    if min(Point(ground[:2]).distance(s) for s in relevant) <= required:
        raise ValueError("Recovery launch column lacks mapped clearance")
    return {
        "map_sha256": p["map_sha256"],
        "required_centerline_clearance_m": required,
        "minimum_clearance_m": min(line.distance(s) for s in relevant),
    }


def deadline_for(begin, overall_deadline):
    if (
        not all(type(v) in (int, float) and math.isfinite(v) for v in (begin, overall_deadline))
        or begin < 0
        or overall_deadline < begin + LIMITS["recovery_timeout_s"]
    ):
        raise ValueError("Insufficient fixed recovery reserve")
    return begin + LIMITS["recovery_timeout_s"]


def validate_sample(config, row, anchor, *, landing=False):
    p = config["decisions"]["endpoint_feedback"]
    a, b = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
    xyz = row["vehicle"]["xyz"]
    delta = [b[i] - a[i] for i in range(2)]
    fraction = max(
        0, min(1, sum((xyz[i] - a[i]) * delta[i] for i in range(2)) / sum(d * d for d in delta))
    )
    lateral = math.hypot(*(xyz[i] - a[i] - fraction * delta[i] for i in range(2)))
    if (
        not all(
            type(v) in (int, float) and math.isfinite(v)
            for v in (
                *xyz,
                *row["velocity_ned"],
                row["sim_s"],
                row["wall_s"],
                row["vehicle"]["age_s"],
                row["vehicle"]["sensor_sim_s"],
                row["battery_fraction"],
            )
        )
        or row["run_id"] != config["run_id"]
        or row["world_sha256"] != config["world"]["world_sha256"]
        or not 0 <= row["vehicle"]["age_s"] <= 2
        or row["position_valid"] is not True
        or row["battery_fraction"] < 0.2
        or row["reset_counters"] != anchor["reset_counters"]
        or (not landing and (row["arming_state"] != 2 or row["landed"] is not False))
        # Land detection may precede automatic disarm. This transition is
        # admissible while descending; terminal success still requires disarm.
        or (
            landing
            and (row["arming_state"], row["landed"]) not in ((2, False), (2, True), (1, True))
        )
        or lateral > p["corridor_half_width_m"] + p["tracking_tube_m"]
        or (not landing and abs(xyz[2] - a[2]) > 0.5)
        or (landing and not -0.2 <= xyz[2] <= a[2] + 0.5)
    ):
        raise ValueError("Recovery observation leaves approved envelope/state")


def recover(
    config,
    decisions,
    reason,
    *,
    sample,
    upload,
    activate,
    wait_for,
    land,
    contacts,
    event,
    clock,
    set_phase,
):
    """Use flight-worker callbacks only after irreversible model revocation."""
    if policy(config) is None:
        raise ValueError("Recovery authority absent")
    begin = clock()
    deadline = deadline_for(begin, config["timeout_s"] - 10)
    anchor = sample()
    validate_sample(config, anchor, anchor)
    event(
        "candidate_rejected",
        reason=str(reason),
        cycle=decisions.cycle,
        observation=anchor,
        recovery_begin_wall_s=begin,
        recovery_deadline_wall_s=deadline,
    )
    receipt = decisions.stop()
    if not decisions.closed or decisions.active or receipt.get("session_revoked") is not True:
        raise ValueError("Model authority revocation unconfirmed")
    event("model_authority_revoked", receipt=receipt)

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError("Fixed recovery deadline exceeded")
        return left

    def stable(predicate):
        first = last = None
        count = 0

        def check(row):
            nonlocal first, last, count
            remaining()
            validate_sample(config, row, anchor, landing=row["phase"] != "recovery_return")
            if not predicate(row):
                first = last = None
                count = 0
                return False
            if last is not None and (
                row["sim_s"] <= last["sim_s"]
                or row["vehicle"]["sensor_sim_s"] <= last["vehicle"]["sensor_sim_s"]
            ):
                return False
            if last is not None and (
                row["sim_s"] - last["sim_s"] > 2 or row["wall_s"] - last["wall_s"] > 3
            ):
                first = None
                count = 0
            first = row if first is None else first
            last = row
            count += 1
            return count >= 3 and row["sim_s"] - first["sim_s"] >= 2

        return check

    home = config["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]
    set_phase("recovery_return")
    event(
        "recovery_return_authorized",
        authority="separate fixed vehicle recovery",
        target_world_xyz_m=home,
        deadline_wall_s=deadline,
    )
    remaining()
    upload("01-FEEDBACK-EXIT")
    remaining()
    activate(expires_at_wall_s=deadline)
    returned = wait_for(
        stable(
            lambda r: (
                math.dist(r["vehicle"]["xyz"][:2], home[:2]) <= 0.25
                and abs(r["vehicle"]["xyz"][2] - home[2]) <= 0.15
                and math.hypot(*r["velocity_ned"]) <= 0.3
                and r["arming_state"] == 2
                and r["landed"] is False
            )
        ),
        min(150, remaining()),
    )
    event("recovery_return_observed", observation=returned)
    set_phase("recovery_land")
    remaining()
    land()
    final = wait_for(
        stable(
            lambda r: (
                r["landed"] is True
                and r["arming_state"] == 1
                and math.dist(r["vehicle"]["xyz"][:2], home[:2]) < 1.5
                and -0.2 <= r["vehicle"]["xyz"][2] <= 0.5
                and math.hypot(*r["velocity_ned"]) <= 0.3
            )
        ),
        min(90, remaining()),
    )
    fresh_contact = any(
        c["topic"] == "launch_pad"
        and "x500" in c["collision1"] + c["collision2"]
        and final["sim_s"] - 2 <= c["sensor_sim_s"] <= final["sim_s"]
        for c in contacts()
    )
    if not fresh_contact:
        raise ValueError("No fresh independent launch-pad contact")
    remaining()
    event("recovery_landing_disarm_observed", observation=final)
    return {
        "status": "failed_recovered",
        "mission_outcome": "failed",
        "recovery_outcome": "observed_return_landed_disarmed",
        "recovery_duration_wall_s": clock() - begin,
        "recovery_deadline_wall_s": deadline,
        "physical_execution_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
    }
