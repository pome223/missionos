"""Experimental low-pressure capture of a fixed, reviewed desired reference.

The stopping-distance term is a heuristic request. Reference feedforward,
external moments and finite jets share the same hardware, so its scalar bound
is neither a stopping guarantee nor an entry-feasibility/admission test.
Actual pose, rates, availability and finite actuator states are never assigned.
"""

from dataclasses import replace
from hashlib import sha256
import math
from numbers import Real
from pathlib import Path

import numpy as np

from . import starship_sixdof as dyn
from .starship_booster_control import reallocate_measured_tvc
from .starship_booster_reference import BoosterReference, ReferenceNode, _parse

REFERENCE_ASSET_RELATIVE = (
    "docs/assets/starship-system-repair-20261018/booster-h0-desired-reference-v1.json"
)
REFERENCE_ASSET_SHA256 = "6ba2ba9cfab490b2b457395cff7a053f16ef02218c04fb3b531bf9a213d418c9"
REFERENCE_SOURCE_SHA256 = "ca52b1bde4ec0214087aac82cf07a3e8e19a57ab192e154d5b2dd62e4eef27fe"
REFERENCE_PROFILE_SHA256 = "4a0bebe69c97978daeabe7f16a4b30a1b69864a75668217106b1ca0ef4439df5"
REFERENCE_DATA_SHA256 = "7c52ba682b8e8e992bca6ae7ea030560a06648bfdbb080cb32d1801b9c76e4f7"
POLICY_ID = "fixed_h0_low_pressure_reference_capture_v1"


def _number(value, name, *, positive=False):
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError("invalid_low_pressure_capture_" + name)
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid_low_pressure_capture_" + name) from exc
    if not math.isfinite(value) or positive and value <= 0:
        raise ValueError("invalid_low_pressure_capture_" + name)
    return value


def _checked_reference(reference):
    if (
        type(reference) is not BoosterReference
        or reference.source_sha256 != REFERENCE_SOURCE_SHA256
        or reference.profile_sha256 != REFERENCE_PROFILE_SHA256
        or reference.reference_data_sha256 != REFERENCE_DATA_SHA256
    ):
        raise ValueError("fixed_reviewed_booster_reference_required")
    return reference


def _load_reference_bytes(raw):
    if type(raw) is not bytes or sha256(raw).hexdigest() != REFERENCE_ASSET_SHA256:
        raise ValueError("reviewed_booster_reference_asset_sha256_mismatch")
    record = _parse(raw, maximum_bytes=128 * 1024)
    if (
        type(record) is not dict
        or record.get("schema") != "missionos.booster_desired_reference.v1"
        or record.get("source_sha256") != REFERENCE_SOURCE_SHA256
        or record.get("profile_sha256") != REFERENCE_PROFILE_SHA256
        or record.get("reference_data_sha256") != REFERENCE_DATA_SHA256
        or record.get("desired_reference_only") is not True
        or record.get("actual_state_assigned") is not False
        or record.get("qualification_refreshed") is not False
        or record.get("first_landing_request_is_phase_transition_not_ignition") is not True
        or type(record.get("nodes")) is not list
    ):
        raise ValueError("reviewed_booster_reference_contract_mismatch")
    nodes = []
    for node in record["nodes"]:
        if (
            type(node) is not dict
            or set(node) != {"time_s", "q_body_to_eci", "omega_body_rad_s", "flap_angles_rad"}
            or any(
                type(node.get(key)) is not list
                for key in ("q_body_to_eci", "omega_body_rad_s", "flap_angles_rad")
            )
        ):
            raise ValueError("reviewed_booster_reference_node_mismatch")
        validated = ReferenceNode(
            node["time_s"],
            tuple(node["q_body_to_eci"]),
            tuple(node["omega_body_rad_s"]),
            tuple(node["flap_angles_rad"]),
        )
        # These desired values have already been normalized by the reviewed
        # extractor. Re-normalizing a serialized unit quaternion can move one
        # ulp and change its source-bound data digest. Validate normally first,
        # then retain the canonical desired tuple, never an actual plant state.
        object.__setattr__(
            validated, "q_body_to_eci", tuple(float(x) for x in node["q_body_to_eci"])
        )
        nodes.append(validated)
    reference = _checked_reference(
        BoosterReference(
            tuple(nodes),
            record["reference_cutoff_time_s"],
            record["source_sha256"],
            record["profile_sha256"],
            record["maximum_sample_gap_s"],
            record["first_landing_request_s"],
        )
    )
    if reference.record() != record:
        raise ValueError("reviewed_booster_reference_reconstruction_mismatch")
    return reference


