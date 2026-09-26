"""AeroVLA proposal-to-candidate guard, with no approval or dispatch capability.

Envelope/current state are supplied by the trusted mission host, never by the
model. A passing candidate still needs fresh clearance, an approved executor
contract, revalidation and observed PX4 completion. Safe to copy into SITL.
"""

from __future__ import annotations

import hashlib
import json
import math
import re


def content_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value, size):
    return isinstance(value, list) and len(value) == size and all(map(_number, value))


def decode_action(text):
    """Strict native bins; terminal text is not a landing authorization."""
    if not isinstance(text, str) or len(text) > 100:
        raise ValueError("malformed_action")
    value = text.strip()
    if value.endswith("</s>"):
        value = value[:-4].strip()
    if value in ("LAND", "<LAND>"):
        raise ValueError("terminal_proposal_requires_separate_workflow")
    if value.endswith(" LAND"):
        raise ValueError("terminal_proposal_requires_separate_workflow")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    match = re.fullmatch(r"(\d{1,2})[ ,]+(\d{1,2})[ ,]+(\d{1,2})", value)
    if not match:
        raise ValueError("malformed_action")
    bins = [int(v) for v in match.groups()]
    if any(v > 98 for v in bins):
        raise ValueError("action_bin_out_of_range")
    if bins == [0, 49, 49]:
        raise ValueError("terminal_proposal_requires_separate_workflow")
    return bins, [bins[0] / 98 * 5, bins[1] / 98 * 10 - 5, bins[2] / 98 * 2.2 - 1.1]


def entry_limits(urban):
    """Inspection envelope, fixed before launch and included in the plan hash."""
    entry, altitude = urban["entry_north_m"], urban["cruise_altitude_m"]
    return {
        "phase": "urban_entry",
        "north_m": [entry - 10, entry + 10],
        "east_m": [-10, 10],
        "altitude_m": [altitude - 1, altitude + 1],
        "vehicle_margin_m": 0.5,
        "max_translation_m": 5.0,
        "max_yaw_delta_rad": 1.1,
        "max_nominal_motion_s": 9.0,
        "max_observation_age_s": 2.0,
        "max_pose_drift_m": 0.5,
        "max_heading_drift_rad": 0.1,
        "max_hold_speed_mps": 0.5,
    }


def make_envelope(config):
    limits = config["urban"]["vla_guard_limits"]
    return {
        **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
        "schema_version": "ship_vla_envelope.v1",
        "execution_scope": "sim",
        "inspection_authorized": config.get("operator_approval_ref")
        == "operator:explicit-sitl-opt-in",
        "approval_ref": config.get("operator_approval_ref"),
        "clock_id": "run_elapsed:" + config["run_id"],
        "frame_id": "px4_local_ned:" + config["run_id"],
        "expires_at_s": config["timeout_s"],
        "limits": limits,
    }


