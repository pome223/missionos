"""Explicit vehicle-side shortening of a VLA translation; no dispatch authority.

Raw VLA output remains immutable. Only the length along its original translation
may decrease; full endpoint/geometry/forecast/freshness gates still apply.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re

POLICY = {
    "schema": "yokohama.vehicle-goal-distance-adapter.v1",
    "method": "closest_point_on_original_translation",
    "preserve_heading_and_yaw": True,
    "allow_extension": False,
    "grants_dispatch_authority": False,
}


def adapt_candidate(config, original, observation, response_sha256):
    from scripts.yokohama_endpoint_feedback import (
        feedback_policy,
        validate_feedback_candidate,
        validate_feedback_observation,
    )
    from src.runtime.yokohama_native import digest, vla_candidate

    enabled = config.get("decisions", {}).get("goal_distance_adapter")
    if enabled is None:
        return deepcopy(original), None
    if enabled != POLICY:
        raise ValueError("Unsupported vehicle goal-distance adapter policy")
    p = feedback_policy(config)
    if p is None:
        raise ValueError("Goal-distance adapter needs the bounded endpoint contract")
    validate_feedback_observation(config, observation, held=True)
    if not isinstance(response_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", response_sha256):
        raise ValueError("Unbound raw VLA response")
    expected = vla_candidate(" ".join(map(str, original["bins"])), observation)
    if expected != original:
        raise ValueError("Raw VLA candidate was modified before adaptation")
    start, target, goal = (
        observation["vehicle"]["xyz"],
        original["target_world_xyz_m"],
        p["goal_world_xyz_m"],
    )
    delta = [b - a for a, b in zip(start, target)]
    length = math.hypot(*delta)
    if (
        not 0.5 <= length <= p["max_segment_m"]
        or abs(target[2] - start[2]) > 0.205
        or abs(target[2] - p["entry_world_xyz_m"][2]) > 0.205
    ):
        raise ValueError("Adapter cannot rescue an unbounded or nonlevel raw proposal")
    # The closest point on the original ray prevents passage beyond the goal
    # plane. No lateral steering, yaw correction or extension is introduced.
    projection = sum((g - a) * d for g, a, d in zip(goal, start, delta)) / (length * length)
    if not math.isfinite(projection) or projection <= 0:
        raise ValueError("Raw proposal does not point toward the approved goal")
    scale = min(1.0, projection)
    selected = deepcopy(original)
    if scale < 1:
        selected["target_world_xyz_m"] = [round(a + scale * d, 9) for a, d in zip(start, delta)]
        selected["delta_body_frd"] = [scale * value for value in original["delta_body_frd"][:3]] + [
            original["delta_body_frd"][3]
        ]
    # Clipping is not approval. Keep every existing endpoint limit, including
    # the 0.5 m minimum and progress/corridor/height requirements.
    validate_feedback_candidate(config, start, selected["target_world_xyz_m"])
    receipt = {
        "schema": POLICY["schema"],
        "policy_sha256": digest(POLICY),
        "config_sha256": digest(config),
        "input_observation_sha256": digest(observation),
        "vla_response_sha256": response_sha256,
        "original_candidate": deepcopy(original),
        "executed_candidate": deepcopy(selected),
        "original_candidate_sha256": digest(original),
        "executed_candidate_sha256": digest(selected),
        "scale": scale,
        "remaining_goal_distance_m": math.dist(start, goal),
        "original_distance_m": length,
        "executed_distance_m": math.dist(start, selected["target_world_xyz_m"]),
        "reason": "shorten_at_approved_goal_plane"
        if scale < 1
        else "original_translation_within_goal_distance",
        "raw_model_output_evaluation": False,
        "grants_dispatch_authority": False,
    }
    return selected, receipt


def validate_adaptation(config, proposal):
    """Recompute the record before WAM/authorization; never adjust it again."""
    if config.get("decisions", {}).get("goal_distance_adapter") is None:
        if proposal.get("vehicle_distance_adjustment") is not None:
            raise ValueError("Undeclared vehicle candidate adjustment")
        return
    receipt = proposal.get("vehicle_distance_adjustment")
    if not isinstance(receipt, dict):
        raise ValueError("Vehicle candidate adjustment receipt missing")
    selected, expected = adapt_candidate(
        config,
        receipt["original_candidate"],
        proposal["input_observation"],
        proposal["vla_response_sha256"],
    )
    if receipt != expected or proposal["candidate"] != selected:
        raise ValueError("Vehicle candidate adjustment changed or unbound")
