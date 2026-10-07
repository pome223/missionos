"""Read-only diagnostics of persisted recovery states; no integrator or guidance.

Gate geometry is reconstructed by the existing independent recovery checker.
Margins and main-engine force projections are diagnostics, never dispatch gates.
"""
from __future__ import annotations

import math

from . import starship_booster_recovery_verifier as checks

GATE_UNITS = {"horizontal_position": "m", "pin_height": "m", "pin_vertical_speed": "m/s",
              "pin_horizontal_speed": "m/s", "tilt": "deg", "clocking": "deg",
              "body_rate": "rad/s", "fuel_reserve": "kg"}


def gate_margins(arrival):
    """Positive is inside each simultaneous bound; do not aggregate mixed units."""
    limits, pins = arrival["limits"], arrival["pins"]
    heights = [p["height_above_support_m"] for p in pins]
    vertical = [p["velocity_enu_mps"][2] for p in pins]
    return {
        "horizontal_position": limits["horizontal_position_m"]-math.hypot(*arrival["midpoint_enu_m"][:2]),
        "pin_height": min(min(heights)-limits["pin_clearance_min_m"], limits["pin_clearance_max_m"]-max(heights)),
        "pin_vertical_speed": min(min(vertical)-limits["pin_vertical_speed_min_mps"],
                                  limits["pin_vertical_speed_max_mps"]-max(vertical)),
        "pin_horizontal_speed": limits["pin_horizontal_speed_mps"]-max(math.hypot(*p["velocity_enu_mps"][:2]) for p in pins),
        "tilt": limits["attitude_angle_deg"]-arrival["tilt_deg"],
        "clocking": limits["attitude_angle_deg"]-arrival["clocking_error_deg"],
        "body_rate": limits["body_rate_rad_s"]-arrival["body_rate_rad_s"],
        "fuel_reserve": arrival["propellant_kg"]-limits["propellant_reserve_kg"],
    }


def main_thrust(state, profile):
    """Actual main engines only, active BODY Ry(gy)Rx(gx); no RCS/aero loads."""
    force = [0., 0., 0.]
    magnitude_sum = 0.
    if state["propellant_kg"] > 0:
        for engine in state["engine_states"][:profile["booster"]["engine_count"]]:
            if not engine["available"]:
                continue
            gx, gy = engine["gimbal_x_rad"], engine["gimbal_y_rad"]
            magnitude = profile["booster"]["engine_thrust_n"]*engine["throttle"]
            direction = [math.sin(gy)*math.cos(gx), -math.sin(gx), math.cos(gy)*math.cos(gx)]
            force = [a+magnitude*b for a, b in zip(force, direction)]
            magnitude_sum += magnitude
    _, axes = checks._tower(profile, state["time_s"])
    inertial = checks._rotate(state["q_body_to_eci"], force)
    enu = [checks._dot(inertial, a) for a in axes]
    mass = profile["booster"]["dry_mass_kg"]+state["propellant_kg"]
    return {"force_enu_n": enu, "magnitude_sum_n": magnitude_sum,
            "upward_acceleration_mps2": enu[2]/mass,
            "upward_fraction": enu[2]/magnitude_sum if magnitude_sum else None}


def state_diagnostic(state, profile, config):
    checks._state(state, profile)
    arrival = checks._arrival(state, profile, config)
    margins = gate_margins(arrival)
    if all(x >= 0 for x in margins.values()) != arrival["eligible"]:
        raise ValueError("diagnostic_gate_disagreement")
    origin, axes = checks._tower(profile, state["time_s"])
    relative = checks._sub(state["v_eci_mps"], checks._cross([0., 0., checks._ROTATION], state["r_eci_m"]))
    cg = [checks._dot(checks._sub(state["r_eci_m"], origin), a) for a in axes]
    return {"time_s": state["time_s"], "cg_position_enu_m": cg,
            "cg_velocity_enu_mps": [checks._dot(relative, a) for a in axes],
            "arrival": arrival, "margins": margins, "main_thrust": main_thrust(state, profile),
            "failed_gates": [name for name, margin in margins.items() if margin < 0]}


def ideal_stop_location(state, preview, profile):
    """Map the preview's CG displacement to a frozen tower frame, not pin arrival.

    WGS84 axes at the entry CG differ from axes at the tower. Project all three
    displacement components; never add CG-local horizontal motion to pin ENU.
    Frame rotation/curvature during the preview and future attitude are omitted.
    """
    if preview["burn_fuel_feasible"] is not True:
        return None
    x, y, z = state["r_eci_m"]
    radius_xy = math.hypot(x, y)
    lon = math.atan2(y, x)
    lat = math.atan2(z, radius_xy*(1-checks._E2))
    # Bounded ellipsoid-normal reconstruction, including the polar case.
    for _ in range(12):
        normal = checks._A/math.sqrt(1-checks._E2*math.sin(lat)**2)
        after = math.atan2(z+checks._E2*normal*math.sin(lat), radius_xy)
        if abs(after-lat) < 1e-14:
            lat = after
            break
        lat = after
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    cg_axes = [[-so, co, 0.], [-sl*co, -sl*so, cl], [cl*co, cl*so, sl]]
    components = [*preview["estimated_horizontal_displacement_m"], -preview["estimated_stopping_height_m"]]
    shift = [sum(components[j]*cg_axes[j][i] for j in range(3)) for i in range(3)]
    origin, tower_axes = checks._tower(profile, state["time_s"])
    offset = checks._sub(checks._add(state["r_eci_m"], shift), origin)
    horizontal = [checks._dot(offset, axis) for axis in tower_axes[:2]]
    return {"cg_horizontal_position_enu_m": horizontal, "horizontal_error_m": math.hypot(*horizontal),
            "scope": "ideal preview CG endpoint projected into frozen request-time tower ENU; not pin arrival or executable capture"}


