"""Pure/public fixture helpers for an opt-in finite-time sensitivity diagnostic.

These tests do not execute the eight physical branches.  The ignored diagnostic
script invokes the frozen full continuation loop and retains its raw evidence.
Four projected physical coordinates do not identify the augmented actuator and
reference-memory dynamics, even if the projected eigenvalues lie inside a disk.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import _initialized, configuration
from src.runtime.starship_booster_recovery import _tower_observation
from src.runtime.starship_constrained_recovery import CONFIG as DRIVER_CONFIG
from src.runtime.starship_reference_tracking import ReferenceTracker
from src.runtime.starship_terminal_guidance import terminal_horizontal_request

COORDINATES = ("east_position_m", "east_speed_mps", "east_tilt_rad", "east_tilt_rate_rad_s")
EPSILONS = (.001, .001, .00001, .00001)  # Same four physical probe sizes as the earlier public local test.
HORIZON_S = 2.
MAXIMUM_STEPS_PER_BRANCH = 20
MAXIMUM_BRANCHES = 8


def local_fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    body, initial = _initialized(profile, catch, "booster_catch")
    site = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=initial.time_s)
    up, east, north = env.local_frame(site)
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    position = env.add(initial.r_eci_m, env.scale(up, 20.))
    gravity = env.EARTH_MU_M3_S2/env.norm(position)**2
    mass = dyn.mass_properties(body, initial.propellant_kg).mass_kg
    selected = (3, 8)
    throttle = mass*gravity/sum(body.engines[i].max_thrust_n for i in selected)
    state = replace(initial, r_eci_m=position, v_eci_mps=env.cross(earth, position),
        omega_body_rad_s=dyn.inverse_rotate(initial.q_body_to_eci, earth),
        engine_states=tuple(dyn.EngineState(throttle=throttle if i in selected else 0.) for i in range(len(body.engines))))
    previous_time = state.time_s-.1
    previous_q = dyn.quaternion_multiply(dyn.axis_angle((0., 0., 1.), -env.EARTH_ROTATION_RAD_S*.1), state.q_body_to_eci)
    frame = ConditionedGeographicFrame(previous_q, previous_time, maximum_roll_rate_rad_s=.03/.28)
    tracker = ReferenceTracker(profile, previous_q, previous_time, initial_reference_rate_eci_rad_s=earth)
    tracker.first_update = False
    snapshot = shooting.capture_context(state, profile, catch, DRIVER_CONFIG,
        phase="recovery_landing_13", start_time_s=state.time_s, deadline_s=state.time_s+1200.,
        plan={"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., 0.],
              "bank_heading_enu": [1., 0., 0.], "bank_sign": -1, "prediction_is_execution": False},
        refreshed=True, burn_start_s=None, settle_start_s=None, previous_entry_axis_enu=[0., 0., 1.],
        entry_pretrim_prepared_at_s=None, next_preview_s=state.time_s, braking_preview=None,
        prior_command_reference={"quaternion": list(previous_q), "time_s": previous_time},
        conditioned_reference=frame, reference_tracker=tracker)
    return profile, catch, body, state, snapshot, (east, north, up)


def perturbed_snapshot(snapshot, axes, component, delta):
    result = deepcopy(snapshot)
    state = dyn.state_from_dict(result["state"])
    east, north, _ = axes
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    if component == 0:
        position = env.add(state.r_eci_m, env.scale(east, delta))
        state = replace(state, r_eci_m=position, v_eci_mps=env.cross(earth, position))
    elif component == 1:
        state = replace(state, v_eci_mps=env.add(state.v_eci_mps, env.scale(east, delta)))
    elif component == 2:
        q = dyn.quaternion_multiply(dyn.axis_angle(north, delta), state.q_body_to_eci)
        state = replace(state, q_body_to_eci=q, omega_body_rad_s=dyn.inverse_rotate(q, earth))
    elif component == 3:
        state = replace(state, omega_body_rad_s=dyn.inverse_rotate(state.q_body_to_eci,
            env.add(earth, env.scale(north, delta))))
    else:
        raise ValueError("invalid_local_probe_coordinate")
    result["state"] = shooting.saved(asdict(state))
    return result


def measured_coordinates(state, body, profile, catch):
    arrival = _tower_observation(state, body, profile, catch)
    gravity = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
    _, info = terminal_horizontal_request(state, arrival, profile, catch, gravity)
    return [info["observed_cg_position_enu_m"][0], info["observed_cg_velocity_enu_mps"][0],
            info["observed_tilt_enu_rad"][0], info["observed_tilt_rate_enu_rad_s"][0]]


def clipping_counts(run, body):
    counts = {"reference_rate": 0, "reference_acceleration": 0, "feedback_acceleration": 0,
              "terminal_tilt": 0, "main_gimbal": 0, "rcs": 0}
    for point in run["recovery_record"]["checkpoints"]:
        nav, command = point["navigation"], point["command"]
        ref = nav.get("reference_tracking", {})
        counts["reference_rate"] += ref.get("rate_clipped") is True or ref.get("goal_rate_clipped") is True
        counts["reference_acceleration"] += ref.get("acceleration_clipped") is True
        counts["feedback_acceleration"] += nav.get("reference_tracking_control", {}).get("acceleration_clipped") is True
        counts["terminal_tilt"] += nav.get("terminal_horizontal_feedback", {}).get("tilt_request_saturated") is True
        if command is not None:
            counts["main_gimbal"] += any(engine.max_gimbal_rad > 0 and
                max(abs(value["gimbal_x_rad"]), abs(value["gimbal_y_rad"])) >= engine.max_gimbal_rad-1e-10
                for engine, value in zip(body.engines[:33], command["engines"][:33]))
            counts["rcs"] += any(value["throttle"] >= 1.-1e-10 for value in command["engines"][33:])
    return counts


def finite_time_summary(end_coordinates, counts):
    if len(end_coordinates) != MAXIMUM_BRANCHES or len(counts) != MAXIMUM_BRANCHES:
        raise ValueError("incomplete_local_probe_set")
    matrix = np.column_stack([(np.asarray(end_coordinates[2*i])-np.asarray(end_coordinates[2*i+1]))/(2*EPSILONS[i])
                              for i in range(4)])
    if not np.all(np.isfinite(matrix)):
        raise ValueError("nonfinite_local_sensitivity")
    poles = np.linalg.eigvals(matrix)
    clipped = any(any(value > 0 for value in entry.values()) for entry in counts)
    return {"coordinates": list(COORDINATES), "finite_time_sensitivity_matrix": matrix.tolist(),
        "projected_eigenvalues": [{"real": float(value.real), "imag": float(value.imag), "magnitude": float(abs(value))} for value in poles],
        "projected_spectral_radius": float(max(abs(value) for value in poles)),
        "clipped_or_nonsmooth": clipped,
        "interpretation": "diagnostic_only_clipped_samples" if clipped else "projected_finite_time_sensitivity_only",
        "augmented_state_stability_established": False, "nonlinear_stability_established": False,
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False,
        "limitations": ["Initial actuator and complete reference histories are held fixed, not independent sensitivity inputs.",
            "Their actual states evolve in the frozen full controller/plant during every branch.",
            "Four projected finite-time eigenvalues do not identify the augmented Markov system.",
            "Clipping makes these central secants a diagnostic rather than a smooth local stability certificate."]}


def test_public_initial_fixture_and_perturbations_preserve_identical_reference_history():
    profile, catch, body, _, snapshot, axes = local_fixture()
    for component, epsilon in enumerate(EPSILONS):
        varied = perturbed_snapshot(snapshot, axes, component, epsilon)
        shooting.validate_context(varied, profile, catch, DRIVER_CONFIG)
        assert varied["context"] == snapshot["context"]
        coordinates = measured_coordinates(dyn.state_from_dict(varied["state"]), body, profile, catch)
        assert all(math.isfinite(value) for value in coordinates)


def test_projected_eigen_summary_refuses_an_augmented_or_nonlinear_stability_claim():
    ends = []
    for index, epsilon in enumerate(EPSILONS):
        for sign in (1., -1.):
            row = [0., 0., 0., 0.]
            row[index] = sign*epsilon*.9
            ends.append(row)
    counts = [{"reference_rate": 0} for _ in ends]
    result = finite_time_summary(ends, counts)
    assert result["projected_spectral_radius"] == pytest.approx(.9)
    assert result["augmented_state_stability_established"] is result["nonlinear_stability_established"] is False
    counts[0]["reference_rate"] = 1
    result = finite_time_summary(ends, counts)
    assert result["clipped_or_nonsmooth"] is True
    assert result["interpretation"] == "diagnostic_only_clipped_samples"


def test_incomplete_local_probe_set_is_not_summarized_as_success():
    with pytest.raises(ValueError, match="incomplete_local_probe_set"):
        finite_time_summary([[0., 0., 0., 0.]], [{"reference_rate": 0}])
