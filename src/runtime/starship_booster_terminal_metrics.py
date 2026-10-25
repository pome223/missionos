"""Saved-only availability-aware Booster control and whole-mission checks.

Current-load arithmetic validates recorded allocation witnesses. No controller,
plant, trajectory forecast, model provider or qualification is invoked.
"""
import math
import re

import numpy as np

from . import starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from .starship_experimental_mission_metrics import (
    STATE_FIELDS, _require, _same, _verify_booster, digest)
from .starship_state_mission_metrics import _verify_state_physical_mission
from .starship_booster_outage_metrics import _allocation

POLICY = "availability_terminal_recovery_v1"
MODES = {"powered_reference_capture", "powered_reference_shutdown_tail", "low_pressure_residual_capture", "low_pressure_reference_capture", "legacy_healthy_control", "measured_tvc_shutdown_tail",
    "measured_tvc_recentered_coast", "measured_tvc_available_main_control", "legacy_degraded_control"}
PHASES = {"booster_boostback_slew", "booster_boostback", "booster_entry_coast", "booster_landing_burn"}


def _settings(value):
    from .starship_booster_terminal_control import BoosterTerminalRecoverySettings
    _require(type(value) is dict, "Explicit frozen availability-aware control settings required")
    wanted = BoosterTerminalRecoverySettings(value.get("powered_terminal_alignment", True),
        shutdown_tail_only=value.get("shutdown_tail_only", False),
        low_pressure_reference_capture=value.get("low_pressure_reference_capture", False),
        powered_reference_capture=value.get("powered_reference_capture", False),
        residual_authority_capture=value.get("residual_authority_capture", False),
        energy_terminal_descent=value.get("energy_terminal_descent", False),
        planned_capture_impulse_compensation=value.get("planned_capture_impulse_compensation", False),
        terminal_horizontal_braking=value.get("terminal_horizontal_braking", False)).record()
    _require(value == wanted, "Explicit frozen availability-aware control settings required")
    return wanted


