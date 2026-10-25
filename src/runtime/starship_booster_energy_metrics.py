"""Saved-only checks for the opt-in energy-water-return repair.

No guidance, controller, trajectory solver or plant is executed here. Static
loads, recorded requests and contact geometry are checked separately from
physical success. This is not independent-model flight qualification.
"""
from dataclasses import replace
import math

import numpy as np

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from .starship_booster_outage_metrics import _allocation, _availability, _hash, _near
from .starship_experimental_mission_metrics import STATE_FIELDS, _require, _same, _verify_booster, digest
from .starship_state_mission_metrics import _verify_state_physical_mission

POLICY = "availability_energy_water_return_v2"
PHASES = ("booster_boostback_slew", "booster_boostback", "booster_boostback_settle", "booster_entry_coast",
          "booster_landing_align", "booster_landing_burn")
MODES = {"measured_tvc_shutdown_tail", "jet_limited_pd_low_q", "finite_fin_authority_pd",
         "finite_fins_and_measured_tvc", "powered_slew_or_braking_measured_tvc",
         "fuel_depleted_uncontrolled_coast"}


def _settings(value):
    from .starship_booster_energy_guidance import BoosterEnergyControlSettings
    keys = {"schema", "policy_id", "boostback_vertical_target_mps", "boostback_velocity_tau_s"}
    _require(type(value) is dict and set(value) in (keys, keys | {"handoff_main_count"}), "Explicit energy control settings required")
    wanted = BoosterEnergyControlSettings(value["boostback_vertical_target_mps"], value["boostback_velocity_tau_s"],
        value.get("handoff_main_count", 0)).record()
    _require(value == wanted, "Energy control settings differ from registered family")
    return wanted


def _endpoint(actual, target, tau, rate, interval):
    distance = abs(target-actual)
    saturated = max(0., (distance-tau*rate)/rate)
    move = (rate*interval if interval <= saturated else
            distance-min(distance, tau*rate)*math.exp(-(interval-saturated)/tau))
    return actual+math.copysign(move, target-actual)


def _pd(state, target, guidance, observed):
    q = state.q_body_to_eci
    error = dyn.quaternion_multiply((q[0], *(-x for x in q[1:])), target)
    if error[0] < 0:
        error = tuple(-x for x in error)
    rate = np.asarray(state.omega_body_rad_s)
    frequency, cap = guidance["attitude_frequency_rad_s"], guidance["max_angular_acceleration_rad_s2"]
    acceleration = np.clip(2*frequency**2*np.asarray(error[1:])
        -2*guidance["attitude_damping_ratio"]*frequency*rate, -cap, cap)
    inertia = np.asarray(observed["inertia_kg_m2"])
    return acceleration, inertia@acceleration+np.cross(rate, inertia@rate)


