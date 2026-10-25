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

POLICY = "availability_water_return_v1"
MODES = {"jet_stopping_distance_low_q", "finite_fins_and_measured_tvc",
         "powered_slew_or_braking_measured_tvc", "measured_tvc_shutdown_tail"}
PHASES = {"booster_boostback_slew", "booster_boostback", "booster_entry_coast", "booster_landing_burn"}


def _settings(value):
    from .starship_booster_outage_control import BoosterOutageControlSettings
    wanted = BoosterOutageControlSettings().record()
    _require(type(value) is dict and value == wanted, "Explicit frozen availability-aware control settings required")
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


def _allocation(state, command, diagnostic, craft, profile, phase):
    """Recompute achieved-current-load Jacobian/residual, never the controller."""
    observed = dyn.observe(state, craft)
    info = diagnostic.get("measured_tvc_allocation")
    gimbal_capacity = sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])*math.sin(engine.max_gimbal_rad)
        for engine, load in zip(craft.engines, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and engine.max_gimbal_rad > 0.)
    jet_capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
    tail = phase == "booster_entry_coast" and gimbal_capacity > jet_capacity
    requested_main = any(request.enabled and request.throttle > 0 for engine, request in zip(craft.engines, command.engines)
        if not engine.name.startswith("rcs_"))
    recenter = not requested_main and not tail
    _require(type(info) is dict and info.get("policy_id") == ("measured_tvc_v1" if tail else "measured_tvc_coast_recenter_v2")
        and info.get("main_throttle_commands_preserved") is True, "Missing actual-thrust allocation witness")
    if tail:
        _require(not requested_main and all(key not in info for key in
            ("coast_recenter_requested", "gimbal_allocation_applied", "finite_actuator_tracking_required")),
            "Strong shutdown tail lost TVC or commanded a main restart")
    else:
        _require(info.get("finite_actuator_tracking_required") is True
            and info.get("coast_recenter_requested") is recenter, "Coast recenter claim differs from actual main requests")
    columns, channels, moment = [], [], np.zeros(3)
    thrust = 0.
    for index, (engine, actual, load) in enumerate(zip(craft.engines, state.engine_states, observed["engine_loads"])):
        if engine.name.startswith("rcs_"):
            continue
        moment += load["torque_body_nm"]
        force = load["thrust_n"]
        thrust += force
        if recenter or not actual.available or force <= 0 or engine.max_gimbal_rad <= 0:
            continue
        _require(engine.direction_body == (0., 0., 1.), "Unregistered main thrust direction")
        gx, gy = actual.gimbal_x_rad, actual.gimbal_y_rad
        lever = np.asarray(engine.position_body_m)-observed["com_body_m"]
        dx = np.asarray((-math.sin(gy)*math.sin(gx), -math.cos(gx), -math.cos(gy)*math.sin(gx)))
        dy = np.asarray((math.cos(gy)*math.cos(gx), 0., -math.sin(gy)*math.cos(gx)))
        columns.extend((np.cross(lever, force*dx), np.cross(lever, force*dy)))
        channels.extend(((index, "gimbal_x_rad"), (index, "gimbal_y_rad")))
    _near(info.get("main_thrust_n"), thrust, "TVC witness differs from actual available finite main loads")
    _near(info.get("current_main_torque_body_nm"), moment, "TVC witness differs from actual finite main torque")
    _require(type(info.get("gimballed_engine_count")) is int and info["gimballed_engine_count"] == len(channels)//2,
        "TVC witness used unavailable or unpowered gimbal authority")
    residual = np.asarray(diagnostic["requested_torque_body_nm"])-moment
    if recenter:
        _require(info.get("applied") is True and info.get("gimbal_allocation_applied") is False,
            "Coast recenter claims instantaneous TVC allocation")
        _require(all(request.gimbal_x_rad == request.gimbal_y_rad == 0. for engine, request in zip(craft.engines, command.engines)
            if not engine.name.startswith("rcs_")), "Uncommanded main gimbals did not request finite recentering")
    else:
        _require(info.get("applied") is bool(columns)
            and (tail or info.get("gimbal_allocation_applied") is bool(columns)),
            "TVC allocation status differs from actual authority")
        if columns:
            matrix = np.asarray(columns).T
            delta = [getattr(command.engines[i], axis)-getattr(state.engine_states[i], axis) for i, axis in channels]
            residual -= matrix@np.asarray(delta)
    if recenter or columns:
        _near(info.get("predicted_residual_torque_body_nm"), residual, "TVC residual differs from actual loads and requested finite angles")
        capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        for engine, request in zip(craft.engines, command.engines):
            if engine.name.startswith("rcs_"):
                _, axis, sign, _ = engine.name.split("_")
                demand = max(0., min(1., float(residual[int(axis)])*int(sign)/capacity))
                wanted = demand if demand > 1e-8 else 0.
                _require(request.enabled is (demand > 1e-8) and saved._near(request.throttle, wanted, 1e-10),
                    "RCS request differs from measured-TVC residual")
    return observed, gimbal_capacity, jet_capacity, tail