def _hash(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _near(actual, wanted, message, tolerance=1e-5):
    left, right = np.asarray(actual, dtype=float), np.asarray(wanted, dtype=float)
    _require(left.shape == right.shape and np.all(np.isfinite(left))
        and np.allclose(left, right, rtol=2e-10, atol=tolerance), message)


def _availability(sample, separation):
    states, initial = sample.get("engine_states"), separation.get("engine_states")
    _require(type(states) is list and len(states) == len(initial)
        and all(type(row.get("available")) is bool and row["available"] is before["available"]
            for row, before in zip(states, initial)), "Inherited engine availability was reset or changed")
    return True


def _receipt(actual, wanted, message):
    """Compare independently rebuilt native receipts without boolean coercion."""
    if type(wanted) is dict:
        _require(type(actual) is dict and set(actual) == set(wanted), message)
        for key, value in wanted.items():
            _receipt(actual[key], value, message + ": " + key)
    elif type(wanted) is list:
        _require(type(actual) is list and len(actual) == len(wanted), message)
        for left, right in zip(actual, wanted):
            _receipt(left, right, message)
    elif type(wanted) in (bool, str) or wanted is None:
        _require(type(actual) is type(wanted) and actual == wanted, message)
    elif type(wanted) is int:
        _require(type(actual) is int and actual == wanted, message)
    else:
        _require(saved._number(actual), message)
        _near(actual, wanted, message)


def _desired_reference(state, coast_started_s):
    # This is pure interpolation of the fixed public desired-only asset. It
    # neither loads a private flight nor assigns/propgates an actual state.
    from .starship_booster_lowq_capture import load_reference
    return load_reference().at(state.time_s, actual_cutoff_time_s=coast_started_s)


POWER_STOPPED = {"completed", "incomplete_timeout", "incomplete_reserve", "incomplete_no_center"}


def _powered_request(actual, state, craft, coast, *, previous, first, stopped):
    desired = _desired_reference(state, coast)
    q = dyn.normalize_quaternion(state.q_body_to_eci)
    inverse = (q[0], *(-x for x in q[1:]))
    error = dyn.quaternion_multiply(inverse, desired.q_body_to_eci)
    angle = math.degrees(2*math.acos(min(1., abs(float(error[0])))))
    rate = dyn.rotate(inverse, dyn.rotate(desired.q_body_to_eci, desired.omega_reference_body_rad_s))
    acceleration = dyn.rotate(inverse, dyn.rotate(desired.q_body_to_eci, desired.alpha_reference_body_rad_s2))
    rate_error = math.hypot(*(x-y for x, y in zip(state.omega_body_rad_s, rate)))
    elapsed = state.time_s-coast
    center = next((i for i in range(3) if state.engine_states[i].available), None)
    status = previous
    if previous not in POWER_STOPPED:
        if state.propellant_kg <= 75000.:
            status = "incomplete_reserve"
        elif elapsed > 15.+1e-9:
            status = "incomplete_timeout"
        elif angle <= 1. and rate_error <= .001:
            status = "completed"
        elif elapsed >= 15.-1e-9:
            status = "incomplete_timeout"
        elif center is None:
            status = "incomplete_no_center"
        else:
            status = "active"
    active = status == "active"
    from dataclasses import asdict
    expected = {"schema":"missionos.booster_powered_reference_capture_request.v1",
        "policy_id":"fixed_h0_powered_reference_capture_v1", "time_s":state.time_s,
        "coast_started_s":coast, "started_s":coast, "first_request_time_s":first,
        "elapsed_s":float(elapsed), "status":status, "previous_status":previous,
        "status_changed":status != previous,
        "stopped_s":state.time_s if previous not in POWER_STOPPED and status in POWER_STOPPED else stopped,
        "active":active, "count":center+1 if active else 0,
        "throttle":float(craft.engines[center].min_throttle) if active else 0.,
        "selected_center_index":center if active else None,
        "maximum_duration_s":15., "minimum_propellant_kg":75000., "maximum_attitude_error_deg":1.,
        "maximum_body_rate_error_rad_s":.001, "guard_clock_tolerance_s":1e-9, "desired_reference":desired.record(),
        "reference_rate_actual_body_rad_s":list(rate), "reference_acceleration_actual_body_rad_s2":list(acceleration),
        "actual_q_body_to_eci":list(q), "actual_body_rate_rad_s":list(state.omega_body_rad_s),
        "actual_propellant_kg":float(state.propellant_kg), "actual_state_sha256":digest(asdict(state)),
        "attitude_error_deg":angle, "body_rate_error_rad_s":rate_error,
        "guard_order":"reserve; time strictly beyond limit; measured achievement; time at limit; no available center",
        "request_is_execution":False, "actual_state_assigned":False, "physics_limits_changed":False,
        "qualification_refreshed":False, "model_calls":0}
    _receipt(actual, expected, "Powered reference request differs from actual pose/rate/fuel and sticky pulse guard")
    return expected


def _powered_control(row, state, command, craft, profile, observed, request, tail):
    """Rebuild existing tracking PD and finite-load allocation from saved facts."""
    diagnostic = row["controller"]
    info = diagnostic.get("powered_reference_capture")
    mode = "powered_reference_capture" if request["active"] else "powered_reference_shutdown_tail"
    _receipt(info, {"schema":"missionos.booster_powered_reference_capture_control.v1",
        "policy_id":"fixed_h0_powered_reference_capture_v1", "mode":mode,"request":request,
        "tail_tracking":tail, "recenter_uncommanded_main":not tail,
        "reference_transport_cross_term_applied_by_existing_controller":True,
        "finite_actuator_tracking_required":True,"actual_state_assigned":False,
        "physics_limits_changed":False,"qualification_refreshed":False,"model_calls":0},
        "Powered reference allocation used a different request or control scope")
    q, omega = state.q_body_to_eci, np.asarray(state.omega_body_rad_s)
    target = request["desired_reference"]["q_body_to_eci"]
    error = np.asarray(dyn.quaternion_multiply((q[0], *(-x for x in q[1:])), target))
    if error[0] < 0:
        error = -error
    g = profile["guidance"]
    rate = np.asarray(request["reference_rate_actual_body_rad_s"])
    acceleration = np.asarray(request["reference_acceleration_actual_body_rad_s2"])
    limit, frequency, damping = (g[k] for k in ("max_angular_acceleration_rad_s2","attitude_frequency_rad_s","attitude_damping_ratio"))
    _require(np.linalg.norm(rate) <= limit/frequency+1e-10 and np.linalg.norm(acceleration) <= limit+1e-10,
        "Powered desired reference exceeds unchanged tracking input bounds")
    cross = np.cross(omega, rate)
    raw = 2*frequency**2*error[1:]-2*damping*frequency*(omega-rate)+acceleration-cross
    clipped = np.clip(raw, -limit, limit)
    inertia = np.asarray(observed["inertia_kg_m2"])
    rigid = inertia@clipped+np.cross(omega, inertia@omega)
    aero = np.asarray(observed["aero_torque_body_nm"])
    wanted = rigid-aero
    tracking = {"schema":"missionos.starship_reference_tracking_control.v1","policy_id":"bounded_reference_tracking_v1",
        "reference_rate_body_rad_s":rate.tolist(),"reference_acceleration_body_rad_s":acceleration.tolist(),
        "actual_body_rate_rad_s":omega.tolist(),"transport_term_body_rad_s2":cross.tolist(),
        "raw_requested_acceleration_body_rad_s2":raw.tolist(),"limited_requested_acceleration_body_rad_s2":clipped.tolist(),
        "component_acceleration_limit_rad_s2":float(limit),"rate_ceiling_rad_s":float(limit/frequency),
        "acceleration_clipped":bool(np.any(raw != clipped)),"reference_tracking_is_execution":False,
        "requested_rigid_body_torque_body_nm":rigid.tolist(),"actual_aero_torque_body_nm":aero.tolist(),
        "pre_fin_torque_body_nm":wanted.tolist()}
    _receipt(diagnostic.get("reference_tracking_control"),tracking,"Powered reference PD/transport differs from actual inputs")
    _near(diagnostic["requested_torque_body_nm"],wanted,"Powered reference torque differs from existing-gain rigid-body request")
    _near(diagnostic["target_q_body_to_eci"],target,"Powered capture target is not the pinned desired-only reference",1e-12)
    _near(diagnostic["attitude_error_deg"],math.degrees(2*math.acos(min(1.,abs(error[0])))),"Powered attitude error differs",1e-9)
    _require(diagnostic.get("control_allocation") == "bounded_linearized_flap_gimbal_plus_physical_jet_pairs"
        and tuple(command.flap_angles_rad) == state.flap_angles_rad,
        "Powered capture changed the existing allocation or applied reference flaps")
    count, throttle = request["count"], request["throttle"]
    _require(all(actual.available or not target_engine.enabled and target_engine.throttle == 0
        and target_engine.gimbal_x_rad == target_engine.gimbal_y_rad == 0
        for actual,target_engine in zip(state.engine_states,command.engines)),
        "Powered capture did not preserve the unavailable-engine command inhibit")
    _require(all(e.enabled is (i<count and throttle>0 and state.engine_states[i].available)
        and e.throttle == (throttle if i<count and state.engine_states[i].available else 0.)
        for i,e in enumerate(command.engines[:33])),"Powered reference pulse changed its finite physical center request")
    columns, channels, moment = [], [], np.zeros(3)
    thrust = 0.
    for index,(engine,actual,load) in enumerate(zip(craft.engines,state.engine_states,observed["engine_loads"])):
        if engine.name.startswith("rcs_"):
            continue
        force = load["thrust_n"]
        thrust += force
        moment += load["torque_body_nm"]
        if not actual.available or force<=0 or engine.max_gimbal_rad<=0:
            continue
        gx,gy = actual.gimbal_x_rad,actual.gimbal_y_rad
        lever = np.asarray(engine.position_body_m)-observed["com_body_m"]
        dx = (-math.sin(gy)*math.sin(gx),-math.cos(gx),-math.cos(gy)*math.sin(gx))
        dy = (math.cos(gy)*math.cos(gx),0.,-math.sin(gy)*math.cos(gx))
        columns.extend((np.cross(lever,force*np.asarray(dx)),np.cross(lever,force*np.asarray(dy))))
        channels.extend(((index,"gimbal_x_rad"),(index,"gimbal_y_rad")))
    allocation = diagnostic.get("measured_tvc_allocation")
    _require(type(allocation) is dict and allocation.get("policy_id") ==
        ("measured_tvc_v1" if tail else "measured_tvc_coast_recenter_v2"), "Powered capture lacks current finite-load allocation")
    _near(allocation.get("main_thrust_n"),thrust,"Powered TVC thrust differs from actual finite loads")
    _near(allocation.get("current_main_torque_body_nm"),moment,"Powered TVC omitted current shutdown torque")
    _require(type(allocation.get("gimballed_engine_count")) is int and allocation["gimballed_engine_count"] == len(channels)//2
        and allocation.get("main_throttle_commands_preserved") is True
        and allocation.get("applied") is bool(columns),"Powered TVC claimed absent finite authority")
    if not tail:
        _require(allocation.get("coast_recenter_requested") is False
            and allocation.get("gimbal_allocation_applied") is bool(columns)
            and allocation.get("finite_actuator_tracking_required") is True,
            "Powered pulse confused commanded-on engines with coast recentering")
    if columns:
        matrix = np.asarray(columns).T
        delta = np.linalg.lstsq(matrix,wanted-moment,rcond=1e-9)[0]
        actual_delta = []
        for value,(index,axis) in zip(delta,channels):
            current = getattr(state.engine_states[index],axis)
            bound = craft.engines[index].max_gimbal_rad
            target_angle = float(np.clip(current+value,-bound,bound))
            _near(getattr(command.engines[index],axis),target_angle,"Powered TVC request does not solve current finite-load geometry",1e-10)
            actual_delta.append(target_angle-current)
        residual = wanted-moment-matrix@np.asarray(actual_delta)
        _near(allocation.get("predicted_residual_torque_body_nm"),residual,"Powered finite-load residual differs")
        capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        for engine,actual,request_engine in zip(craft.engines,state.engine_states,command.engines):
            if engine.name.startswith("rcs_"):
                _,axis,sign,_ = engine.name.split("_")
                value = max(0.,min(1.,float(residual[int(axis)])*int(sign)/capacity)) if actual.available else 0.
                _require(request_engine.enabled is (value>1e-8) and saved._near(request_engine.throttle,value if value>1e-8 else 0.,1e-10),
                    "Powered capture jets differ from measured-load residual")
    else:
        # With no achieved gimballed thrust the existing allocator preserves
        # its original request. Check that request arithmetically, without
        # falsely declaring the requested gimbals to be achieved authority.
        raw_columns, raw_channels, fixed = [], [], np.zeros(3)
        for index,engine in enumerate(craft.engines[:count]):
            if not state.engine_states[index].available:
                continue
            force = engine.max_thrust_n*throttle
            lever = np.asarray(engine.position_body_m)-observed["com_body_m"]
            fixed += np.cross(lever,(0.,0.,force))
            if engine.max_gimbal_rad and force>0:
                raw_columns.extend((np.cross(lever,(0.,-force,0.)),np.cross(lever,(force,0.,0.))))
                raw_channels.extend(((index,"gimbal_x_rad"),(index,"gimbal_y_rad")))
        residual = wanted-fixed
        angles = {}
        if raw_columns:
            matrix = np.asarray(raw_columns).T
            values = np.linalg.lstsq(matrix,residual,rcond=1e-9)[0]
            clipped_values = []
            for value,(index,axis) in zip(values,raw_channels):
                bound = craft.engines[index].max_gimbal_rad
                target_angle = float(np.clip(value,-bound,bound))
                angles[index,axis] = target_angle
                clipped_values.append(target_angle)
            residual -= matrix@np.asarray(clipped_values)
        capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        for index,(engine,target_engine) in enumerate(zip(craft.engines,command.engines)):
            if engine.name.startswith("rcs_"):
                _,axis,sign,_ = engine.name.split("_")
                fraction = max(0.,min(1.,float(residual[int(axis)])*int(sign)/capacity)) if state.engine_states[index].available else 0.
                _require(target_engine.enabled is (fraction>1e-8) and saved._near(target_engine.throttle,fraction if fraction>1e-8 else 0.,1e-10),
                    "Unspooled powered request differs from the unchanged bounded allocation")
            else:
                _near(target_engine.gimbal_x_rad,angles.get((index,"gimbal_x_rad"),0.),"Unspooled requested gimbal differs",1e-10)
                _near(target_engine.gimbal_y_rad,angles.get((index,"gimbal_y_rad"),0.),"Unspooled requested gimbal differs",1e-10)
    return mode


def _lowq_command(row, state, command, craft, profile, observed, coast_started_s):
    """Rebuild reference transport and bounded request; no control invocation."""
    diagnostic = row["controller"]
    info = diagnostic.get("low_pressure_reference_capture")
    _require(type(info) is dict and saved._number(info.get("control_interval_s"))
        and 0 < info["control_interval_s"] <= 1., "Missing finite reference-capture interval")
    interval = info["control_interval_s"]
    desired = _desired_reference(state, coast_started_s)
    q = dyn.normalize_quaternion(state.q_body_to_eci)
    inverse = (q[0], *(-x for x in q[1:]))
    error = np.asarray(dyn.quaternion_multiply(inverse, desired.q_body_to_eci))
    if error[0] < 0:
        error = -error
    sine = float(np.linalg.norm(error[1:]))
    angle = 2*math.atan2(sine, max(0., float(error[0])))
    axis = error[1:]/sine if sine > 1e-12 else np.zeros(3)
    rate_eci = dyn.rotate(desired.q_body_to_eci, desired.omega_reference_body_rad_s)
    alpha_eci = dyn.rotate(desired.q_body_to_eci, desired.alpha_reference_body_rad_s2)
    rate = np.asarray(dyn.rotate(inverse, rate_eci))
    feedforward = np.asarray(dyn.rotate(inverse, alpha_eci))
    omega = np.asarray(state.omega_body_rad_s)
    inertia = np.asarray(observed["inertia_kg_m2"])
    capacities, delays = np.zeros((3, 2)), [interval]
    for engine, actual in zip(craft.engines, state.engine_states):
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            coordinate, sign = int(coordinate), int(sign)
            _require(coordinate in range(3) and sign in (-1, 1), "Unregistered signed reference-capture jet")
            torque = np.cross(np.asarray(engine.position_body_m)-observed["com_body_m"],
                np.asarray(engine.direction_body)*engine.max_thrust_n)
            capacities[coordinate, int(sign > 0)] += abs(float(torque[coordinate]))
            delays.append(engine.throttle_time_constant_s+interval)
    limits = np.min(capacities, axis=1)
    bound = float(np.max(np.sum(np.abs(inertia), axis=1)))
    configured = float(profile["guidance"]["max_angular_acceleration_rad_s2"])
    alpha = min(configured, float(np.min(limits))/bound)
    delay = max(delays)
    speed = math.sqrt((alpha*delay)**2+2*alpha*angle)-alpha*delay
    target_rate = rate+axis*speed
    transport = -np.cross(omega, rate)
    raw_accel = (target_rate-omega)/(2*delay)+feedforward+transport
    accel = raw_accel.copy()
    if np.linalg.norm(accel) > alpha:
        accel *= alpha/float(np.linalg.norm(accel))
    rigid = inertia@accel+np.cross(omega, inertia@omega)
    aero = np.asarray(observed["aero_torque_body_nm"])
    gravity = np.asarray(observed["gravity_gradient_torque_body_nm"])
    raw_torque = rigid-aero-gravity
    torque = np.clip(raw_torque, -limits, limits)
    expected = {"schema":"missionos.booster_low_pressure_reference_capture.v1",
        "policy_id":"fixed_h0_low_pressure_reference_capture_v1", "desired_reference":desired.record(),
        "coast_started_s":float(coast_started_s), "control_interval_s":interval,
        "actual_q_body_to_eci":list(q), "actual_omega_body_rad_s":omega.tolist(),
        "target_q_body_to_eci":list(desired.q_body_to_eci), "reference_rate_eci_rad_s":list(rate_eci),
        "reference_acceleration_eci_rad_s2":list(alpha_eci),
        "reference_rate_actual_body_rad_s":rate.tolist(), "reference_acceleration_actual_body_rad_s2":feedforward.tolist(),
        "error_axis_actual_body":axis.tolist(), "error_angle_rad":angle,
        "stopping_rate_correction_rad_s":speed, "desired_rate_body_rad_s":target_rate.tolist(),
        "frame_transport_correction_body_rad_s2":transport.tolist(),
        "requested_acceleration_unclipped_body_rad_s2":raw_accel.tolist(),
        "requested_acceleration_body_rad_s2":accel.tolist(), "configured_alpha_limit_rad_s2":configured,
        "inertia_row_sum_bound_kg_m2":bound, "stopping_alpha_rad_s2":alpha, "actuator_delay_s":delay,
        "signed_jet_capacity_negative_positive_nm":capacities.tolist(), "bidirectional_jet_limits_nm":limits.tolist(),
        "requested_rigid_torque_body_nm":rigid.tolist(), "subtracted_aero_torque_body_nm":aero.tolist(),
        "subtracted_gravity_gradient_torque_body_nm":gravity.tolist(),
        "requested_torque_unclipped_body_nm":raw_torque.tolist(), "requested_torque_body_nm":torque.tolist(),
        "unavailable_engine_indices":[i for i, e in enumerate(state.engine_states) if not e.available],
        "main_moment_subtracted_before_allocator":False, "reference_flaps_applied":False,
        "actual_flap_angles_held":True, "stopping_law_is_heuristic_request":True, "stopping_guarantee":False,
        "entry_admission_granted":False, "finite_actuator_tracking_required":True,
        "actual_state_assigned":False, "physics_limits_changed":False, "qualification_refreshed":False, "model_calls":0}
    _receipt(info, expected, "Reference capture request differs from saved state and pinned desired reference")
    outer = {"control_allocation":"coast_stopping_distance_reference_capture_v1",
        "target_q_body_to_eci":list(desired.q_body_to_eci), "attitude_error_deg":math.degrees(angle),
        "requested_torque_body_nm":torque.tolist(), "coast_rate_reference_body_rad_s":target_rate.tolist(),
        "coast_rate_limit_rad_s":speed, "coast_stopping_alpha_rad_s2":alpha,
        "coast_actuator_delay_s":delay, "coast_available_jet_torque_nm":limits.tolist(), "main_engine_commands_off":True}
    for key, wanted in outer.items():
        _receipt(diagnostic.get(key), wanted, "Unbound reference-capture outer diagnostic: "+key)
    _require(tuple(command.flap_angles_rad) == state.flap_angles_rad,
        "Reference capture assigned desired flaps instead of holding actual finite angles")
    _require(all(not request.enabled and request.throttle == 0 and request.gimbal_x_rad == request.gimbal_y_rad == 0
        for engine, request in zip(craft.engines, command.engines) if not engine.name.startswith("rcs_")),
        "Reference capture restarted a main engine or assigned finite gimbal state")
    moment = np.sum([np.asarray(load["torque_body_nm"]) for engine, load in zip(craft.engines, observed["engine_loads"])
        if not engine.name.startswith("rcs_")], axis=0)
    residual = torque-moment
    allocation = diagnostic.get("measured_tvc_allocation")
    expected_allocation = {"policy_id":"measured_tvc_coast_recenter_v2",
        "main_thrust_n":float(sum(load["thrust_n"] for engine, load in zip(craft.engines, observed["engine_loads"])
            if not engine.name.startswith("rcs_"))), "gimballed_engine_count":0,
        "main_throttle_commands_preserved":True, "current_main_torque_body_nm":moment.tolist(), "applied":True,
        "coast_recenter_requested":True, "gimbal_allocation_applied":False, "finite_actuator_tracking_required":True,
        "predicted_residual_torque_body_nm":residual.tolist(),
        "allocation_scope":"commanded-off main gimbal recentering and current tail-moment RCS residual; finite tracking still required"}
    _receipt(allocation, expected_allocation, "Reference capture omitted or double-counted actual weak-tail moment")
    nominal = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
    for engine, actual, request in zip(craft.engines, state.engine_states, command.engines):
        if not engine.name.startswith("rcs_"):
            continue
        _, coordinate, sign, _ = engine.name.split("_")
        fraction = max(0., min(1., float(residual[int(coordinate)])*int(sign)/nominal)) if actual.available else 0.
        _require(request.enabled is (fraction > 1e-8) and saved._near(request.throttle, fraction if fraction > 1e-8 else 0., 1e-10)
            and request.gimbal_x_rad == request.gimbal_y_rad == 0., "Capture jet request differs from current-load residual and actual availability")


def _residual_command(row,state,command,craft,profile,observed,coast):
    """Independently account for the signed physical torque remaining after FF."""
    diagnostic=row["controller"]
    info=diagnostic.get("residual_reference_capture")
    _require(type(info) is dict and saved._number(info.get("control_interval_s"))
        and 0<info["control_interval_s"]<=1.,"Missing actual residual-capture control interval")
    interval=info["control_interval_s"]
    desired=_desired_reference(state,coast)
    q=dyn.normalize_quaternion(state.q_body_to_eci)
    inverse=(q[0],*(-x for x in q[1:]))
    error=np.asarray(dyn.quaternion_multiply(inverse,desired.q_body_to_eci))
    if error[0]<0:
        error=-error
    sine=float(np.linalg.norm(error[1:]))
    angle=2*math.atan2(sine,max(0.,float(error[0])))
    axis=error[1:]/sine if sine>1e-12 else np.zeros(3)
    rate_eci=dyn.rotate(desired.q_body_to_eci,desired.omega_reference_body_rad_s)
    alpha_eci=dyn.rotate(desired.q_body_to_eci,desired.alpha_reference_body_rad_s2)
    rate=np.asarray(dyn.rotate(inverse,rate_eci))
    reference_alpha=np.asarray(dyn.rotate(inverse,alpha_eci))
    inertia=np.asarray(observed["inertia_kg_m2"])
    omega=np.asarray(state.omega_body_rad_s)
    capacities,delays=np.zeros((3,2)),[interval]
    for engine,actual in zip(craft.engines,state.engine_states):
        if engine.name.startswith("rcs_") and actual.available:
            _,coordinate,sign,_=engine.name.split("_")
            coordinate,sign=int(coordinate),int(sign)
            _require(coordinate in range(3) and sign in (-1,1),"Unregistered signed residual-capture jet")
            torque=np.cross(np.asarray(engine.position_body_m)-observed["com_body_m"],
                np.asarray(engine.direction_body)*engine.max_thrust_n)
            capacities[coordinate,int(sign>0)]+=abs(float(torque[coordinate]))
            delays.append(engine.throttle_time_constant_s+interval)
    lower,upper=-capacities[:,0],capacities[:,1]
    delay=max(delays)
    configured=float(profile["guidance"]["max_angular_acceleration_rad_s2"])
    transport=-np.cross(omega,rate)
    feedforward=reference_alpha+transport
    gyro=np.cross(omega,inertia@omega)
    aero=np.asarray(observed["aero_torque_body_nm"])
    gravity=np.asarray(observed["gravity_gradient_torque_body_nm"])
    main=np.sum([np.asarray(load["torque_body_nm"]) for engine,load in zip(craft.engines,observed["engine_loads"])
        if not engine.name.startswith("rcs_")],axis=0)
    ff_torque=inertia@feedforward+gyro-aero-gravity-main
    feasible=bool(np.all(ff_torque>=lower) and np.all(ff_torque<=upper))
    direction=-(inertia@axis)
    directional=[float((upper[i]-ff_torque[i])/value) if value>0
        else float((ff_torque[i]-lower[i])/abs(value)) if value<0 else None for i,value in enumerate(direction)]
    stopping=max(0.,min([configured,*(value for value in directional if value is not None)])) if feasible else 0.
    speed=math.sqrt((stopping*delay)**2+2*stopping*angle)-stopping*delay
    desired_rate=rate+axis*speed
    closing=(desired_rate-omega)/(2*delay)
    raw_acceleration=feedforward+closing
    acceleration=np.clip(raw_acceleration,-configured,configured)
    rigid=inertia@acceleration+gyro
    raw_rcs=rigid-aero-gravity-main
    rcs=np.clip(raw_rcs,lower,upper)
    wanted=rcs+main
    expected={"schema":"missionos.booster_residual_reference_capture.v1",
        "policy_id":"fixed_h0_residual_authority_capture_v1","desired_reference":desired.record(),
        "coast_started_s":float(coast),"control_interval_s":interval,"actual_q_body_to_eci":list(q),
        "actual_omega_body_rad_s":omega.tolist(),"target_q_body_to_eci":list(desired.q_body_to_eci),
        "reference_rate_eci_rad_s":list(rate_eci),"reference_acceleration_eci_rad_s2":list(alpha_eci),
        "reference_rate_actual_body_rad_s":rate.tolist(),"reference_acceleration_actual_body_rad_s2":reference_alpha.tolist(),
        "frame_transport_correction_body_rad_s2":transport.tolist(),"error_axis_actual_body":axis.tolist(),"error_angle_rad":angle,
        "actual_inertia_kg_m2":inertia.tolist(),
        "signed_jet_capacity_negative_positive_nm":capacities.tolist(),"signed_jet_lower_torque_nm":lower.tolist(),
        "signed_jet_upper_torque_nm":upper.tolist(),"actuator_delay_s":delay,
        "configured_component_alpha_limit_rad_s2":configured,"feedforward_acceleration_body_rad_s2":feedforward.tolist(),
        "feedforward_rcs_torque_body_nm":ff_torque.tolist(),"feedforward_within_signed_jet_box":feasible,
        "feedforward_negative_headroom_torque_nm":(ff_torque-lower).tolist(),
        "feedforward_positive_headroom_torque_nm":(upper-ff_torque).tolist(),
        "gyro_torque_body_nm":gyro.tolist(),"actual_aero_torque_body_nm":aero.tolist(),
        "actual_gravity_gradient_torque_body_nm":gravity.tolist(),"actual_main_torque_body_nm":main.tolist(),
        "braking_direction_torque_nm_per_rad_s2":direction.tolist(),"braking_alpha_limits_by_axis_rad_s2":directional,
        "stopping_alpha_rad_s2":stopping,"stopping_rate_correction_rad_s":speed,"desired_rate_body_rad_s":desired_rate.tolist(),
        "closing_acceleration_unclipped_body_rad_s2":closing.tolist(),"requested_acceleration_raw_body_rad_s2":raw_acceleration.tolist(),
        "requested_acceleration_body_rad_s2":acceleration.tolist(),"component_acceleration_clipped":bool(np.any(raw_acceleration!=acceleration)),
        "requested_rigid_torque_body_nm":rigid.tolist(),"requested_rcs_torque_raw_body_nm":raw_rcs.tolist(),
        "requested_rcs_torque_body_nm":rcs.tolist(),"rcs_torque_clipped":bool(np.any(raw_rcs!=rcs)),
        "requested_torque_before_main_allocator_body_nm":wanted.tolist(),"main_moment_added_back_before_allocator":True,
        "main_moment_subtracted_by_allocator_once":True,"unavailable_engine_indices":[i for i,e in enumerate(state.engine_states) if not e.available],
        "available_signed_rcs_command_normalization":True,"actual_flap_angles_held":True,"reference_flaps_applied":False,
        "stopping_alpha_applied_only_to_closing_speed":True,"total_acceleration_norm_clipped":False,
        "closing_acceleration_clipped_to_stopping_alpha":False,"stopping_law_is_heuristic_request":True,
        "stopping_guarantee":False,"entry_admission_granted":False,"finite_actuator_tracking_required":True,
        "actual_state_assigned":False,"physics_limits_changed":False,"qualification_refreshed":False,"model_calls":0}
    _receipt(info,expected,"Residual capture differs from signed physical headroom and actual inputs")
    outer={"control_allocation":"residual_reference_capture_v1","target_q_body_to_eci":list(desired.q_body_to_eci),
        "attitude_error_deg":math.degrees(angle),"requested_torque_body_nm":wanted.tolist(),
        "coast_rate_reference_body_rad_s":desired_rate.tolist(),"coast_rate_limit_rad_s":speed,
        "coast_stopping_alpha_rad_s2":stopping,"coast_actuator_delay_s":delay,
        "coast_available_jet_torque_nm":np.minimum(capacities[:,0],capacities[:,1]).tolist(),"main_engine_commands_off":True}
    for key,value in outer.items():
        _receipt(diagnostic.get(key),value,"Unbound residual-capture outer diagnostic: "+key)
    _require(tuple(command.flap_angles_rad)==state.flap_angles_rad,"Residual capture applied desired fin state")
    _require(all(not e.enabled and e.throttle==0 and e.gimbal_x_rad==e.gimbal_y_rad==0 for e in command.engines[:33]),
        "Residual capture restarted mains or skipped finite gimbal recentering")
    allocation={"policy_id":"measured_tvc_coast_recenter_v2","main_thrust_n":float(sum(load["thrust_n"]
            for engine,load in zip(craft.engines,observed["engine_loads"]) if not engine.name.startswith("rcs_"))),
        "gimballed_engine_count":0,"main_throttle_commands_preserved":True,"current_main_torque_body_nm":main.tolist(),
        "applied":True,"coast_recenter_requested":True,"gimbal_allocation_applied":False,"finite_actuator_tracking_required":True,
        "predicted_residual_torque_body_nm":(wanted-main).tolist(),
        "allocation_scope":"commanded-off main gimbal recentering and current tail-moment RCS residual; finite tracking still required"}
    _receipt(diagnostic.get("measured_tvc_allocation"),allocation,"Residual capture lost exactly-once current main-moment subtraction")
    for engine,actual,target in zip(craft.engines,state.engine_states,command.engines):
        if not engine.name.startswith("rcs_"):
            continue
        _,coordinate,sign,_=engine.name.split("_")
        coordinate,sign=int(coordinate),int(sign)
        cap=capacities[coordinate,int(sign>0)]
        value=float(max(0.,min(1.,float(rcs[coordinate])*sign/max(cap,1e-30)))) if actual.available else 0.
        _require(target.enabled is (value>1e-8) and saved._near(target.throttle,value if value>1e-8 else 0.,1e-10)
            and target.gimbal_x_rad==target.gimbal_y_rad==0.,"Residual jet request borrowed nominal or unavailable signed capacity")


def _energy_descent_request(row, state, command, craft, profile, observed):
    """Independently rebuild the registered guidance request, without propagation."""
    from . import starship_physics as env
    from .starship_sixdof_contact import hull_clearance
    diagnostic = row["controller"]
    clearance = hull_clearance(state, craft, profile["geometry"]["booster_length_m"],
        profile["geometry"]["radius_m"])["signed_clearance_m"]
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    up, _east, north = env.local_frame(point)
    velocity = env.air_relative_velocity(point)
    vertical = env.dot(velocity, up)
    gravity = env.EARTH_MU_M3_S2 / env.norm(state.r_eci_m)**2
    tau = profile.get("booster_return", {}).get("landing_velocity_tau_s", 1.5)
    horizontal_tau = profile.get("booster_return", {}).get("landing_horizontal_tau_s", 8.)
    tilt = profile.get("booster_return", {}).get("landing_max_tilt_deg", 15.)
    # This explicit design is tied to the pinned 2 m/s downward water gate.
    target = 1.5
    speed = math.sqrt(target**2 + 16. * max(clearance, 0.))
    desired = -speed
    feedforward = -8. * vertical / speed if clearance >= 0 else 0.
    feedback = (desired - vertical) / tau
    raw = gravity + feedforward + feedback
    force = max(.5, raw)
    receipt = diagnostic.get("terminal_energy_descent")
    _require(type(receipt) is dict, "Missing actual energy-descent guidance receipt")
    expected = {"schema":"missionos.booster_terminal_energy_descent.v1",
        "clearance_m":clearance,"reference_height_m":max(clearance,0.),
        "vertical_speed_mps":vertical,"gravity_mps2":gravity,"target_downward_speed_mps":target,
        "velocity_tau_s":tau,"reference_deceleration_mps2":8.,"reference_speed_mps":speed,
        "desired_vertical_speed_mps":desired,"approximate_com_vertical_feedforward_mps2":feedforward,
        "feedback_acceleration_mps2":feedback,"raw_vertical_force_acceleration_mps2":raw,
        "minimum_vertical_force_acceleration_mps2":.5,"vertical_force_acceleration_mps2":force,
        "uses_com_vertical_speed_approximation":True,"stopping_guarantee":False}
    _receipt(receipt, expected, "Energy descent differs from actual clearance/velocity and fixed controller design")
    horizontal = tuple(v - vertical*u for v,u in zip(velocity, up))
    braking = bool(diagnostic["booster_terminal_recovery"]["settings"].get("terminal_horizontal_braking",False)
        and observed["altitude_m"] <= 1000.)
    if braking:
        force_body = tuple(a+b for a,b in zip(observed["thrust_force_body_n"],observed["aero_force_body_n"]))
        force_eci = dyn.rotate(state.q_body_to_eci,force_body)
        acceleration = tuple(value/observed["mass_kg"] for value in force_eci)
        acceleration_h = tuple(a-env.dot(acceleration,up)*u for a,u in zip(acceleration,up))
        frequency = profile["guidance"]["attitude_frequency_rad_s"]
        lead = 2./frequency
        predicted = tuple(v+lead*a for v,a in zip(horizontal,acceleration_h))
        horizontal_tau = 2.
        lateral = tuple(-v/horizontal_tau for v in predicted)
    else:
        lateral = tuple(-v/horizontal_tau for v in horizontal)
        _require(diagnostic.get("terminal_horizontal_braking") is None,
            "Nonactive guidance borrowed horizontal braking receipt")
    length = math.hypot(*lateral)
    scale = min(1., force*math.tan(math.radians(tilt))/max(length,1e-12))
    if braking:
        expected_braking = {"schema":"missionos.booster_terminal_horizontal_braking.v1",
            "altitude_m":observed["altitude_m"],"maximum_activation_altitude_m":1000.,
            "profile_horizontal_velocity_tau_s":profile.get("booster_return",{}).get("landing_horizontal_tau_s",8.),
            "selected_horizontal_velocity_tau_s":2.,"horizontal_velocity_eci_mps":list(horizontal),
            "uncapped_horizontal_acceleration_eci_mps2":list(lateral),
            "horizontal_acceleration_eci_mps2":[scale*a for a in lateral],
            "vertical_force_acceleration_mps2":force,"maximum_tilt_deg":tilt,
            "controller_assumption_changed":True,"physics_limits_changed":False,"actual_state_assigned":False,
            "current_mass_kg":observed["mass_kg"],"current_thrust_force_body_n":list(observed["thrust_force_body_n"]),
            "current_aero_force_body_n":list(observed["aero_force_body_n"]),"attitude_frequency_rad_s":frequency,
            "lead_horizon_s":lead,"actual_horizontal_force_acceleration_eci_mps2":list(acceleration_h),
            "predicted_horizontal_velocity_eci_mps":list(predicted),"prediction_is_achieved_state":False}
        _receipt(diagnostic.get("terminal_horizontal_braking"),expected_braking,
            "Horizontal braking differs from actual current force/attitude and declared causal lead")
    requested = tuple(force*u + scale*a for u,a in zip(up,lateral))
    axis = env.unit(requested)
    target_q = diagnostic["target_q_body_to_eci"]
    _near(dyn.rotate(target_q,(0.,0.,1.)), axis,
        "Terminal energy guidance target axis differs from actual velocity damping and vertical request", 1e-10)
    north_projected = tuple(n-env.dot(north,axis)*z for n,z in zip(north,axis))
    _near(dyn.rotate(target_q,(1.,0.,0.)), env.unit(north_projected),
        "Terminal energy target roll differs from the original local-north convention",1e-10)
    alignment = env.dot(dyn.rotate(state.q_body_to_eci,(0.,0.,1.)), axis)
    count, throttle = 0, 0.
    if alignment > .5:
        required = observed["mass_kg"] * env.norm(requested) / alignment
        available = 0.
        for count in range(1,4):
            if state.engine_states[count-1].available:
                available += craft.engines[count-1].max_thrust_n
            if available >= required:
                break
        floor = max((e.min_throttle for i,e in enumerate(craft.engines[:count])
            if state.engine_states[i].available),default=0.)
        throttle = max(floor,min(1.,required/max(available,1.)))
    metadata = diagnostic["booster_terminal_recovery"]
    _require(metadata["legacy_requested_main_count"] == count,
        "Energy descent changed actual available center prefix selection")
    _near(metadata["legacy_requested_main_throttle"], throttle,
        "Energy descent main request differs from actual force/alignment/availability",1e-10)
    for i, engine in enumerate(command.engines[:33]):
        _require(engine.enabled is (i < count), "Energy descent main enable differs from actual prefix")
        _near(engine.throttle, throttle if i < count else 0.,
            "Energy descent main throttle differs from guidance force",1e-10)


def _command_witness(row, craft, profile, separation, expected_settings=None, *, capture_context=None, horizon=None, return_site=None):
    _availability(row, separation)
    state = dyn.state_from_dict({key: row[key] for key in STATE_FIELDS})
    command = dyn.command_from_dict(row["command"])
    diagnostic = row["controller"]
    dyn._validate_pair(state, craft, command)
    for engine, actual, request in zip(craft.engines, state.engine_states, command.engines):
        _require(max(abs(actual.gimbal_x_rad), abs(actual.gimbal_y_rad), abs(request.gimbal_x_rad), abs(request.gimbal_y_rad))
            <= engine.max_gimbal_rad+1e-10, "Recorded finite gimbal exceeds unchanged hardware bounds")
    _require(len(state.flap_angles_rad) == len(craft.aero_panels)
        and all(max(abs(actual), abs(request)) <= panel.max_deflection_rad+1e-10
            for actual, request, panel in zip(state.flap_angles_rad, command.flap_angles_rad, craft.aero_panels)),
        "Finite fin or command exceeds unchanged actuator bounds")
    observed = dyn.observe(state, craft)
    degraded = any(not actual.available for actual in state.engine_states[:33])
    metadata = diagnostic.get("booster_terminal_recovery")
    settings = _settings(metadata.get("settings") if type(metadata) is dict else None)
    capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])*math.sin(engine.max_gimbal_rad)
        for engine, load in zip(craft.engines, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and engine.max_gimbal_rad > 0))
    jet_capacity = float(2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"])
    uncommanded = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])*math.sin(engine.max_gimbal_rad)
        for engine, actual, request, load in zip(craft.engines, state.engine_states, command.engines, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0
        and not (request.enabled and request.throttle > 0)))
    tail_selected = bool(degraded and uncommanded > jet_capacity)
    active = bool(degraded and (not settings.get("shutdown_tail_only", False) or uncommanded > jet_capacity))
    tail = bool(active and row["phase"] == "booster_entry_coast" and capacity > jet_capacity)
    powered_request, powered, powered_tail = None, False, False
    capture = False
    if settings.get("low_pressure_reference_capture", False):
        context_fields = {"coast_started_s", "closed"} | ({"powered_request"} if settings.get("powered_reference_capture",False) else set())
        _require(type(capture_context) is dict and set(capture_context) == context_fields
            and type(capture_context["closed"]) is bool, "Reference capture requires actual cutoff and sticky regime context")
        coast = capture_context["coast_started_s"]
        _require(coast is None or saved._number(coast) and separation["time_s"] <= coast <= state.time_s,
            "Reference capture cutoff is not an actual prior flight event")
        capture = bool(degraded and coast is not None and not capture_context["closed"]
            and observed["dynamic_pressure_pa"] <= 50. and capacity <= jet_capacity)
        _require(not capture or row["phase"] in ("booster_entry_coast", "booster_landing_burn"),
            "Reference capture escaped actual post-boostback scope")
        if settings.get("powered_reference_capture",False):
            powered_request = capture_context["powered_request"]
            powered = bool(degraded and coast is not None and not capture_context["closed"]
                and powered_request is not None and (powered_request["active"] or capacity > jet_capacity))
            powered_tail = bool(powered and not powered_request["active"])
            if powered:
                capture, active, tail = False, True, powered_tail
    wanted_mode = ("powered_reference_shutdown_tail" if powered_tail else "powered_reference_capture" if powered else
        "low_pressure_residual_capture" if capture and settings.get("residual_authority_capture",False) else "low_pressure_reference_capture" if capture else "legacy_healthy_control" if not degraded else "legacy_degraded_control" if not active else "measured_tvc_shutdown_tail" if tail else
        "measured_tvc_recentered_coast" if row["phase"] == "booster_entry_coast" else "measured_tvc_available_main_control")
    _require(expected_settings is None or settings == expected_settings,
        "Actual control sample borrowed another registered alignment policy")
    compensation = settings.get("planned_capture_impulse_compensation", False)
    energy_descent = bool(settings.get("energy_terminal_descent", False) and degraded and row["phase"] == "booster_landing_burn"
        and (not compensation or observed["altitude_m"] <= 10000.))
    compensation_active = bool(compensation and degraded)
    expected_metadata = {"schema": "missionos.booster_terminal_recovery_control.v1",
        "policy_id": POLICY, "settings": settings,
        "mode": wanted_mode, "availability_control_active": active,
        "actual_unavailable_main_indices": [i for i, e in enumerate(state.engine_states[:33]) if not e.available],
        "activation_source": "actual_main_engine_availability",
        "current_main_thrust_n": float(sum(load["thrust_n"] for engine, load in zip(craft.engines, observed["engine_loads"])
            if not engine.name.startswith("rcs_"))),
        "current_gimballed_main_torque_capacity_nm": capacity, "physical_rcs_pair_torque_nm": jet_capacity,
        "recenter_uncommanded_main": bool(active and not tail), "shutdown_tail_tvc_required": tail,
        "shutdown_tail_tvc_criterion": ("explicit_bounded_reference_pulse_or_current_capacity_shutdown_tail" if powered
            else "actual_gimballed_main_capacity_exceeds_physical_rcs_pair"),
        "legacy_guidance_preserved": not (capture or powered or energy_descent or compensation_active), "finite_fin_allocator_added": False,
        "physics_limits_changed": False, "actual_state_assigned": False, "model_calls": 0}
    if settings.get("shutdown_tail_only", False):
        expected_metadata.update(shutdown_tail_only=True,
            uncommanded_gimballed_main_torque_capacity_nm=uncommanded,
            tail_allocation_selected=tail_selected if settings.get("powered_reference_capture",False) else active)
        _require(metadata.get("shutdown_tail_only") is True and type(metadata.get("tail_allocation_selected")) is bool,
            "Shutdown-tail selection lost its declared boolean type")
    if settings.get("low_pressure_reference_capture", False):
        legacy_count, legacy_throttle = metadata.get("legacy_requested_main_count"), metadata.get("legacy_requested_main_throttle")
        _require(type(legacy_count) is int and 0 <= legacy_count <= 33 and saved._number(legacy_throttle)
            and 0 <= legacy_throttle <= 1, "Missing original legacy main request before reference capture")
        _require(row["phase"] != "booster_entry_coast" or legacy_count == 0 and legacy_throttle == 0,
            "Original coast request cannot restart main engines")
        main_on = legacy_throttle > 0
        if settings.get("powered_reference_capture",False):
            main_on = bool(powered_request["active"] if powered else False if capture else legacy_throttle>0 and legacy_count>0)
        interval = profile["integration"]["powered_dt_s"] if main_on or observed["altitude_m"] < 100000. else profile["integration"]["coast_dt_s"]
        if horizon is not None:
            interval = min(interval, horizon-state.time_s)
        if powered and powered_request["active"]:
            interval = min(interval,powered_request["coast_started_s"]+15.-state.time_s)
        _require(interval > 0, "Reference command is outside the actual execution horizon")
        expected_metadata.update(low_pressure_reference_capture=True,
            reference_capture_active=capture, reference_capture_closed=capture_context["closed"],
            coast_started_s=capture_context["coast_started_s"],
            handoff_reason="dynamic_pressure_exceeded_50pa" if capture_context["closed"] else None,
            legacy_requested_main_count=legacy_count, legacy_requested_main_throttle=legacy_throttle,
            control_interval_s=interval)
        _require(metadata.get("low_pressure_reference_capture") is True
            and type(metadata.get("reference_capture_active")) is bool
            and type(metadata.get("reference_capture_closed")) is bool,
            "Reference capture regime fields lost their boolean types")
    if settings.get("powered_reference_capture",False):
        expected_metadata.update(powered_reference_capture=True,powered_reference_control_selected=powered,
            powered_reference_tail_tracking=powered_tail,powered_reference_capture_request=powered_request)
    if settings.get("residual_authority_capture",False):
        expected_metadata.update(residual_authority_capture=True)
        _require(metadata.get("residual_authority_capture") is True, "Residual capture declaration lost boolean type")
    if settings.get("energy_terminal_descent", False):
        expected_metadata.update(energy_terminal_descent=True, energy_terminal_descent_active=energy_descent)
        _require(metadata.get("energy_terminal_descent") is True
            and type(metadata.get("energy_terminal_descent_active")) is bool,
            "Terminal descent declaration lost boolean type")
    if compensation:
        expected_metadata.update(planned_capture_impulse_compensation=True)
        _require(metadata.get("planned_capture_impulse_compensation") is True,
            "Capture impulse declaration lost boolean type")
    if settings.get("terminal_horizontal_braking", False):
        expected_metadata.update(terminal_horizontal_braking=True)
        _require(metadata.get("terminal_horizontal_braking") is True,
            "Horizontal braking declaration lost boolean type")
    _require(metadata == expected_metadata, "Terminal recovery receipt differs from actual capabilities and scope")
    _require(type(metadata["model_calls"]) is int and all(type(metadata[key]) is bool for key in (
        "availability_control_active", "recenter_uncommanded_main", "shutdown_tail_tvc_required",
        "legacy_guidance_preserved", "finite_fin_allocator_added", "physics_limits_changed", "actual_state_assigned")),
        "Terminal recovery claim fields lost their declared types")
    _require(diagnostic.get("development_fin_allocation") is None
        and diagnostic.get("finite_coast_authority") is None, "Legacy-guidance repair borrowed new fin or coast mechanisms")
    horizontal_active = bool(settings.get("terminal_horizontal_braking",False) and degraded
        and row["phase"] == "booster_landing_burn" and observed["altitude_m"] <= 1000.)
    if not horizontal_active:
        _require(diagnostic.get("terminal_horizontal_braking") is None,
            "Nonactive scope borrowed horizontal braking receipt")
    if compensation:
        from .starship_booster_impulse_metrics import verify_compensation
        verify_compensation(row, state, craft, profile, observed, return_site, capture_context)
    else:
        _require(diagnostic.get("capture_impulse_compensation") is None,
            "Original control path borrowed a planned impulse correction")
    if powered:
        _require(diagnostic.get("low_pressure_reference_capture") is None and diagnostic.get("residual_reference_capture") is None,
            "Powered pulse borrowed an RCS-only receipt")
        return _powered_control(row,state,command,craft,profile,observed,powered_request,powered_tail)
    _require(diagnostic.get("powered_reference_capture") is None,"Original/RCS control path borrowed powered capture")
    if capture:
        residual=settings.get("residual_authority_capture",False)
        receipt_key="residual_reference_capture" if residual else "low_pressure_reference_capture"
        _near(diagnostic[receipt_key]["control_interval_s"], interval,
            "Reference capture timing differs from original caller interval", 1e-12)
        _require(diagnostic.get("low_pressure_reference_capture" if residual else "residual_reference_capture") is None,
            "Reference capture borrowed the other registered control law")
        if residual:
            _residual_command(row,state,command,craft,profile,observed,capture_context["coast_started_s"])
        else:
            _lowq_command(row, state, command, craft, profile, observed, capture_context["coast_started_s"])
        return wanted_mode
    _require(diagnostic.get("low_pressure_reference_capture") is None and diagnostic.get("residual_reference_capture") is None,
        "Legacy control path borrowed a reference-capture receipt")
    if energy_descent:
        _energy_descent_request(row, state, command, craft, profile, observed)
    else:
        _require(diagnostic.get("terminal_energy_descent") is None,
            "Legacy terminal path borrowed an energy-shaped guidance receipt")
    # Reconstruct the legacy PD and local fin residual from static saved facts.
    q, w = state.q_body_to_eci, np.asarray(state.omega_body_rad_s)
    error = dyn.quaternion_multiply((q[0], *(-x for x in q[1:])), diagnostic["target_q_body_to_eci"])
    if error[0] < 0:
        error = tuple(-x for x in error)
    g = profile["guidance"]
    alpha = np.clip(2*g["attitude_frequency_rad_s"]**2*np.asarray(error[1:])
        -2*g["attitude_damping_ratio"]*g["attitude_frequency_rad_s"]*w,
        -g["max_angular_acceleration_rad_s2"], g["max_angular_acceleration_rad_s2"])
    inertia = np.asarray(observed["inertia_kg_m2"])
    demand = inertia@alpha+np.cross(w, inertia@w)-observed["aero_torque_body_nm"]
    if row["phase"] in ("booster_entry_coast", "booster_landing_burn") and observed["dynamic_pressure_pa"] > 50.:
        from dataclasses import replace
        columns, changes = [], []
        for i, panel in enumerate(craft.aero_panels):
            if not panel.name.startswith(("flap_", "grid_fin_")):
                continue
            actual = state.flap_angles_rad[i]
            delta = .01 if actual+.01 < panel.max_deflection_rad else -.01
            angles = list(state.flap_angles_rad)
            angles[i] += delta
            probe = dyn.observe(replace(state, flap_angles_rad=tuple(angles)), craft)
            columns.append((np.asarray(probe["aero_torque_body_nm"])-observed["aero_torque_body_nm"])/delta)
            changes.append(command.flap_angles_rad[i]-actual)
        if columns:
            demand -= np.asarray(columns).T@np.asarray(changes)
    _near(diagnostic["requested_torque_body_nm"], demand, "Saved PD/legacy-fin residual differs from actual inputs")
    _require(metadata.get("settings") == _settings(metadata.get("settings"))
        and metadata.get("actual_unavailable_main_indices") == [i for i, e in enumerate(state.engine_states[:33]) if not e.available]
        and metadata.get("activation_source") == "actual_main_engine_availability"
        and metadata.get("legacy_guidance_preserved") is (not (energy_descent or compensation_active))
        and metadata.get("finite_fin_allocator_added") is False
        and type(metadata.get("model_calls")) is int and metadata["model_calls"] == 0,
        "Terminal recovery used an unbound policy, oracle label or added mechanism")
    if active:
        _, capacity, jet_capacity, tail = _allocation(state, command, diagnostic, craft, profile, row["phase"])
        _near(metadata["current_gimballed_main_torque_capacity_nm"], capacity, "Current TVC capacity differs")
        _near(metadata["physical_rcs_pair_torque_nm"], jet_capacity, "Physical jet pair capacity differs")
        _require(metadata["recenter_uncommanded_main"] is (not tail), "Measured shutdown-tail scope differs")
    else:
        _require(diagnostic.get("measured_tvc_allocation") is None, "Healthy legacy path borrowed measured-TVC changes")
    if row["phase"] == "booster_landing_burn":
        alignment = diagnostic["terminal_thrust_alignment_cosine"]
        actual_axis = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
        target_axis = dyn.rotate(diagnostic["target_q_body_to_eci"], (0., 0., 1.))
        _near(alignment, sum(a*b for a, b in zip(actual_axis, target_axis)),
            "Terminal alignment is not the actual attitude against the requested direction", 1e-10)
        _require(diagnostic["terminal_braking_alignment_gate_met"] is (alignment > .5), "Actual terminal alignment gate differs")
        if degraded and settings.get("powered_terminal_alignment", True) and alignment <= .5:
            _require(all(e.enabled is (i < 3) and e.throttle == (.4 if i < 3 else 0.)
                for i, e in enumerate(command.engines[:33])), "Unavailable-path powered slew did not request exactly three finite centers")
        elif alignment <= .5:
            _require(all(not e.enabled and e.throttle == 0 for e in command.engines[:33]), "Healthy path changed original alignment gate")
    return wanted_mode