def _finite_authority(receipt, state, craft, profile, observed, target, interval):
    _require(type(receipt) is dict and receipt.get("schema") == "missionos.booster_finite_coast_authority.v1"
        and receipt.get("policy_id") == "actual_pd_finite_endpoint_v1"
        and receipt.get("time_s") == state.time_s and receipt.get("control_interval_s") == interval,
        "Missing or stale finite coast authority witness")
    for key in ("prediction_is_execution", "actual_state_assigned", "physics_limits_changed"):
        _require(receipt.get(key) is False, "Coast feasibility was presented as execution or changed physics")
    actuator = {"engines": [[bool(e.available), float(e.throttle), float(e.gimbal_x_rad), float(e.gimbal_y_rad)]
        for e in state.engine_states], "fins": [float(x) for x in state.flap_angles_rad]}
    _require(receipt.get("source_actuator_sha256") == digest(actuator), "Coast authority lost actual actuator identity")
    nominal, lower, upper = np.zeros((3, 2)), np.zeros(3), np.zeros(3)
    paired = True
    for axis in range(3):
        for sign in (-1, 1):
            indices = [i for i, e in enumerate(craft.engines) if e.name.startswith(f"rcs_{axis}_{sign}_")]
            _require(len(indices) == 2 and all(state.engine_states[i].available for i in indices)
                and state.propellant_kg > 0, "Authority borrowed missing signed jet hardware")
            torques, endpoints = [], []
            for i in indices:
                engine, actual = craft.engines[i], state.engine_states[i]
                force = np.asarray(engine.direction_body)*engine.max_thrust_n
                moment = np.cross(np.asarray(engine.position_body_m)-observed["com_body_m"], force)
                torques.append(moment[axis])
                endpoints.append([_endpoint(actual.throttle, target, engine.throttle_time_constant_s,
                    engine.throttle_rate_per_s, interval) for target in (0., 1.)])
            nominal[axis, int(sign > 0)] = abs(sum(torques))
            if not np.allclose(endpoints[0], endpoints[1], rtol=0, atol=1e-12):
                paired = False
                continue
            minimum, maximum = sum(t*x[0] for t, x in zip(torques, endpoints)), sum(t*x[1] for t, x in zip(torques, endpoints))
            lower[axis] += min(minimum, maximum)
            upper[axis] += max(minimum, maximum)
    _near(receipt.get("available_signed_jet_torque_nm"), nominal, "Signed jet hardware capacity differs")
    _near(receipt.get("finite_jet_lower_torque_nm"), lower, "Finite negative jet endpoint differs")
    _near(receipt.get("finite_jet_upper_torque_nm"), upper, "Finite positive jet endpoint differs")
    _require(receipt.get("finite_jet_pair_scope_valid") is paired, "Force-cancelling jet pair assumption differs")
    tvc = 0.
    main_moment = np.zeros(3)
    for engine, actual, load in zip(craft.engines, state.engine_states, observed["engine_loads"]):
        if not engine.name.startswith("rcs_"):
            main_moment += load["torque_body_nm"]
            if actual.available and engine.max_gimbal_rad > 0:
                tvc += load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])*math.sin(engine.max_gimbal_rad)
    ceiling = min(.03, profile["guidance"]["max_angular_acceleration_rad_s2"])
    _near(receipt["configured_angular_acceleration_ceiling_rad_s2"], ceiling, "Configured coast ceiling differs", 1e-12)
    _near(receipt["dynamic_pressure_pa"], observed["dynamic_pressure_pa"], "Coast pressure witness differs")
    _near(receipt["current_main_torque_body_nm"], main_moment, "Coast current main moment differs")
    _near(receipt["current_gimballed_main_torque_capacity_nm"], tvc, "Coast actual TVC capacity differs")
    baseline = min(ceiling, (float(np.min(nominal))+tvc)/max(sum(abs(x) for x in row) for row in observed["inertia_kg_m2"]))
    cap, frequency = receipt["coast_angular_acceleration_limit_rad_s2"], receipt["coast_control_frequency_rad_s"]
    _require(saved._number(cap) and baseline <= cap <= ceiling and type(receipt.get("candidate_count")) is int
        and 0 <= receipt["candidate_count"] <= 7, "Coast authority exceeds unchanged bounds")
    _near(receipt["baseline_angular_acceleration_limit_rad_s2"], baseline, "Coast baseline differs", 1e-12)
    _near(frequency, min(profile["guidance"]["attitude_frequency_rad_s"], math.sqrt(cap/math.pi)), "Coast frequency differs", 1e-12)
    g = {**profile["guidance"], "attitude_frequency_rad_s": frequency, "max_angular_acceleration_rad_s2": cap}
    if receipt.get("additional_fin_authority_admitted") is False:
        _near(cap, baseline, "Unadmitted finite fin authority increased control bandwidth", 1e-12)
    else:
        _require(receipt.get("additional_fin_authority_admitted") is True and observed["dynamic_pressure_pa"] > 100.
            and tvc <= float(np.min(nominal)) and paired and 1 <= receipt["candidate_count"] <= 7,
            "Finite fin authority escaped its weak-tail, high-pressure scope")
        acceleration, rigid = _pd(state, target, g, observed)
        _near(receipt["requested_acceleration_body_rad_s2"], acceleration, "Authority PD demand differs", 1e-12)
        _near(receipt["requested_rigid_torque_body_nm"], rigid, "Authority rigid torque differs")
        indices = [i for i, p in enumerate(craft.aero_panels) if p.name.startswith("grid_fin_")]
        _require(receipt["fin_indices"] == indices, "Authority borrowed different fins")
        endpoints = receipt["predicted_fin_endpoint_angles_rad"]
        _require(len(endpoints) == len(indices), "Authority endpoint cardinality differs")
        angles = list(state.flap_angles_rad)
        for i, endpoint in zip(indices, endpoints):
            panel, actual = craft.aero_panels[i], state.flap_angles_rad[i]
            low = _endpoint(actual, -panel.max_deflection_rad, panel.deflection_time_constant_s, panel.deflection_rate_rad_s, interval)
            high = _endpoint(actual, panel.max_deflection_rad, panel.deflection_time_constant_s, panel.deflection_rate_rad_s, interval)
            _require(saved._number(endpoint) and low-1e-12 <= endpoint <= high+1e-12, "Authority fin endpoint exceeds finite capability")
            angles[i] = endpoint
        moment = np.asarray(dyn.observe(replace(state, flap_angles_rad=tuple(angles)), craft)["aero_torque_body_nm"])
        residual = rigid-moment-main_moment
        _near(receipt["predicted_endpoint_aero_torque_body_nm"], moment, "Authority nonlinear endpoint moment differs")
        _near(receipt["predicted_jet_residual_torque_body_nm"], residual, "Authority residual differs")
        _require(np.all(residual >= lower-1e-7) and np.all(residual <= upper+1e-7), "Authority residual exceeds finite signed jet endpoints")
    return g