def analyze_run(run, profile, config):
    """Call only after the source run passes verify_recovery and its hash binding."""
    checks._profile_and_config(profile, config)
    landing = [e for e in run["events"] if e["event"] == "landing_stage_requested"]
    if not landing or landing[0].get("requested_engine_count") != 13:
        raise ValueError("missing_landing_request_state")
    start = landing[0]["time_s"]
    # Merge exact stage event states with saved checkpoints. Do not interpolate
    # a state into the narrow height band or infer continuous gate satisfaction.
    points = {}
    for index, point in enumerate(run["recovery_record"]["checkpoints"]):
        if point["time_s"] >= start:
            points[point["time_s"]] = {"state": point["state"], "phase": point["phase"],
                "checkpoint_index": index, "stage_events": [], "command": point["command"],
                "navigation": point["navigation"]}
    for event in landing:
        t = event["time_s"]
        if event["state"]["time_s"] != t:
            raise ValueError("landing_event_clock_mismatch")
        if t in points and event["state"] != points[t]["state"]:
            raise ValueError("landing_event_state_mismatch")
        point = points.setdefault(t, {"state": event["state"], "phase": "recovery_landing_"+str(event["requested_engine_count"]),
            "checkpoint_index": None, "stage_events": [], "command": None, "navigation": {}})
        point["stage_events"].append(event["requested_engine_count"])
        if point["checkpoint_index"] is None:
            point["phase"] = "recovery_landing_"+str(event["requested_engine_count"])
    if not points or points[max(points)]["state"] != run["final_state"]:
        raise ValueError("missing_terminal_state")
    rows = []
    for t, point in sorted(points.items()):
        row = state_diagnostic(point["state"], profile, config)
        row.update(elapsed_s=t-start, phase=point["phase"], checkpoint_index=point["checkpoint_index"],
                   stage_events=point["stage_events"], attitude_error_deg=point["navigation"].get("attitude_error_deg"),
                   requested_main_engine_count=(sum(e["throttle"] > 0 for e in point["command"]["engines"][:profile["booster"]["engine_count"]])
                                                if point["command"] else None))
        rows.append(row)
    first, final = rows[0], rows[-1]
    intervals = {}
    for name in GATE_UNITS:
        failing = next((i for i, row in enumerate(rows) if row["margins"][name] < 0), None)
        intervals[name] = {"inside_at_any_saved_state": any(row["margins"][name] >= 0 for row in rows),
            "best_saved_margin": max(row["margins"][name] for row in rows),
            "first_failed_saved_time_s": rows[failing]["time_s"] if failing is not None else None,
            "previous_saved_time_s": rows[failing-1]["time_s"] if failing is not None and failing > 0 else None}
    preview = landing[0]["prediction"]
    feasible = preview["burn_fuel_feasible"] is True
    # Remaining ideal-preview fuel has its own mass-dependent support reserve.
    # At an exhausted/incomplete preview its consumed amount is not fuel needed
    # to stop. Preserve that distinction and do not fabricate a reserve surplus.
    preview_fuel = max(0., first["arrival"]["propellant_kg"]-preview["estimated_burn_propellant_kg"])
    preview_reserve = ((profile["booster"]["dry_mass_kg"]+preview_fuel)*9.81
                       /(profile["booster"]["engine_isp_s"]*checks._G0)*first["arrival"]["limits"]["reserve_horizon_s"])
    return {"landing_request_time_s": start, "landing_request": first, "terminal": final,
            "elapsed_to_terminal_s": final["elapsed_s"], "terminal_ground_speed_mps": run["outcome"]["final_ground_speed_mps"],
            "termination": run["outcome"]["termination"], "preview": preview,
            "ideal_preview_terminal_fuel_kg": preview_fuel if feasible else None,
            "ideal_preview_capture_reserve_kg": preview_reserve if feasible else None,
            "ideal_preview_fuel_surplus_kg": preview_fuel-preview_reserve if feasible else None,
            "ideal_stop_location": ideal_stop_location(landing[0]["state"], preview, profile),
            "gate_summary": intervals, "all_gates_inside_saved_count": sum(not row["failed_gates"] for row in rows),
            "minimum_saved_horizontal_error_m": min(math.hypot(*row["arrival"]["midpoint_enu_m"][:2]) for row in rows),
            "negative_main_upward_force_saved_count": sum(row["main_thrust"]["force_enu_n"][2] < 0 for row in rows),
            "rows": rows, "sampled_only": True, "dynamics_reexecuted": False, "physical_execution": False}