def _capture_contexts(record, craft, separation):
    """Bind the sticky gate to a forced actual crossing sample and cutoff event."""
    events = record.get("events", [])
    cutoffs = [e for e in events if e.get("event") == "booster_boostback_cutoff_command"]
    crossings = [e for e in events if e.get("event") == "booster_reference_capture_handoff"]
    _require(len(cutoffs) <= 1 and len(crossings) <= 1, "Reference capture has duplicate cutoff or handoff events")
    cutoff = cutoffs[0]["time_s"] if cutoffs else None
    _require(cutoff is None or saved._number(cutoff) and cutoff >= separation["time_s"],
        "Reference cutoff event predates actual separation")
    post_burn = [row for row in record["samples"] if row.get("command") is not None
        and row.get("phase") in ("booster_entry_coast", "booster_landing_burn")]
    _require((cutoff is None and not post_burn) or cutoff is not None and post_burn
        and post_burn[0]["time_s"] == cutoff,
        "Reference cutoff is not the actual first saved post-boostback control transition")
    degraded = any(not e["available"] for e in separation["engine_states"][:33])
    handoff = record["booster_terminal_recovery"].get("reference_capture_handoff")
    _require("reference_capture_handoff" in record["booster_terminal_recovery"], "Missing explicit actual capture-handoff result")
    if crossings:
        event = crossings[0]
        _require(degraded and cutoff is not None and saved._number(event.get("time_s")) and event["time_s"] >= cutoff,
            "Capture handoff has no actual failed-main post-cutoff scope")
        state = dyn.state_from_dict(event["state"])
        _require(state.time_s == event["time_s"], "Capture handoff state and event clocks differ")
        _availability(event["state"], separation)
        observed = dyn.observe(state, craft)
        _require(observed["dynamic_pressure_pa"] > 50., "Capture closed without actual pressure crossing")
        desired = _desired_reference(state, cutoff)
        inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
        rate = dyn.rotate(inverse, dyn.rotate(desired.q_body_to_eci, desired.omega_reference_body_rad_s))
        error = dyn.quaternion_multiply(inverse, desired.q_body_to_eci)
        angle = math.degrees(2*math.acos(min(1., abs(error[0]))))
        rate_error = float(np.linalg.norm(np.asarray(state.omega_body_rad_s)-rate))
        expected = {"time_s":state.time_s, "state":event["state"],
            "dynamic_pressure_pa":float(observed["dynamic_pressure_pa"]), "desired_reference":desired.record(),
            "full_attitude_error_deg":angle, "body_rate_error_rad_s":rate_error,
            "capture_met":bool(angle <= 5. and rate_error <= .003),
            "handoff_reason":"dynamic_pressure_exceeded_50pa", "acceptance_gate":False}
        _receipt(handoff, expected, "Actual capture handoff differs from desired-only reference and measured pose/rate")
        _receipt({k:event.get(k) for k in expected}, expected, "Capture event and result handoff differ")
        matching = [row for row in record["samples"] if row.get("command") is not None and row["time_s"] == state.time_s]
        _require(len(matching) == 1 and _same(matching[0], event["state"]),
            "Actual capture handoff is not its forced saved control sample")
        closed_time = state.time_s
    else:
        _require(handoff is None, "Capture handoff result has no actual crossing event")
        closed_time = None
    contexts = {}
    for row in record["samples"]:
        if row.get("command") is None:
            continue
        coast = cutoff if cutoff is not None and row["time_s"] >= cutoff else None
        closed = bool(closed_time is not None and row["time_s"] >= closed_time)
        if degraded and coast is not None and not closed:
            state = dyn.state_from_dict({key:row[key] for key in STATE_FIELDS})
            _require(dyn.observe(state, craft)["dynamic_pressure_pa"] <= 50.,
                "Reference capture missed an earlier actual saved pressure crossing")
        contexts[id(row)] = {"coast_started_s":coast, "closed":closed}
    return contexts