def _guidance_facts(row, state, craft, profile, observed, settings, separation, site, goal):
    """Check saved input facts and selected equations without running guidance."""
    from .starship_sixdof_contact import hull_clearance
    evidence = row["controller"]["energy_guidance"]
    actual, request = evidence["observation"], evidence["request"]
    before, after = evidence["memory_before"], evidence["memory_after"]
    _require(before["phase"] in PHASES and after["phase"] == row["phase"]
        and PHASES.index(before["phase"]) <= PHASES.index(after["phase"])
        and before["origin_time_s"] == after["origin_time_s"] == separation["time_s"]
        and after["last_time_s"] == state.time_s
        and (before["last_time_s"] is None or before["last_time_s"] <= state.time_s),
        "Guidance memory lost original separation, current time or monotone phase")
    for memory in (before, after):
        for field in ("boostback_started_s", "last_time_s", "handoff_started_s"):
            value = memory[field]
            _require(value is None or saved._number(value) and separation["time_s"] <= value <= state.time_s,
                "Guidance memory contains a future or pre-origin timestamp")
        _require(type(memory["aerobraking_started"]) is bool, "Aerobraking memory is not boolean")
    _require(before["boostback_started_s"] is None or after["boostback_started_s"] == before["boostback_started_s"],
        "Boostback ignition time was rewritten")
    if before["boostback_started_s"] is None and after["boostback_started_s"] is not None:
        _require(before["phase"] in PHASES[:2] and after["phase"] == PHASES[1]
            and after["boostback_started_s"] == state.time_s, "Boostback ignition time lacks a current transition")
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    up, east, north = env.local_frame(point)
    surface = env.surface_state(site["latitude_deg"], site["longitude_deg"], site["elevation_m"], time_s=state.time_s)
    difference = env.add(surface.r, env.scale(state.r_eci_m, -1.))
    displacement = env.add(difference, env.scale(up, -env.dot(difference, up)))
    basis = (east, north, up)
    velocity = tuple(env.dot(env.air_relative_velocity(point), axis) for axis in basis)
    position = (-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"])
    body = tuple(env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), axis) for axis in basis)
    alpha = (math.degrees(math.acos(max(-1., min(1., -env.dot(body, velocity)/env.norm(velocity)))))
        if env.norm(velocity) > 0 else 0.)
    clearance = hull_clearance(state, craft, profile["geometry"]["booster_length_m"],
        profile["geometry"]["radius_m"])["signed_clearance_m"]
    for key, value in (("time_s", state.time_s), ("position_enu_m", position), ("velocity_enu_mps", velocity),
        ("clearance_m", clearance), ("mass_kg", observed["mass_kg"]), ("propellant_kg", state.propellant_kg),
        ("body_axis_enu", body), ("body_rate_rad_s", env.norm(state.omega_body_rad_s)),
        ("dynamic_pressure_pa", observed["dynamic_pressure_pa"]), ("actual_alpha_from_tail_first_deg", alpha),
        ("inertia_bound_kg_m2", max(sum(abs(x) for x in r) for r in observed["inertia_kg_m2"])),
        ("engine_lever_m", abs(craft.engines[0].position_body_m[2]-observed["com_body_m"][2]))):
        _near(actual[key], value, "Guidance input differs from actual saved "+key)
    gravity = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
    _near(actual["gravity_mps2"], gravity, "Guidance gravity fact differs", 1e-10)
    mains = [{"max_thrust_n": engine.max_thrust_n, "min_throttle": engine.min_throttle,
        "available": bool(engine_state.available)} for engine, engine_state in zip(craft.engines[:33], state.engine_states[:33])]
    _require(actual["mains"] == mains, "Guidance borrowed unavailable engine or a different thrust capability")
    count, throttle, axis = request["main_prefix_count"], request["main_throttle"], request["target_axis_enu"]
    _require(type(count) is int and 0 <= count <= 33 and saved._number(throttle) and 0 <= throttle <= 1,
        "Guidance main request is outside actual bounds")
    _require(count != 0 or throttle == 0., "Zero main prefix has a nonzero throttle request")
    _near(env.norm(axis), 1., "Guidance target axis is not unit", 1e-10)
    target = tuple(sum(axis[i]*basis[i][j] for i in range(3)) for j in range(3))
    q = row["controller"]["target_q_body_to_eci"]
    _near(dyn.rotate(q, (0., 0., 1.)), target, "Guidance target differs from actual attitude request", 1e-10)
    x = env.add(north, env.scale(target, -env.dot(north, target)))
    if env.norm(x) < 1e-6:
        reference = tuple(float(i == min(range(3), key=lambda j: abs(target[j]))) for i in range(3))
        x = env.add(reference, env.scale(target, -env.dot(reference, target)))
    _near(dyn.rotate(q, (1., 0., 0.)), env.unit(x), "Guidance body-X reference differs from North convention", 1e-10)
    # Reconstruct the full entry attitude; body-Z alignment alone omits roll.
    handoff_count = settings.get("handoff_main_count", 0)
    statuses = {"not_requested", "pending", "completed", "incomplete_timeout", "incomplete_reserve"}
    for memory in (before, after):
        _require(memory["handoff_status"] in statuses, "Unknown powered handoff status")
        _require((memory["handoff_status"] == "not_requested") is (memory["handoff_started_s"] is None),
            "Powered handoff status and start time disagree")
        if not handoff_count:
            _require(memory["handoff_status"] == "not_requested", "Legacy settings borrowed powered handoff history")
    if before["handoff_started_s"] is not None:
        _require(after["handoff_started_s"] == before["handoff_started_s"], "Powered handoff start was rewritten")
    if before["handoff_status"] in statuses-{"not_requested", "pending"}:
        _require(after["handoff_status"] == before["handoff_status"], "Finished handoff status was rewritten")
    if handoff_count:
        entry_axis = evidence["entry"]["axis_enu"]
        entry_z = tuple(sum(entry_axis[i]*basis[i][j] for i in range(3)) for j in range(3))
        entry_x = env.add(north, env.scale(entry_z, -env.dot(north, entry_z)))
        if env.norm(entry_x) < 1e-6:
            reference = tuple(float(i == min(range(3), key=lambda j: abs(entry_z[j]))) for i in range(3))
            entry_x = env.add(reference, env.scale(entry_z, -env.dot(reference, entry_z)))
        entry_x = env.unit(entry_x)
        entry_y = env.cross(entry_z, entry_x)
        trace = sum(env.dot(dyn.rotate(state.q_body_to_eci, axis), wanted)
            for axis, wanted in zip(((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)), (entry_x, entry_y, entry_z)))
        entry_cos = max(-1., min(1., (trace-1.)/2.))
        entry_error = evidence["entry"]["actual_attitude_error_deg"]
        _require(saved._number(entry_error) and 0 <= entry_error <= 180., "Invalid full handoff attitude error")
        # Comparing the cosine avoids amplifying roundoff through acos near zero.
        _near(math.cos(math.radians(entry_error)), entry_cos, "Handoff full attitude differs", 2e-12)
        new_start = before["handoff_started_s"] is None and after["handoff_started_s"] is not None
        if new_start:
            _require(before["phase"] in PHASES[:2] and after["handoff_started_s"] == state.time_s,
                "Handoff start lacks an actual boostback transition")
        processed = before["phase"] == "booster_boostback_settle" or new_start
        if processed:
            info = evidence["diagnostics"]
            elapsed = state.time_s-after["handoff_started_s"]
            _near(info["handoff_full_attitude_error_deg"], entry_error, "Handoff diagnostic hid full attitude error", 1e-6)
            _near(info["handoff_elapsed_s"], elapsed, "Handoff elapsed time differs", 1e-10)
            _near(info["handoff_actual_body_rate_rad_s"], env.norm(state.omega_body_rad_s), "Handoff body rate differs")
            reserve_guard, time_guard = state.propellant_kg <= 60000., elapsed >= 30.
            completed = entry_cos >= math.cos(math.radians(5.)) and env.norm(state.omega_body_rad_s) <= .003
            wanted_status = ("incomplete_reserve" if reserve_guard else "incomplete_timeout" if time_guard
                else "completed" if completed else "pending")
            _require(after["handoff_status"] == wanted_status, "Handoff completion or guard status differs from actual facts")
            _require(info["handoff_completed"] is (wanted_status == "completed"), "Handoff completion diagnostic differs")
            wanted_guard = "handoff_reserve_guard" if reserve_guard else "handoff_time_guard" if time_guard else None
            _require(info["handoff_guard_reason"] == wanted_guard, "Handoff guard reason differs")
            if wanted_status != "pending":
                _require(after["phase"] == "booster_entry_coast" and count == 0 and throttle == 0.,
                    "Handoff completion skipped actual all-main shutdown coast")
            else:
                _require(after["phase"] == "booster_boostback_settle", "Pending handoff escaped powered phase")
    command = dyn.command_from_dict(row["command"])
    for i, engine in enumerate(command.engines[:33]):
        _require(engine.enabled is (i < count and throttle > 0), "Actual main enable prefix differs from guidance")
        _near(engine.throttle, throttle if i < count else 0., "Actual main throttle differs from guidance", 1e-12)
    preview = evidence["preview"]
    coast_processed = before["phase"] in {"booster_boostback_slew", "booster_boostback",
        "booster_boostback_settle", "booster_entry_coast"} and after["phase"] in {
            "booster_entry_coast", "booster_landing_align", "booster_landing_burn"}
    detected = (coast_processed and velocity[2] < 0 and observed["dynamic_pressure_pa"] >= 500.
        and alpha >= 5. and evidence["entry"]["angle_deg"] >= 10.)
    _require(after["aerobraking_started"] is (before["aerobraking_started"] or bool(detected)),
        "Aerobraking memory was fabricated or reset")
    admitted = (preview["complete_arrest"] is True and preview["termination"] == "arrested"
        and preview["stopping_distance_m"] >= 0 and preview["minimum_altitude_m"] >= 0
        and preview["remaining_propellant_kg"] >= goal["minimum_propellant_kg"])
    if before["phase"] != after["phase"] and after["phase"] in {"booster_landing_align", "booster_landing_burn"}:
        _require(admitted and after["aerobraking_started"] is True,
            "Landing transition used invalid stopping travel, reserve or aerobraking")
        if before["phase"] in {"booster_boostback_slew", "booster_boostback", "booster_boostback_settle", "booster_entry_coast"}:
            _require(velocity[2] < 0, "Coast alignment was entered without descending")
    if after["phase"] == "booster_entry_coast":
        _require(count == 0 and throttle == 0., "Entry coast requested an unregistered main restart")
        _near(axis, env.unit(evidence["entry"]["axis_enu"]), "Coast request differs from selected entry axis", 1e-10)
    if after["phase"] == "booster_boostback_settle":
        _require(settings.get("handoff_main_count", 0) > 0 and count == settings["handoff_main_count"],
            "Powered handoff borrowed an unregistered prefix")
        _near(throttle, max(.4, *(e["min_throttle"] for e in mains[:count])), "Powered handoff throttle differs", 1e-12)
        _near(axis, env.unit(evidence["entry"]["axis_enu"]), "Powered handoff lost predicted entry target", 1e-10)
    if after["phase"] == "booster_landing_align":
        _near(axis, (0., 0., 1.), "Landing alignment target is not engine-first", 1e-10)
        active = math.acos(max(-1., min(1., body[2]))) > math.radians(5.) or env.norm(state.omega_body_rad_s) > .02
        wanted_count = 3 if active else 0
        wanted_throttle = max(.4, *(e["min_throttle"] for e in mains[:3])) if active else 0.
        _require(count == wanted_count, "Alignment main prefix differs")
        _near(throttle, wanted_throttle, "Alignment throttle differs", 1e-12)
    if after["phase"] == "booster_landing_burn":
        if before["phase"] != after["phase"]:
            _require(math.acos(max(-1., min(1., body[2]))) <= math.radians(5.)
                and env.norm(state.omega_body_rad_s) <= .02, "Aligned preview was used before actual alignment")
        down = max(0., -velocity[2])
        effective = max(.1, clearance-down*(profile["actuators"]["throttle_tau_s"]+profile["integration"]["powered_dt_s"]))
        upward = gravity+max(0., (down*down-1.5**2)/(2*effective))
        if velocity[2] >= -1.5:
            upward += (-1.5-velocity[2])/1.5
        upward = max(0., upward)
        actuators = profile["actuators"]
        delay = 3*actuators["throttle_tau_s"]+1/actuators["throttle_rate_s"]+3*actuators["gimbal_tau_s"]+actuators["max_gimbal_deg"]/actuators["gimbal_rate_deg_s"]
        footprint = tuple(position[i]+velocity[i]*(8.+delay) for i in range(2))
        inside = math.hypot(*position[:2]) <= goal["area_radius_m"] and math.hypot(*footprint) <= goal["area_radius_m"]
        horizontal = tuple(-velocity[i]/8. if inside else -position[i]/64.-2*velocity[i]/8. for i in range(2))
        factor = min(1., upward*math.tan(math.radians(15.))/max(math.hypot(*horizontal), 1e-30))
        force = (*tuple(x*factor for x in horizontal), upward)
        info = evidence["diagnostics"]
        _require(info["area_velocity_damping"] is inside, "Terminal control chased centre inside admitted braking footprint")
        _near(info["braking_footprint_enu_m"], footprint, "Terminal footprint differs")
        _near(info["upward_thrust_acceleration_mps2"], upward, "Terminal signed vertical law differs")
        if env.norm(force) <= 1e-12:
            _require(count == 0 and throttle == 0., "Nonpositive thrust request restarted a main engine")
        else:
            _near(axis, env.unit(force), "Terminal target differs from bounded acceleration")
            alignment = env.dot(body, axis)
            if alignment > .5:
                _prefix_matches(count, throttle, observed["mass_kg"]*env.norm(force)/alignment, mains, 13)
            else:
                _require(count == 3, "Misaligned terminal slew used a different engine prefix")
                _near(throttle, max(.4, *(e["min_throttle"] for e in mains[:3])), "Terminal slew throttle differs", 1e-12)
    if before["phase"] in PHASES[:2]:
        vz = settings["boostback_vertical_target_mps"]
        fall = max(30., min(400., (vz+math.sqrt(vz*vz+2*gravity*max(0., position[2])))/gravity))
        horizontal = tuple(-x/fall for x in position[:2])
        factor = min(1., 700./max(math.hypot(*horizontal), 1e-30))
        wanted = (*tuple(x*factor for x in horizontal), vz)
        _near(evidence["diagnostics"]["boostback_target_velocity_enu_mps"], wanted,
            "Boostback target froze old displacement instead of actual current position")
        if after["phase"] in PHASES[:2]:
            error = tuple(wanted[i]-velocity[i] for i in range(3))
            acceleration = tuple(error[i]/settings["boostback_velocity_tau_s"]+(gravity if i == 2 else 0.) for i in range(3))
            _near(axis, env.unit(acceleration), "Boostback target differs from current velocity error")
            if env.dot(body, axis) >= math.cos(math.radians(20.)):
                _prefix_matches(count, throttle, observed["mass_kg"]*env.norm(acceleration), mains, 33)
            else:
                _require(count == 3, "Boostback slew used a different engine prefix")
                _near(throttle, max(.4, *(e["min_throttle"] for e in mains[:3])), "Boostback slew throttle differs", 1e-12)