def load_reference():
    """Read only the fixed reviewed asset; callers may retain this immutable value.

    No private output path, saved vehicle state, controller or physics runner is
    loaded. The profile pin describes the source data; a registered caller must
    separately freeze its actual runtime profile and source.
    """
    return _load_reference_bytes(
        (Path(__file__).resolve().parents[2] / REFERENCE_ASSET_RELATIVE).read_bytes()
    )


def capture_control(state, craft, profile, *, coast_started_s, control_interval_s, reference=None):
    """Request finite physical jets around actual state; do not propagate a plant."""
    coast_started = _number(coast_started_s, "coast_started_s")
    interval = _number(control_interval_s, "control_interval_s", positive=True)
    if coast_started < 0 or interval > 1:
        raise ValueError("invalid_low_pressure_capture_clock_or_interval")
    reference = _checked_reference(load_reference() if reference is None else reference)
    desired = reference.at(state.time_s, actual_cutoff_time_s=coast_started)
    observed = dyn.observe(state, craft)
    inertia = np.asarray(observed["inertia_kg_m2"], dtype=float)
    omega = np.asarray(state.omega_body_rad_s, dtype=float)
    actual_q = dyn.normalize_quaternion(state.q_body_to_eci)
    inverse = (actual_q[0], *(-x for x in actual_q[1:]))
    target_q = desired.q_body_to_eci
    error = np.asarray(dyn.quaternion_multiply(inverse, target_q), dtype=float)
    if error[0] < 0:
        error = -error
    vector_norm = float(np.linalg.norm(error[1:]))
    angle = 2 * math.atan2(vector_norm, max(0.0, float(error[0])))
    axis = error[1:] / vector_norm if vector_norm > 1e-12 else np.zeros(3)
    reference_rate_eci = dyn.rotate(target_q, desired.omega_reference_body_rad_s)
    reference_alpha_eci = dyn.rotate(target_q, desired.alpha_reference_body_rad_s2)
    reference_rate = np.asarray(dyn.rotate(inverse, reference_rate_eci))
    reference_alpha = np.asarray(dyn.rotate(inverse, reference_alpha_eci))
    capacities = np.zeros((3, 2))
    delays = [interval]
    unavailable = []
    for index, (engine, actual) in enumerate(zip(craft.engines, state.engine_states)):
        if not actual.available:
            unavailable.append(index)
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            coordinate, sign = int(coordinate), int(sign)
            if coordinate not in range(3) or sign not in (-1, 1):
                raise ValueError("invalid_low_pressure_capture_signed_jet_identity")
            lever = np.asarray(engine.position_body_m) - observed["com_body_m"]
            torque = np.cross(lever, np.asarray(engine.direction_body) * engine.max_thrust_n)
            capacities[coordinate, int(sign > 0)] += abs(float(torque[coordinate]))
            delays.append(engine.throttle_time_constant_s + interval)
    limits = np.min(capacities, axis=1)
    inertia_bound = float(np.max(np.sum(np.abs(inertia), axis=1)))
    configured_alpha = _number(
        profile["guidance"]["max_angular_acceleration_rad_s2"], "alpha_limit", positive=True
    )
    if not np.isfinite(inertia).all() or inertia_bound <= 0:
        raise ValueError("invalid_low_pressure_capture_actual_inertia")
    alpha = min(configured_alpha, float(np.min(limits)) / inertia_bound)
    delay = max(delays)
    speed = math.sqrt((alpha * delay) ** 2 + 2 * alpha * angle) - alpha * delay
    desired_rate = reference_rate + axis * speed
    transport = -np.cross(omega, reference_rate)
    acceleration_unclipped = (desired_rate - omega) / (2 * delay) + reference_alpha + transport
    acceleration = acceleration_unclipped.copy()
    magnitude = float(np.linalg.norm(acceleration))
    if magnitude > alpha:
        acceleration *= alpha / magnitude
    rigid = inertia @ acceleration + np.cross(omega, inertia @ omega)
    aero = np.asarray(observed["aero_torque_body_nm"])
    gravity_gradient = np.asarray(observed["gravity_gradient_torque_body_nm"])
    wanted_unclipped = rigid - aero - gravity_gradient
    wanted = np.clip(wanted_unclipped, -limits, limits)
    if not all(
        np.isfinite(value).all()
        for value in (
            reference_rate,
            reference_alpha,
            acceleration_unclipped,
            acceleration,
            wanted_unclipped,
            wanted,
        )
    ):
        raise ValueError("nonfinite_low_pressure_capture_request")
    commands = []
    for engine, actual in zip(craft.engines, state.engine_states):
        command = dyn.EngineCommand()
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            coordinate, sign = int(coordinate), int(sign)
            capacity = capacities[coordinate, int(sign > 0)]
            throttle = float(
                max(0.0, min(1.0, float(wanted[coordinate]) * sign / max(capacity, 1e-30)))
            )
            command = dyn.EngineCommand(throttle > 1e-8, throttle if throttle > 1e-8 else 0.0)
        commands.append(command)
    receipt = {
        "schema": "missionos.booster_low_pressure_reference_capture.v1",
        "policy_id": POLICY_ID,
        "desired_reference": desired.record(),
        "coast_started_s": coast_started,
        "control_interval_s": interval,
        "actual_q_body_to_eci": list(actual_q),
        "actual_omega_body_rad_s": omega.tolist(),
        "target_q_body_to_eci": list(target_q),
        "reference_rate_eci_rad_s": list(reference_rate_eci),
        "reference_acceleration_eci_rad_s2": list(reference_alpha_eci),
        "reference_rate_actual_body_rad_s": reference_rate.tolist(),
        "reference_acceleration_actual_body_rad_s2": reference_alpha.tolist(),
        "error_axis_actual_body": axis.tolist(),
        "error_angle_rad": angle,
        "stopping_rate_correction_rad_s": speed,
        "desired_rate_body_rad_s": desired_rate.tolist(),
        "frame_transport_correction_body_rad_s2": transport.tolist(),
        "requested_acceleration_unclipped_body_rad_s2": acceleration_unclipped.tolist(),
        "requested_acceleration_body_rad_s2": acceleration.tolist(),
        "configured_alpha_limit_rad_s2": configured_alpha,
        "inertia_row_sum_bound_kg_m2": inertia_bound,
        "stopping_alpha_rad_s2": alpha,
        "actuator_delay_s": delay,
        "signed_jet_capacity_negative_positive_nm": capacities.tolist(),
        "bidirectional_jet_limits_nm": limits.tolist(),
        "requested_rigid_torque_body_nm": rigid.tolist(),
        "subtracted_aero_torque_body_nm": aero.tolist(),
        "subtracted_gravity_gradient_torque_body_nm": gravity_gradient.tolist(),
        "requested_torque_unclipped_body_nm": wanted_unclipped.tolist(),
        "requested_torque_body_nm": wanted.tolist(),
        "unavailable_engine_indices": unavailable,
        "main_moment_subtracted_before_allocator": False,
        "reference_flaps_applied": False,
        "actual_flap_angles_held": True,
        "stopping_law_is_heuristic_request": True,
        "stopping_guarantee": False,
        "entry_admission_granted": False,
        "finite_actuator_tracking_required": True,
        "actual_state_assigned": False,
        "physics_limits_changed": False,
        "qualification_refreshed": False,
        "model_calls": 0,
    }
    diagnostic = {
        "control_allocation": "coast_stopping_distance_reference_capture_v1",
        "target_q_body_to_eci": list(target_q),
        "attitude_error_deg": math.degrees(angle),
        "requested_torque_body_nm": wanted.tolist(),
        "coast_rate_reference_body_rad_s": desired_rate.tolist(),
        "coast_rate_limit_rad_s": speed,
        "coast_stopping_alpha_rad_s2": alpha,
        "coast_actuator_delay_s": delay,
        "coast_available_jet_torque_nm": limits.tolist(),
        "main_engine_commands_off": True,
        "low_pressure_reference_capture": receipt,
    }
    command, diagnostic = reallocate_measured_tvc(
        state,
        craft,
        dyn.Command6DOF(tuple(commands), state.flap_angles_rad),
        diagnostic,
        profile,
        observed=observed,
        recenter_uncommanded_main=True,
    )
    # The existing measured allocator uses common nominal RCS pairs. Availability
    # is an actual fact; never request an unavailable jet after its residual pass.
    command = replace(
        command,
        engines=tuple(
            target if actual.available else dyn.EngineCommand()
            for target, actual in zip(command.engines, state.engine_states)
        ),
    )
    return command, diagnostic
