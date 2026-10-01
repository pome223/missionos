"""Simulator-only qualification contract between VLA guard and PX4 executor."""

from __future__ import annotations

import math

if __package__:
    from .ship_vla_adapter import assess_proposal, content_hash, make_envelope, snapshot_from_px4
else:
    from ship_vla_adapter import assess_proposal, content_hash, make_envelope, snapshot_from_px4


def executor_contract(*, native=False):
    value = {
        "schema_version": "ship_vla_executor_contract.v1",
        "controller": "px4_position_yaw_ramp.v1",
        "execution_scope": "sim",
        "proposal_source": "explicit_fixture_not_model_output",
        "fixture_text": "55 47 55",
        "stream_hz": 20,
        "prestream_s": 1.5,
        "speed_mps": 1.0,
        "max_observed_speed_mps": 2.0,
        "timeout_s": 30.0,
        "observation_timeout_s": 0.8,
        "tracking_tube_m": 0.75,
        "target_tolerance_m": 0.25,
        "altitude_tolerance_m": 0.15,
        "yaw_tolerance_rad": 0.05,
        "settle_speed_mps": 0.3,
        "settle_s": 2.0,
    }
    if native:
        value.update(
            proposal_source="fresh_native_aerovla", prestream_fixture_text=value.pop("fixture_text")
        )
    return value


def require_contract(config):
    native = bool(config.get("urban", {}).get("aerovla_live"))
    if (
        config.get("execution_scope") != "sim"
        or config.get("operator_approval_ref") != "operator:explicit-sitl-opt-in"
        or config.get("urban", {}).get("vla_executor_contract") != executor_contract(native=native)
    ):
        raise ValueError("executor_not_explicitly_authorized")
    return config["urban"]["vla_executor_contract"]


def intersects_box(start, end, center, size, margin):
    lower, upper = 0.0, 1.0
    for a, b, c, width in zip(start, end, center, size):
        a, b, half = a - c, b - c, width / 2 + margin
        if abs(b - a) < 1e-10:
            if abs(a) > half:
                return False
            continue
        enter, leave = sorted(((-half - a) / (b - a), (half - a) / (b - a)))
        lower, upper = max(lower, enter), min(upper, leave)
        if lower > upper:
            return False
    return True


def clearance(row, candidate, config):
    """Fresh simulator geometry only; explicitly not camera obstacle perception."""
    if (
        not row.get("poses_fresh")
        or not row.get("urban_contact_monitors_connected")
        or row.get("urban_contact_observed")
    ):
        raise ValueError("fresh_simulator_clearance_unavailable")
    start = row["vehicle"]["xyz"]
    if len(start) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in start):
        raise ValueError("invalid_simulator_ego_geometry")
    delta = [b - a for a, b in zip(row["local_ned"], candidate["target_ned_m"])]
    end = [start[0] + delta[1], start[1] + delta[0], start[2] - delta[2]]
    urban = config["urban"]
    observed = row["urban_buildings"]
    if len(observed) != len(urban["buildings"]):
        raise ValueError("building_observation_missing")
    boxes = [(row["urban_obstacle"], urban["obstacle_size_m"])]
    for actual, expected in zip(observed, urban["buildings"]):
        if not actual or math.dist(actual["xyz"], expected["xyz"]) > 0.01:
            raise ValueError("building_geometry_changed")
        boxes.append((actual, expected["size"]))
    margin = (
        executor_contract()["tracking_tube_m"]
        + config["urban"]["vla_guard_limits"]["vehicle_margin_m"]
    )
    for entity, size in boxes:
        if (
            not entity
            or len(entity["xyz"]) != 3
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in entity["xyz"])
        ):
            raise ValueError("invalid_obstacle_geometry")
        if intersects_box(start, end, entity["xyz"], size, margin):
            raise ValueError("candidate_obstacle_clearance_rejected")
    return {
        "source": "fresh_gazebo_geometry_not_onboard_perception",
        "sample_elapsed_s": row["elapsed_s"],
        "start_enu_m": start,
        "end_enu_m": end,
        "margin_m": margin,
        "entities": [p["id"] for p, _ in boxes],
        "row_sha256": content_hash(row),
    }