def _prefix_matches(count, throttle, force, mains, maximum):
    supply = 0.
    expected = 0
    if force > 0:
        for expected in range(1, maximum+1):
            if mains[expected-1]["available"]:
                supply += mains[expected-1]["max_thrust_n"]
            if supply >= force:
                break
    wanted = (max(max(e["min_throttle"] for e in mains[:expected] if e["available"]),
        min(1., force/supply)) if supply > 0 else 0.)
    _require(count == expected, "Main prefix used unavailable capacity or compacted engine identities")
    _near(throttle, wanted, "Main throttle differs from available physical prefix", 1e-12)


def _command_witness(row, craft, profile, separation, settings, site, goal, horizon=None):
    _availability(row, separation)
    _require(row["phase"] in PHASES, "Unknown energy-control phase")
    state = dyn.state_from_dict({key: row[key] for key in STATE_FIELDS})
    command = dyn.command_from_dict(row["command"])
    dyn._validate_pair(state, craft, command)
    diagnostic = row["controller"]
    meta = diagnostic["booster_energy_control"]
    if meta.get("mode") == "fuel_depleted_uncontrolled_coast":
        _require(state.propellant_kg == 0. and meta.get("policy_id") == POLICY and meta.get("settings") == settings
            and meta.get("actual_state_assigned") is False and meta.get("physics_limits_changed") is False
            and meta.get("actual_uncontrolled") is True
            and all(e.enabled is False and e.throttle == e.gimbal_x_rad == e.gimbal_y_rad == 0. for e in command.engines)
            and command.flap_angles_rad == state.flap_angles_rad
            and diagnostic.get("finite_coast_authority") is None,
            "Uncontrolled fuel-depleted coast borrowed fuel, torque or actuation")
        return meta["mode"]
    observed, capacity, jets, tail = _allocation(state, command, diagnostic, craft, profile, row["phase"])
    _guidance_facts(row, state, craft, profile, observed, settings, separation, site, goal)
    _require(meta.get("policy_id") == POLICY and meta.get("settings") == settings
        and meta.get("mode") in MODES and meta.get("measured_finite_main_loads") is True
        and meta.get("physics_limits_changed") is False and meta.get("actual_state_assigned") is False
        and meta.get("recenter_uncommanded_main") is (not tail), "Energy allocation scope differs")
    _near(meta["current_gimballed_main_torque_capacity_nm"], capacity, "Energy allocation TVC capacity differs")
    _near(meta["physical_rcs_pair_torque_nm"], jets, "Energy allocation jet capacity differs")
    _require(meta["shutdown_tail_tvc_required"] is tail, "Shutdown tail allocation differs")
    remaining = math.inf if horizon is None else horizon-state.time_s
    _require(remaining > 0, "Control request is not before its physical horizon")
    interval = min(.25, remaining, profile["integration"]["powered_dt_s"] if any(e.enabled and e.throttle > 0
        for engine, e in zip(craft.engines, command.engines) if not engine.name.startswith("rcs_"))
        or observed["altitude_m"] < 100000. else profile["integration"]["coast_dt_s"])
    g = profile["guidance"]
    if row["phase"] == "booster_entry_coast":
        g = _finite_authority(diagnostic["finite_coast_authority"], state, craft, profile, observed,
            diagnostic["target_q_body_to_eci"], interval)
        wanted_mode = ("measured_tvc_shutdown_tail" if tail else "jet_limited_pd_low_q"
            if observed["dynamic_pressure_pa"] <= 100. else "finite_fin_authority_pd"
            if diagnostic["finite_coast_authority"]["additional_fin_authority_admitted"] else "finite_fins_and_measured_tvc")
    else:
        wanted_mode = "powered_slew_or_braking_measured_tvc"
        _require(diagnostic.get("finite_coast_authority") is None, "Powered phase borrowed coast-only authority")
    _require(meta["mode"] == wanted_mode, "Energy control mode differs from actual phase and physical authority")
    _, rigid = _pd(state, diagnostic["target_q_body_to_eci"], g, observed)
    demand = rigid-np.asarray(observed["aero_torque_body_nm"])
    fin = diagnostic.get("development_fin_allocation")
    if row["phase"] in {"booster_boostback_settle", "booster_entry_coast", "booster_landing_align",
            "booster_landing_burn"} and observed["dynamic_pressure_pa"] > 50.:
        _require(type(fin) is dict, "Missing actual finite-fin allocation witness")
    if fin is None:
        _near(diagnostic["requested_torque_body_nm"], demand, "Energy PD demand differs from actual state")
    else:
        _require(fin.get("schema") == "missionos.starship_fin_allocation.v1"
            and fin.get("policy_id") == "finite_regularized_fins_v1" and fin.get("actual_state_assigned") is False
            and fin.get("production_policy_admitted") is False and fin.get("interval_s") == interval,
            "Finite-fin policy or actual interval differs")
        indices = [i for i, p in enumerate(craft.aero_panels) if p.name.startswith("grid_fin_")]
        _require(fin["fin_indices"] == indices, "Energy controller borrowed another fin inventory")
        _near(fin["requested_increment_torque_body_nm"], demand, "Energy finite-fin demand differs")
        angles = list(state.flap_angles_rad)
        for j, i in enumerate(indices):
            panel = craft.aero_panels[i]
            _near(fin["actual_angles_rad"][j], state.flap_angles_rad[i], "Finite fin actual angle differs", 1e-12)
            _near(fin["command_angles_rad"][j], command.flap_angles_rad[i], "Finite fin command differs", 1e-12)
            _require(abs(command.flap_angles_rad[i]) <= panel.max_deflection_rad+1e-10, "Fin command exceeds hardware")
            endpoint = _endpoint(state.flap_angles_rad[i], command.flap_angles_rad[i],
                panel.deflection_time_constant_s, panel.deflection_rate_rad_s, interval)
            _near(fin["predicted_endpoint_angles_rad"][j], endpoint, "Fin endpoint differs from finite request", 1e-10)
            angles[i] = endpoint
        moment = dyn.observe(replace(state, flap_angles_rad=tuple(angles)), craft)["aero_torque_body_nm"]
        residual = demand-(np.asarray(moment)-observed["aero_torque_body_nm"])
        _near(fin["predicted_residual_torque_body_nm"], residual, "Finite-fin nonlinear residual differs")
        _near(diagnostic["requested_torque_body_nm"], residual, "Finite-fin residual differs from measured-TVC input")
    return meta["mode"]