def _command_witness(row, craft, profile, separation):
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
    observed, capacity, jet_capacity, tail = _allocation(state, command, diagnostic, craft, profile, row["phase"])
    mode = ("measured_tvc_shutdown_tail" if tail else "jet_stopping_distance_low_q"
        if row["phase"] == "booster_entry_coast" and observed["dynamic_pressure_pa"] <= 100.
        else "finite_fins_and_measured_tvc" if row["phase"] == "booster_entry_coast" else "powered_slew_or_braking_measured_tvc")
    _require(diagnostic.get("booster_outage_control") == {"policy_id": POLICY, "mode": mode,
        "measured_finite_main_loads": True, "recenter_uncommanded_main": not tail,
        "physics_limits_changed": False, "actual_state_assigned": False,
        "current_gimballed_main_torque_capacity_nm": capacity,
        "physical_rcs_pair_torque_nm": float(jet_capacity), "shutdown_tail_tvc_required": bool(tail),
        "shutdown_tail_tvc_criterion": "actual_gimballed_main_capacity_exceeds_physical_rcs_pair"},
        "Control mode or finite-load scope differs")
    if mode == "jet_stopping_distance_low_q":
        _require(diagnostic.get("control_allocation") == "coast_stopping_distance_v1"
            and diagnostic.get("main_engine_commands_off") is True,
            "Low-pressure coast is not the explicit jet-limited slew")
        inertia = np.asarray(observed["inertia_kg_m2"])
        bound = float(np.max(np.sum(np.abs(inertia), axis=1)))
        limit = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        _require(saved._number(diagnostic.get("coast_stopping_alpha_rad_s2"))
            and 0 < diagnostic["coast_stopping_alpha_rad_s2"] <= min(profile["guidance"]["max_angular_acceleration_rad_s2"], limit/bound)+1e-12,
            "Coast slew claims more than actual jet/inertia authority")
    fin = diagnostic.get("development_fin_allocation")
    if (mode != "jet_stopping_distance_low_q" and row["phase"] in {"booster_entry_coast", "booster_landing_burn"}
            and observed["dynamic_pressure_pa"] > 50.):
        _require(type(fin) is dict, "High-pressure fin control lacks its finite allocation witness")
    demand = None
    control_g = profile["guidance"]
    if row["phase"] == "booster_entry_coast":
        torque = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        for index, load in enumerate(observed["engine_loads"][:profile["booster"]["gimbal_engine_count"]]):
            lever = abs(observed["com_body_m"][2]-profile["booster"]["engine_positions_body_m"][index][2])
            torque += load["thrust_n"]*lever*math.sin(math.radians(profile["actuators"]["max_gimbal_deg"]))
        inertia_bound = max(sum(abs(x) for x in row) for row in observed["inertia_kg_m2"])
        alpha = min(control_g["max_angular_acceleration_rad_s2"], torque/inertia_bound)
        frequency = min(control_g["attitude_frequency_rad_s"], math.sqrt(alpha/math.pi))
        _near(diagnostic.get("coast_control_frequency_rad_s"), frequency, "Coast bandwidth differs from actual authority", 1e-12)
        _near(diagnostic.get("coast_angular_acceleration_limit_rad_s2"), alpha, "Coast alpha differs from actual authority", 1e-12)
        control_g = {**control_g, "attitude_frequency_rad_s": frequency, "max_angular_acceleration_rad_s2": alpha}
    if mode != "jet_stopping_distance_low_q":
        q, w = state.q_body_to_eci, np.asarray(state.omega_body_rad_s)
        error = dyn.quaternion_multiply((q[0], *(-x for x in q[1:])), diagnostic["target_q_body_to_eci"])
        if error[0] < 0:
            error = tuple(-x for x in error)
        g = control_g
        alpha = np.clip(2*g["attitude_frequency_rad_s"]**2*np.asarray(error[1:])
            -2*g["attitude_damping_ratio"]*g["attitude_frequency_rad_s"]*w,
            -g["max_angular_acceleration_rad_s2"], g["max_angular_acceleration_rad_s2"])
        inertia = np.asarray(observed["inertia_kg_m2"])
        demand = inertia@alpha+np.cross(w, inertia@w)-observed["aero_torque_body_nm"]
        if fin is None:
            _near(diagnostic["requested_torque_body_nm"], demand, "PD torque does not follow saved actual attitude/rate and profile")
    if fin is not None:
        _require(fin.get("schema") == "missionos.starship_fin_allocation.v1"
            and fin.get("policy_id") == "finite_regularized_fins_v1"
            and fin.get("actual_state_assigned") is False and fin.get("production_policy_admitted") is False
            and saved._number(fin.get("interval_s")) and 0 < fin["interval_s"] <= .25,
            "Finite fin allocation borrowed an unregistered policy or interval")
        indices = fin["fin_indices"]
        _require(indices == [i for i, panel in enumerate(craft.aero_panels) if panel.name.startswith("grid_fin_")],
            "Finite fin inventory differs from actual Booster")
        _near(fin["actual_angles_rad"], [state.flap_angles_rad[i] for i in indices], "Finite fin witness lost actual deflections")
        _near(fin["command_angles_rad"], [command.flap_angles_rad[i] for i in indices], "Finite fin command differs from executed request")
        _near(fin["requested_increment_torque_body_nm"], demand, "Finite fin demand differs from saved actual PD demand")
        _near(fin["predicted_residual_torque_body_nm"], diagnostic["requested_torque_body_nm"], "Finite fin residual differs from measured TVC input")
        for i, actual, endpoint in zip(indices, fin["actual_angles_rad"], fin["predicted_endpoint_angles_rad"]):
            panel = craft.aero_panels[i]
            _require(abs(endpoint) <= panel.max_deflection_rad+1e-10
                and abs(endpoint-actual) <= panel.deflection_rate_rad_s*fin["interval_s"]+1e-10,
                "Finite fin prediction exceeds physical slew authority")
    return mode