def _powered_history(record,craft,separation,contexts):
    """Check saved sticky pulse transitions, not unobserved flight dynamics."""
    history = record["booster_terminal_recovery"].get("powered_reference_capture")
    _require(type(history) is dict and set(history) == {"schema","last_request","last_request_state","status_witnesses"}
        and history["schema"] == "missionos.booster_powered_reference_capture_history.v1",
        "Missing explicit powered-reference pulse history")
    changes = [e for e in record.get("events",[]) if e.get("event") == "booster_powered_reference_capture_status"]
    degraded = any(not e["available"] for e in separation["engine_states"][:33])
    handoff = record["booster_terminal_recovery"]["reference_capture_handoff"]
    closed_time = handoff["time_s"] if handoff is not None else None
    previous,first,stopped,last_request = "pending",None,None,None
    expected_witnesses = []
    for row in record["samples"]:
        if row.get("command") is None:
            continue
        context = contexts[id(row)]
        actual = row["controller"]["booster_terminal_recovery"].get("powered_reference_capture_request")
        wanted = bool(degraded and context["coast_started_s"] is not None
            and (not context["closed"] or row["time_s"] == closed_time))
        if not wanted:
            _require(actual is None,"Powered reference request escaped actual post-cutoff pre-handoff scope")
            context["powered_request"] = None
            continue
        if first is None:
            first = context["coast_started_s"]
            _require(row["time_s"] == first,"Powered capture missed actual cutoff/start control sample")
        state_fields = {key:row[key] for key in STATE_FIELDS}
        state = dyn.state_from_dict(state_fields)
        expected = _powered_request(actual,state,craft,context["coast_started_s"],previous=previous,first=first,stopped=stopped)
        if expected["status_changed"]:
            expected_witnesses.append({"time_s":row["time_s"],"state":state_fields,"request":expected})
        previous,stopped,last_request = expected["status"],expected["stopped_s"],expected
        context["powered_request"] = expected
    _receipt(history["status_witnesses"],expected_witnesses,"Powered pulse status history lost actual forced samples")
    _require(len(changes) == len(expected_witnesses),"Powered pulse transition event history differs")
    for event,wanted in zip(changes,expected_witnesses):
        _receipt({key:event.get(key) for key in wanted},wanted,"Powered pulse transition event differs from actual saved sample")
    if last_request is None:
        _require(history["last_request"] is None and history["last_request_state"] is None,
            "No actual powered-capture scope exists for final history")
        return
    last_state_fields = history["last_request_state"]
    saved._state(last_state_fields,"$.booster_terminal_recovery.powered_reference_capture.last_request_state")
    _availability(last_state_fields,separation)
    last_state = dyn.state_from_dict(last_state_fields)
    _require(last_request["time_s"] <= last_state.time_s <= record["final_state"]["time_s"],
        "Powered final request state is outside actual recorded execution")
    if last_state.time_s == last_request["time_s"]:
        _receipt(history["last_request"],last_request,"Powered final history differs from last actual saved request")
        _require(digest(last_state_fields) == last_request["actual_state_sha256"],"Powered final state differs from actual saved request hash")
    else:
        # Time-limit/contact may end between ordinary samples. Preserve that
        # explicit final request input without calling a controller or plant.
        _require(closed_time is None,"Powered request updated after actual pressure handoff")
        final = _powered_request(history["last_request"],last_state,craft,first,
            previous=previous,first=first,stopped=stopped)
        _require(not final["status_changed"],"Unsaved powered status change has no forced witness")