def _metadata(record, profile, separation, settings, site, goal, horizon):
    from .starship_sixdof_mission import vehicle
    metadata = record.get("booster_energy_control")
    _require(type(metadata) is dict and metadata.get("schema") == "missionos.booster_energy_control.v1"
        and metadata.get("policy_id") == POLICY and metadata.get("settings") == settings,
        "Missing or unbound energy-control metadata")
    for key, wanted in (("availability_preserved", True), ("physics_limits_changed", False),
        ("actual_state_assigned", False), ("mission_authority_granted", False), ("qualification_refreshed", False)):
        _require(metadata.get(key) is wanted, "Energy-control scope or physical limits changed")
    for key in ("nested_sixdof_forecasts", "hosted_model_calls"):
        _require(type(metadata.get(key)) is int and metadata[key] == 0, "Physical repair borrowed forecasts or models")
    _require(not any(k in record for k in ("booster_outage_control", "booster_outage_execution")), "Energy repair borrowed v1 execution")
    craft = vehicle(profile, "booster")
    samples = [r for r in record["samples"] if r.get("command") is not None]
    _require(samples, "Missing energy-control samples")
    modes, previous = {}, -1
    control_horizon = min(horizon, separation["time_s"]+record["guidance_configuration"]["max_duration_s"])
    for row in samples:
        phase = PHASES.index(row["phase"])
        _require(phase >= previous, "Energy-control phase regressed")
        previous = phase
        mode = _command_witness(row, craft, profile, separation, settings, site, goal, control_horizon)
        modes.setdefault((row["phase"], mode), row)
    witnesses = metadata.get("control_witnesses")
    _require(type(witnesses) is list and 0 < len(witnesses) <= len(PHASES)*len(MODES), "Missing or unbounded energy witnesses")
    seen = set()
    for witness in witnesses:
        key = (witness.get("phase"), witness.get("mode"))
        _require(key in modes and key not in seen and witness.get("time_s") == witness["state"]["time_s"], "Duplicate or unsupported energy witness")
        seen.add(key)
        first = modes[key]
        _require(witness["time_s"] == first["time_s"] and _same(first, witness["state"])
            and witness.get("command") == first["command"] and witness.get("controller") == first["controller"], "Energy first witness differs from actual sample")
    _require(seen == set(modes), "Energy control mode has no first witness")
    return len(samples), len(witnesses)