def _control_metadata(record, profile, separation, settings):
    from .starship_sixdof_mission import vehicle
    metadata = record.get("booster_outage_control")
    _require(type(metadata) is dict and metadata.get("schema") == "missionos.booster_outage_control.v1"
        and metadata.get("policy_id") == POLICY and metadata.get("settings") == settings,
        "Missing or unbound availability-aware control metadata")
    for key, wanted in (("availability_preserved", True), ("physics_limits_changed", False),
        ("actual_state_assigned", False), ("mission_authority_granted", False), ("qualification_refreshed", False)):
        _require(metadata.get(key) is wanted, "Outage-control scope or physical limits changed")
    for key in ("nested_sixdof_forecasts", "hosted_model_calls"):
        _require(type(metadata.get(key)) is int and metadata[key] == 0, "Outage physical repair cannot claim model or forecast work")
    craft = vehicle(profile, "booster")
    samples = [row for row in record["samples"] if row.get("command") is not None]
    _require(samples and all(row["phase"] in PHASES for row in samples), "Missing actual control samples or unknown phase")
    modes = {}
    for row in samples:
        mode = _command_witness(row, craft, profile, separation)
        modes.setdefault((row["phase"], mode), row)
    witnesses = metadata.get("control_witnesses")
    _require(type(witnesses) is list and 0 < len(witnesses) <= 6, "Missing or unbounded first control witnesses")
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
    _require(not checkpoint_required or "booster_outage_execution" not in record,
        "Fresh whole mission cannot borrow a saved-separation suffix execution")
    _verify_booster(record, separation, profile, site, goal, horizon, budget,
        availability_check=lambda sample, _kind: _availability(sample, separation)
            and (True if availability_check is None else availability_check(sample, _kind)),
        expected_guidance_policy=POLICY, checkpoint_required=checkpoint_required)
    return _control_metadata(record, profile, separation, settings)


def verify_outage_booster_record(record, profile, *, expected_separation_state, expected_source_sha256,
        expected_source_record_sha256, expected_site, expected_goal, expected_duration_s, expected_settings):
    """Verify a saved-separation suffix; it is not a new ground launch."""
    result = {"schema": "missionos.booster_outage_repair_verification.v1", "passed": False, "issues": [],
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
        _require(record.get("booster_outage_execution") == {"schema": "missionos.booster_outage_repair_execution.v1",
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
        result["issues"].append({"code": "invalid_booster_outage_record", "detail": str(exc)})
    return result


def verify_outage_state_physical_mission(record, profile, *, expected_settings, expected_control_settings,
        expected_source_sha256, expected_site, expected_goal, expected_return_sites, expected_duration_s):
    """Fresh ground-start v7 mission with the explicit availability-aware Booster."""
    try:
        control = _settings(expected_control_settings)
        _require(record.get("experimental_mission_ascent", {}).get("booster_outage_control") == control,
            "Whole-mission outage policy is not bound to its explicit settings")
    except (saved._Invalid, KeyError, TypeError, ValueError) as exc:
        issue = exc.issue if isinstance(exc, saved._Invalid) else {"code": str(exc)}
        return {"schema": "missionos.experimental_outage_state_physical_mission_verification.v1",
            "passed": False, "physical_success": False, "issues": [issue]}
    def check_booster(booster, separation, profile, site, goal, horizon, budget, *, availability_check):
        _booster(booster, separation, profile, site, goal, horizon, budget,
            settings=control, checkpoint_required=True, availability_check=availability_check)
    result = _verify_state_physical_mission(record, profile, expected_settings=expected_settings,
        expected_source_sha256=expected_source_sha256, expected_site=expected_site, expected_goal=expected_goal,
        expected_return_sites=expected_return_sites, expected_duration_s=expected_duration_s,
        booster_policy=POLICY, booster_verifier=check_booster, execution_binding={
            "schema": "missionos.experimental_outage_state_physical_mission_execution.v1",
            "source_sha256": expected_source_sha256, "profile_sha256": digest(profile),
            "settings_sha256": digest(expected_settings), "control_settings_sha256": digest(control)})
    result["schema"] = "missionos.experimental_outage_state_physical_mission_verification.v1"
    return result