def _control_metadata(record, profile, separation, settings, *, horizon=None):
    from .starship_sixdof_mission import vehicle
    metadata = record.get("booster_terminal_recovery")
    _require(type(metadata) is dict and metadata.get("schema") == "missionos.booster_terminal_recovery.v1"
        and metadata.get("policy_id") == POLICY and metadata.get("settings") == settings,
        "Missing or unbound availability-aware control metadata")
    for key, wanted in (("availability_preserved", True), ("physics_limits_changed", False),
        ("actual_state_assigned", False), ("mission_authority_granted", False), ("qualification_refreshed", False),
        ("legacy_guidance_preserved", not settings.get("low_pressure_reference_capture", False)), ("separation_state_preserved", True)):
        _require(metadata.get(key) is wanted, "Outage-control scope or physical limits changed")
    for key in ("nested_sixdof_forecasts", "hosted_model_calls"):
        _require(type(metadata.get(key)) is int and metadata[key] == 0, "Outage physical repair cannot claim model or forecast work")
    craft = vehicle(profile, "booster")
    samples = [row for row in record["samples"] if row.get("command") is not None]
    _require(samples and all(row["phase"] in PHASES for row in samples), "Missing actual control samples or unknown phase")
    contexts = _capture_contexts(record, craft, separation) if settings.get("low_pressure_reference_capture", False) else {}
    _require(settings.get("low_pressure_reference_capture", False) or "reference_capture_handoff" not in metadata,
        "Legacy control metadata borrowed a reference-capture handoff")
    if settings.get("powered_reference_capture",False):
        _powered_history(record,craft,separation,contexts)
    else:
        _require("powered_reference_capture" not in metadata
            and not any(e.get("event") == "booster_powered_reference_capture_status" for e in record.get("events",[])),
            "Unpowered reference policy borrowed a powered pulse history")
    modes = {}
    for row in samples:
        mode = _command_witness(row, craft, profile, separation, settings,
            capture_context=contexts.get(id(row)), horizon=horizon, return_site=record.get("active_return_site"))
        modes.setdefault((row["phase"], mode), row)
    witnesses = metadata.get("control_witnesses")
    _require(type(witnesses) is list and 0 < len(witnesses) <= len(PHASES)*len(MODES), "Missing or unbounded first control witnesses")
    seen = set()
    for witness in witnesses:
        key = (witness.get("phase"), witness.get("mode"))
        _require(key in modes and key not in seen and witness.get("time_s") == witness["state"]["time_s"],
            "Duplicate or unsupported actual control witness")
        seen.add(key)
        row = modes[key]
        _require(witness["time_s"] == row["time_s"] and _same(row, witness["state"])
            and witness.get("command") == row["command"] and witness.get("controller") == row["controller"],
            "First control witness is not its actual saved sample")
    _require(seen == set(modes), "Actual control mode has no first witness")
    return len(samples), len(witnesses)