def _booster(record, separation, profile, site, goal, horizon, budget, *, settings, checkpoint_required, availability_check=None):
    _require(not checkpoint_required or "booster_energy_execution" not in record, "Whole launch borrowed saved-separation execution")
    _verify_booster(record, separation, profile, site, goal, horizon, budget,
        availability_check=lambda sample, kind: _availability(sample, separation)
            and (availability_check is None or availability_check(sample, kind)),
        expected_guidance_policy=POLICY, checkpoint_required=checkpoint_required)
    return _metadata(record, profile, separation, settings, site, goal, horizon)


def verify_energy_booster_record(record, profile, *, expected_separation_state, expected_source_sha256,
        expected_source_record_sha256, expected_site, expected_goal, expected_duration_s, expected_settings):
    result = {"schema": "missionos.booster_energy_repair_verification.v2", "passed": False, "issues": [],
        "physical_success": False, "water_entry_accepted": False, "physical_reexecution": False, "model_calls": 0,
        "ground_launch_verified": False, "qualification_refreshed": False, "physical_execution": False,
        "scope": "saved request/current-load/contact consistency; no independent flight model"}
    try:
        settings = _settings(expected_settings)
        _require(_hash(expected_source_sha256) and _hash(expected_source_record_sha256)
            and type(expected_duration_s) in (int, float) and math.isfinite(expected_duration_s)
            and 0 < expected_duration_s <= 1200., "Explicit source identity and suffix horizon required")
        saved._json_tree(record)
        saved._state(expected_separation_state, "$.expected_separation_state")
        _require(record.get("booster_energy_execution") == {"schema": "missionos.booster_energy_repair_execution.v2",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "separation_sha256": digest(expected_separation_state), "settings_sha256": digest(settings),
            "source_record_sha256": expected_source_record_sha256}, "Energy suffix source/profile/separation/settings binding differs")
        horizon = expected_separation_state["time_s"]+expected_duration_s
        samples, witnesses = _booster(record, expected_separation_state, profile, expected_site, expected_goal,
            horizon, [0], settings=settings, checkpoint_required=False)
        from .starship_splashdown_verifier import verify_splashdown
        water = verify_splashdown(record, expected_goal)
        _require(water["passed"], "Independent saved water-contact geometry rejected")
        final = record["final_state"]
        _require(expected_separation_state["time_s"] <= final["time_s"] <= horizon, "Energy suffix exceeded elapsed horizon")
        result.update(passed=True, physical_success=water["controlled_water_entry_envelope_met"],
            water_entry_accepted=water["controlled_water_entry_envelope_met"], contact=record.get("contact") is not None,
            termination=record["outcome"]["termination"], final_propellant_kg=final["propellant_kg"],
            end_time_s=final["time_s"], verified_command_samples=samples, verified_control_witnesses=witnesses, water=water)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError, StopIteration) as exc:
        result["issues"].append({"code": "invalid_booster_energy_record", "detail": str(exc)})
    return result