def _validate_context(envelope, observation, current, now):
    limits = envelope["limits"]
    if envelope["schema_version"] != "ship_vla_envelope.v1" or not _number(now) or now < 0:
        raise ValueError("invalid_envelope_or_clock")
    if envelope["execution_scope"] != "sim":
        raise ValueError("unsupported_execution_scope")
    for key in ("run_id", "world_sha256", "plan_sha256", "clock_id", "frame_id"):
        if not isinstance(envelope[key], str) or not envelope[key]:
            raise ValueError("invalid_envelope_binding")
    if not _number(envelope["expires_at_s"]):
        raise ValueError("invalid_expiry")
    for key in ("north_m", "east_m", "altitude_m"):
        if not _vector(limits[key], 2) or limits[key][1] <= limits[key][0]:
            raise ValueError("invalid_spatial_bounds")
    for key in (
        "vehicle_margin_m",
        "max_translation_m",
        "max_yaw_delta_rad",
        "max_nominal_motion_s",
        "max_observation_age_s",
        "max_pose_drift_m",
        "max_heading_drift_rad",
        "max_hold_speed_mps",
    ):
        if not _number(limits[key]) or limits[key] <= 0:
            raise ValueError("invalid_limit")
    for state in (observation, current):
        if (
            not _vector(state["position_ned_m"], 3)
            or not _vector(state["velocity_ned_mps"], 3)
            or not _number(state["heading_ned_rad"])
            or not _number(state["observed_at_s"])
            or state["observed_at_s"] < 0
            or not _number(state["pose_age_s"])
            or state["pose_age_s"] < 0
            or not _number(state["image_age_s"])
            or state["image_age_s"] < 0
            or not isinstance(state["image_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["image_sha256"])
            or not isinstance(state["reset_counters"], list)
            or len(state["reset_counters"]) != 3
            or any(type(x) is not int or x < 0 for x in state["reset_counters"])
        ):
            raise ValueError("invalid_observation")
        for key in ("run_id", "world_sha256", "plan_sha256", "clock_id", "frame_id"):
            if state[key] != envelope[key]:
                raise ValueError("observation_binding_mismatch:" + key)


def assess_proposal(proposal, observation, current, envelope, *, now_s, archived=False):
    """Return a bounded geometric candidate, never an executor permit."""
    reasons, candidate = [], None
    result = {
        "schema_version": "ship_vla_assessment.v1",
        "status": "rejected",
        "candidate_eligible": False,
        "approval_granted": False,
        "dispatch_allowed": False,
        "dispatch_invoked": False,
        "physical_execution_invoked": False,
        "candidate": None,
        "reasons": reasons,
        "executor_requirements": [
            "fresh_collision_clearance",
            "approved_px4_controller_contract",
            "revalidate_at_dispatch",
            "verify_observed_completion",
        ],
    }
    try:
        _validate_context(envelope, observation, current, now_s)
        result.update(
            envelope_sha256=content_hash(envelope),
            observation_sha256=content_hash(observation),
            current_state_sha256=content_hash(current),
            proposal_sha256=content_hash(proposal),
            checked_at_s=now_s,
        )
        if set(proposal) != {"generated_text", "binding"}:
            raise ValueError("model_must_not_supply_authority_or_state")
        expected = {k: envelope[k] for k in ("run_id", "world_sha256", "plan_sha256")}
        expected["observation_sha256"] = content_hash(observation)
        if proposal["binding"] != expected:
            raise ValueError("proposal_binding_mismatch")
        bins, (forward, down, yaw) = decode_action(proposal["generated_text"])
        limits = envelope["limits"]
        if archived:
            reasons.append("archived_observation_not_live_authority")
        if envelope["inspection_authorized"] is not True or not envelope.get("approval_ref"):
            reasons.append("inspection_not_authorized")
        if now_s > envelope["expires_at_s"]:
            reasons.append("envelope_expired")
        for label, state in (("input", observation), ("current", current)):
            if state["phase"] != limits["phase"]:
                reasons.append(label + "_phase_mismatch")
            age = now_s - state["observed_at_s"]
            if (
                age < 0
                or age + max(state["pose_age_s"], state["image_age_s"])
                > limits["max_observation_age_s"]
            ):
                reasons.append(label + "_observation_stale_or_future")
            if (
                state["position_valid"] is not True
                or state["nav_state"] != 4
                or state["arming_state"] != 2
                or math.hypot(*state["velocity_ned_mps"]) > limits["max_hold_speed_mps"]
            ):
                reasons.append(label + "_hold_not_verified")
        if current["observed_at_s"] < observation["observed_at_s"]:
            reasons.append("current_observation_precedes_input")
        if current["reset_counters"] != observation["reset_counters"]:
            reasons.append("estimator_reset_since_input")
        if (
            math.dist(observation["position_ned_m"], current["position_ned_m"])
            > limits["max_pose_drift_m"]
        ):
            reasons.append("vehicle_moved_since_input")
        heading_drift = math.remainder(
            current["heading_ned_rad"] - observation["heading_ned_rad"], 2 * math.pi
        )
        if abs(heading_drift) > limits["max_heading_drift_rad"]:
            reasons.append("vehicle_rotated_since_input")
        # Bind derived values at a declared precision before issuing a permit.
        # Linux/macOS libm may differ by an ULP; verification stays exact.
        heading = round(math.remainder(observation["heading_ned_rad"] + yaw, 2 * math.pi), 12)
        # Match upstream yaw-first / large-yaw vertical-only semantics. No
        # clamping, altitude repair or reinterpretation as PX4 velocity.
        horizontal = forward if abs(yaw) < 0.25 else 0.0
        delta = [horizontal * math.cos(heading), horizontal * math.sin(heading), down]
        distance = math.hypot(*delta)
        translation_s = max(1.0, distance) if abs(yaw) < 0.25 else abs(down) / 2
        if distance <= 0.01:
            translation_s = 0.0
        # Upstream allows up to 3 s to rotate, then a 0.5 s braking command.
        nominal = round(3.0 + translation_s + 0.5, 9)
        start = list(observation["position_ned_m"])
        end = [round(a + b, 9) for a, b in zip(start, delta)]
        candidate = {
            "bins": bins,
            "frame_id": envelope["frame_id"],
            "start_ned_m": start,
            "target_ned_m": end,
            "target_heading_ned_rad": heading,
            "horizontal_translation_suppressed": abs(yaw) >= 0.25,
            "controller_sequence": ["yaw_then_translate", "brake"],
            "upstream_nominal_motion_s": nominal,
            "px4_motion_duration_verified": False,
        }
        if distance > limits["max_translation_m"] + 1e-9:
            reasons.append("translation_limit_exceeded")
        if abs(yaw) > limits["max_yaw_delta_rad"] + 1e-9:
            reasons.append("yaw_limit_exceeded")
        if nominal > limits["max_nominal_motion_s"]:
            reasons.append("nominal_duration_limit_exceeded")
        if now_s + nominal > envelope["expires_at_s"]:
            reasons.append("motion_outlives_envelope")
        # Each allowed box is convex: endpoints contain the whole nominal
        # straight segment, inflated by vehicle margin and observed pose drift.
        margin = limits["vehicle_margin_m"] + math.dist(start, current["position_ned_m"])
        for axis, key in enumerate(("north_m", "east_m", "altitude_m")):
            lo, hi = limits[key]
            values = [p[axis] * (-1 if axis == 2 else 1) for p in (start, end)]
            if min(values) - margin < lo or max(values) + margin > hi:
                reasons.append(
                    "altitude_envelope_violation" if axis == 2 else "horizontal_envelope_violation"
                )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        reasons.append(str(exc) if isinstance(exc, ValueError) else "invalid_contract_structure")
    result.update(candidate=candidate, reasons=list(dict.fromkeys(reasons)))
    result["candidate_eligible"] = not result["reasons"]
    result["status"] = "candidate_only" if result["candidate_eligible"] else "rejected"
    return result


def bind_proposal(text, observation, envelope):
    return {
        "generated_text": text,
        "binding": {
            **{k: envelope[k] for k in ("run_id", "world_sha256", "plan_sha256")},
            "observation_sha256": content_hash(observation),
        },
    }


def snapshot_from_px4(row, config):
    """Bind host-observed RGB to validated PX4 state, not Gazebo obstacle truth."""
    if any(row.get(key) != config[key] for key in ("run_id", "world_sha256", "plan_sha256")):
        raise ValueError("px4_sample_mission_binding_mismatch")
    q = row["attitude_q"]
    if not _vector(q, 4) or abs(sum(x * x for x in q) - 1) > 2e-4:
        raise ValueError("invalid_px4_attitude")
    w, x, y, z = q
    raw_position = row["raw_px4"]["vehicle_local_position"]
    attitude = re.search(r"\bq:\s*\[([^]]+)\]", row["raw_px4"]["vehicle_attitude"])
    if not attitude or [float(v) for v in attitude[1].split(",")] != q:
        raise ValueError("attitude_not_bound_to_px4")

    def field(raw, name):
        match = re.search(r"\b" + name + r":\s*([-+0-9.eE]+)\b", raw)
        if not match:
            raise ValueError("px4_field_missing:" + name)
        return float(match[1])

    if (
        row["local_ned"] != [field(raw_position, k) for k in "xyz"]
        or row["velocity_ned"] != [field(raw_position, "v" + k) for k in "xyz"]
        or any(
            row[k] != field(row["raw_px4"]["vehicle_status"], k)
            for k in ("nav_state", "arming_state")
        )
    ):
        raise ValueError("state_not_bound_to_px4")
    ages, resets = [], []
    for name in ("vehicle_local_position", "vehicle_attitude"):
        raw = row["raw_px4"][name]
        match = re.search(r"timestamp:\s*(\d+)\s*\(([0-9.]+) seconds ago\)", raw)
        stamp = re.search(r"timestamp_sample:\s*(\d+)", raw)
        if not match or not stamp or int(stamp[1]) > int(match[1]):
            raise ValueError("px4_timestamp_age_unavailable")
        ages.append(float(match[2]) + (int(match[1]) - int(stamp[1])) / 1e6)
        keys = (
            ("xy_reset_counter", "z_reset_counter")
            if name == "vehicle_local_position"
            else ("quat_reset_counter",)
        )
        for key in keys:
            match = re.search(r"\b" + key + r":\s*(\d+)\b", raw)
            if not match:
                raise ValueError("px4_reset_counter_unavailable")
            resets.append(int(match[1]))
    state = {
        **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
        "clock_id": "run_elapsed:" + config["run_id"],
        "frame_id": "px4_local_ned:" + config["run_id"],
        "phase": "urban_entry",
        "observed_at_s": row["elapsed_s"],
        "pose_age_s": max(ages) + row["px4_query_duration_s"],
        "image_age_s": row["onboard_frame"]["received_age_s"],
        "image_sha256": row["onboard_frame"]["sha256"],
        "position_ned_m": row["local_ned"],
        # Upstream uses scipy Rotation.as_euler('zyx')[0] (extrinsic),
        # which differs from conventional PX4 yaw when roll/pitch are nonzero.
        "heading_ned_rad": round(
            math.atan2(2 * (w * z - x * y), 1 - 2 * (y * y + z * z)), 12
        ),
        "velocity_ned_mps": row["velocity_ned"],
        "position_valid": row["position_valid"] is True
        and all(
            re.search(r"\b" + key + r":\s*True\b", raw_position)
            for key in (
                "xy_valid",
                "z_valid",
                "v_xy_valid",
                "v_z_valid",
                "heading_good_for_control",
            )
        ),
        "nav_state": row["nav_state"],
        "arming_state": row["arming_state"],
        "reset_counters": resets,
    }
    _validate_context(make_envelope(config), state, state, row["elapsed_s"])
    return state


def inspect_live_smoke(row, config):
    """Exercise the real guard using explicit test proposals; never dispatch."""
    from copy import deepcopy

    observation = snapshot_from_px4(row, config)
    envelope = make_envelope(config)
    tests = []
    rejection = {
        "descent": "altitude_envelope_violation",
        "stale": "input_observation_stale_or_future",
        "wrong_plan": "proposal_binding_mismatch",
        "outside_corridor": "horizontal_envelope_violation",
        "terminal": "terminal_proposal_requires_separate_workflow",
    }
    for name, text in (
        ("bounded_motion", "55 49 49"),
        ("descent", "55 85 49"),
        ("stale", "55 49 49"),
        ("wrong_plan", "55 49 49"),
        ("outside_corridor", "98 49 49"),
        ("terminal", "LAND"),
    ):
        sample = deepcopy(observation)
        if name == "stale":
            sample["observed_at_s"] -= 10
        if name == "outside_corridor":
            sample["position_ned_m"][0] = envelope["limits"]["north_m"][1] - 1
            sample["heading_ned_rad"] = 0.0
        proposal = bind_proposal(text, sample, envelope)
        if name == "wrong_plan":
            proposal["binding"]["plan_sha256"] = "not-the-approved-plan"
        current = observation if name == "stale" else sample
        assessment = assess_proposal(proposal, sample, current, envelope, now_s=row["elapsed_s"])
        tests.append(
            {
                "case": name,
                "proposal": proposal,
                "observation": sample,
                "current": current,
                "assessment": assessment,
            }
        )
    return {
        "schema_version": "ship_vla_guard_smoke.v1",
        "source_sample_elapsed_s": row["elapsed_s"],
        "envelope": envelope,
        "tests": tests,
        "proposal_source": "explicit_test_vectors_not_model_output",
        "vla_inference_invoked": False,
        "dispatch_invoked": False,
        "all_expected": all(
            r["assessment"]["candidate_eligible"]
            if r["case"] == "bounded_motion"
            else not r["assessment"]["candidate_eligible"]
            and rejection[r["case"]] in r["assessment"]["reasons"]
            for r in tests
        ),
    }


def verify_guard_smoke(receipt, row, config):
    expected = inspect_live_smoke(row, config)
    if receipt != expected or not expected["all_expected"]:
        raise ValueError("vla_guard_smoke_not_reproducible")
    return {
        "verified": True,
        "cases": len(expected["tests"]),
        "candidate_only": 1,
        "rejected": len(expected["tests"]) - 1,
        "vla_inference_invoked": False,
        "dispatch_invoked": False,
    }


def verify_guard_artifacts(root, config, samples, events):
    """Recompute the receipt from its recorded PX4/RGB sample and deployed code."""
    from pathlib import Path

    root = Path(root)
    path = root / "vla-guard-smoke.json"
    selected = [e for e in events if e["event"] == "vla_guard_inspected"]
    if len(selected) != 1:
        raise ValueError("vla_guard_event_missing_or_duplicated")
    event = selected[0]
    if event["receipt_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError("vla_guard_receipt_hash_mismatch")
    name = "ship_vla_adapter.py"
    digest = config["urban"]["onboard_source_sha256"][name]
    if any(
        hashlib.sha256(p.read_bytes()).hexdigest() != digest
        for p in (root / name, root / "sources" / name, Path(__file__))
    ):
        raise ValueError("vla_guard_deployed_code_mismatch")
    receipt = json.loads(path.read_text())
    matched = [r for r in samples if r["elapsed_s"] == receipt["source_sample_elapsed_s"]]
    if len(matched) != 1:
        raise ValueError("vla_guard_sample_missing_or_duplicated")
    row = matched[0]
    for key in ("run_id", "world_sha256"):
        if row[key] != config[key]:
            raise ValueError("vla_guard_sample_binding_mismatch")
    entry = [e for e in events if e["event"] == "urban_entry_observed"]
    decision = [e for e in events if e["event"] == "urban_decision"]
    if (
        len(entry) != 1
        or len(decision) != 1
        or not entry[0]["elapsed_s"]
        <= row["elapsed_s"]
        <= event["elapsed_s"]
        < decision[0]["elapsed_s"]
        or event["elapsed_s"] - row["elapsed_s"]
        > config["urban"]["vla_guard_limits"]["max_observation_age_s"]
    ):
        raise ValueError("vla_guard_phase_or_event_order_mismatch")
    frame = row["onboard_frame"]
    image_path = (root / frame["file"]).resolve()
    if not image_path.is_relative_to(root.resolve()):
        raise ValueError("vla_guard_image_path_outside_evidence")
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != frame["sha256"]:
        raise ValueError("vla_guard_image_hash_mismatch")
    return verify_guard_smoke(receipt, row, config)