def _booster(record, separation, profile, site, goal, horizon, budget, *, settings, checkpoint_required,
             availability_check=None):
    _require(not any(key in record for key in ("booster_outage_control", "booster_outage_execution", "booster_energy_control", "booster_energy_execution")), "Terminal recovery borrowed another repair scope")
    _require(not checkpoint_required or "booster_terminal_recovery_execution" not in record,
        "Fresh whole mission cannot borrow a saved-separation suffix execution")
    _verify_booster(record, separation, profile, site, goal, horizon, budget,
        availability_check=lambda sample, _kind: _availability(sample, separation)
            and (True if availability_check is None else availability_check(sample, _kind)),
        expected_guidance_policy=POLICY, checkpoint_required=checkpoint_required)
    return _control_metadata(record, profile, separation, settings, horizon=horizon)


def verify_terminal_booster_record(record, profile, *, expected_separation_state, expected_source_sha256,
        expected_source_record_sha256, expected_site, expected_goal, expected_duration_s, expected_settings):
    """Verify a saved-separation suffix; it is not a new ground launch."""
    result = {"schema": "missionos.booster_terminal_recovery_verification.v1", "passed": False, "issues": [],
        "physical_success": False, "water_entry_accepted": False, "physical_reexecution": False, "model_calls": 0,
        "ground_launch_verified": False, "qualification_refreshed": False, "physical_execution": False,
        "scope": "saved state/contact/current-load allocation consistency; no independent physical model"}
    try:
        settings = _settings(expected_settings)
        _require(_hash(expected_source_sha256) and _hash(expected_source_record_sha256)
            and type(expected_duration_s) in (int, float) and math.isfinite(expected_duration_s)
            and 0 < expected_duration_s <= 1200., "Explicit suffix source identity and elapsed bound required")
        saved._json_tree(record)
        saved._state(expected_separation_state, "$.expected_separation_state")
        _require(record.get("booster_terminal_recovery_execution") == {"schema": "missionos.booster_terminal_recovery_execution.v1",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "separation_sha256": digest(expected_separation_state), "settings_sha256": digest(settings),
            "source_record_sha256": expected_source_record_sha256}, "Suffix code/profile/source/separation/settings binding differs")
        horizon = expected_separation_state["time_s"]+expected_duration_s
        samples, witnesses = _booster(record, expected_separation_state, profile, expected_site, expected_goal, horizon, [0],
            settings=settings, checkpoint_required=False)
        from .starship_splashdown_verifier import verify_splashdown
        water = verify_splashdown(record, expected_goal)
        _require(water["passed"], "Independent saved water-contact geometry rejected")
        final = record["final_state"]
        _require(final["time_s"] <= horizon and final["time_s"] >= expected_separation_state["time_s"],
            "Suffix exceeded the registered elapsed horizon")
        result.update(passed=True, physical_success=water["controlled_water_entry_envelope_met"],
            water_entry_accepted=water["controlled_water_entry_envelope_met"],
            contact=record.get("contact") is not None, termination=record["outcome"]["termination"],
            final_propellant_kg=final["propellant_kg"], end_time_s=final["time_s"],
            verified_command_samples=samples, verified_control_witnesses=witnesses, water=water)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_booster_terminal_recovery_record", "detail": str(exc)})
    return result