def verify_energy_state_physical_mission(record, profile, *, expected_settings, expected_control_settings,
        expected_source_sha256, expected_site, expected_goal, expected_return_sites, expected_duration_s):
    try:
        control = _settings(expected_control_settings)
        _require(record.get("experimental_mission_ascent", {}).get("booster_energy_control") == control,
            "Whole mission is not bound to explicit energy-control settings")
    except (saved._Invalid, KeyError, TypeError, ValueError) as exc:
        issue = exc.issue if isinstance(exc, saved._Invalid) else {"code": str(exc)}
        return {"schema": "missionos.experimental_energy_state_physical_mission_verification.v2",
            "passed": False, "physical_success": False, "issues": [issue]}
    def check_booster(booster, separation, profile, site, goal, horizon, budget, *, availability_check):
        _booster(booster, separation, profile, site, goal, horizon, budget,
            settings=control, checkpoint_required=True, availability_check=availability_check)
    result = _verify_state_physical_mission(record, profile, expected_settings=expected_settings,
        expected_source_sha256=expected_source_sha256, expected_site=expected_site, expected_goal=expected_goal,
        expected_return_sites=expected_return_sites, expected_duration_s=expected_duration_s,
        booster_policy=POLICY, booster_verifier=check_booster, execution_binding={
            "schema": "missionos.experimental_energy_state_physical_mission_execution.v2",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "settings_sha256": digest(expected_settings), "control_settings_sha256": digest(control)})
    result["schema"] = "missionos.experimental_energy_state_physical_mission_verification.v2"
    return result