def authorize_candidate(
    config, proposal, input_row, current_row, *, now_s, inference=None, prestream=False
):
    contract = require_contract(config)
    native = contract["proposal_source"] == "fresh_native_aerovla"
    if native and not prestream:
        if __package__:
            from .ship_aerovla_live import validate_inference
        else:
            from ship_aerovla_live import validate_inference
        if inference is None or proposal.get("generated_text") != validate_inference(
            config, input_row, inference, now_s
        ):
            raise ValueError("native_execution_requires_bound_inference")
    elif proposal.get("generated_text") != contract.get(
        "fixture_text", contract.get("prestream_fixture_text")
    ):
        raise ValueError("qualification_requires_explicit_fixture")
    observation, current = (snapshot_from_px4(r, config) for r in (input_row, current_row))
    envelope = make_envelope(config)
    assessment = assess_proposal(proposal, observation, current, envelope, now_s=now_s)
    if not assessment["candidate_eligible"]:
        raise ValueError("candidate_rejected:" + ",".join(assessment["reasons"]))
    if now_s + contract["timeout_s"] > config["timeout_s"]:
        raise ValueError("executor_outlives_mission")
    clear = clearance(current_row, assessment["candidate"], config)
    permit = {
        "schema_version": "ship_vla_execution_permit.v1",
        **{
            k: config[k] for k in ("run_id", "world_sha256", "plan_sha256", "operator_approval_ref")
        },
        "controller_sha256": content_hash(contract),
        "proposal_sha256": content_hash(proposal),
        "input_row_sha256": content_hash(input_row),
        "current_row_sha256": content_hash(current_row),
        "assessment": assessment,
        "clearance": clear,
        "candidate": assessment["candidate"],
        "issued_at_s": now_s,
        "expires_at_s": now_s + contract["timeout_s"],
        "execution_scope": "sim",
        "dispatch_allowed": True,
        "dispatch_invoked": False,
        "proposal_source": "explicit_prestream_fixture_not_model_output"
        if native and prestream
        else contract["proposal_source"],
        "vla_inference_invoked": native and not prestream,
    }
    if native and not prestream:
        permit["inference_sha256"] = content_hash(inference)
    return permit


def distance_to_segment(point, start, end):
    delta = [b - a for a, b in zip(start, end)]
    square = sum(v * v for v in delta)
    fraction = (
        max(0.0, min(1.0, sum((p - a) * d for p, a, d in zip(point, start, delta)) / square))
        if square
        else 0.0
    )
    return math.dist(point, [a + fraction * d for a, d in zip(start, delta)])


def check_execution_sample(row, initial, permit, config, *, modes):
    """Independent fresh-state, reset, path, altitude and collision constraints."""
    contract = require_contract(config)
    state = snapshot_from_px4(row, config)
    if (
        not state["position_valid"]
        or state["arming_state"] != 2
        or state["nav_state"] not in modes
        or max(state["pose_age_s"], state["image_age_s"]) > 0.6
        or state["reset_counters"] != initial["reset_counters"]
    ):
        raise ValueError("execution_state_invalid_stale_or_reset")
    if math.hypot(*state["velocity_ned_mps"]) > contract["max_observed_speed_mps"]:
        raise ValueError("execution_speed_exceeded")
    if not permit["issued_at_s"] <= row["elapsed_s"] <= permit["expires_at_s"]:
        raise ValueError("execution_permit_expired")
    limits = config["urban"]["vla_guard_limits"]
    for axis, key in enumerate(("north_m", "east_m", "altitude_m")):
        value = state["position_ned_m"][axis] * (-1 if axis == 2 else 1)
        lo, hi = limits[key]
        if not lo + limits["vehicle_margin_m"] <= value <= hi - limits["vehicle_margin_m"]:
            raise ValueError("observed_execution_envelope_violation")
    candidate = permit["candidate"]
    if (
        distance_to_segment(
            state["position_ned_m"], candidate["start_ned_m"], candidate["target_ned_m"]
        )
        > contract["tracking_tube_m"]
    ):
        raise ValueError("observed_tracking_tube_violation")
    clearance(row, candidate, config)
    return state


def at_target(state, candidate, contract):
    return (
        math.dist(state["position_ned_m"], candidate["target_ned_m"])
        <= contract["target_tolerance_m"]
        and abs(state["position_ned_m"][2] - candidate["target_ned_m"][2])
        <= contract["altitude_tolerance_m"]
        and abs(
            math.remainder(
                state["heading_ned_rad"] - candidate["target_heading_ned_rad"], 2 * math.pi
            )
        )
        <= contract["yaw_tolerance_rad"]
        and math.hypot(*state["velocity_ned_mps"]) <= contract["settle_speed_mps"]
    )