def verify_terminal_state_physical_mission(record, profile, *, expected_settings, expected_control_settings,
        expected_source_sha256, expected_site, expected_goal, expected_return_sites, expected_duration_s):
    """Fresh ground-start v7 mission with the explicit availability-aware Booster."""
    try:
        control = _settings(expected_control_settings)
        _require(record.get("experimental_mission_ascent", {}).get("booster_terminal_recovery") == control,
            "Whole-mission outage policy is not bound to its explicit settings")
    except (saved._Invalid, KeyError, TypeError, ValueError) as exc:
        issue = exc.issue if isinstance(exc, saved._Invalid) else {"code": str(exc)}
        return {"schema": "missionos.experimental_terminal_state_physical_mission_verification.v1",
            "passed": False, "physical_success": False, "issues": [issue]}
    def check_booster(booster, separation, profile, site, goal, horizon, budget, *, availability_check):
        _booster(booster, separation, profile, site, goal, horizon, budget,
            settings=control, checkpoint_required=True, availability_check=availability_check)
    result = _verify_state_physical_mission(record, profile, expected_settings=expected_settings,
        expected_source_sha256=expected_source_sha256, expected_site=expected_site, expected_goal=expected_goal,
        expected_return_sites=expected_return_sites, expected_duration_s=expected_duration_s,
        booster_policy=POLICY, booster_verifier=check_booster, execution_binding={
            "schema": "missionos.experimental_terminal_state_physical_mission_execution.v1",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "settings_sha256": digest(expected_settings), "control_settings_sha256": digest(control)})
    result["schema"] = "missionos.experimental_terminal_state_physical_mission_verification.v1"
    return result
