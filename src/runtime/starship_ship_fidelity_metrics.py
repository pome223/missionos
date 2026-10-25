"""Observational Stage 2d checks, fixed reference masks and fit-only selection.

No function integrates dynamics or calls a provider. Relative entry anchoring
does not identify the real vehicle's orbit, epoch, datum or aerodynamic model.
Finite contact and convergence remain separate from fitting broadcast curves.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

from . import starship_ascent_metrics as common

REGISTRATION = "examples/spaceflight/starship-ship-fidelity-registration-v1.json"
STATE_FIELDS = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                "propellant_kg", "engine_states", "flap_angles_rad")
COMMANDS = {"deorbit": "deorbit_ignition_command", "deorbit_cutoff": "deorbit_cutoff_command",
            "flip": "flip_and_landing_command"}
TERMINATIONS = {"time_limit", "angular_rate_envelope_exceeded", "surface_impact", "low_speed_surface_contact"}
REFERENCE_EPOCH = 9841.
ANCHOR_ALTITUDE_M = 97700.


def _wire(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _state(value, vehicle, flags, dynamics):
    actual = dynamics.state_from_dict({key: value[key] for key in STATE_FIELDS})
    if [engine.available for engine in actual.engine_states] != flags:
        raise ValueError("inherited_ship_availability_changed")
    command = dynamics.Command6DOF(tuple(dynamics.EngineCommand() for _ in vehicle.engines), actual.flap_angles_rad)
    dynamics._validate_pair(actual, vehicle, command)
    return actual


def _same_full(first, second):
    return all(first.get(key) == second.get(key) for key in STATE_FIELDS)


def _observed_budget(stored, vehicle, flags, dynamics, profile):
    from .starship_retained_return import terminal_budget
    actual = _state(stored, vehicle, flags, dynamics)
    observed = dynamics.observe(actual, vehicle)
    # Budget inputs derive from the actual saved state and frozen vehicle. A
    # forged mass/inertia observation cannot certify its own preparation cost.
    reconstructed = {**_wire(asdict(actual)), **observed, "com_z_m": observed["com_body_m"][2]}
    return terminal_budget(reconstructed, profile)


def _witnesses(record, metadata, vehicle, flags, events, dynamics, saved, profile, settings):
    witnesses = metadata.get("command_witnesses")
    saved._require(type(witnesses) is dict and set(witnesses) == set(COMMANDS), "$", "Missing bounded command table")
    for name, event_name in COMMANDS.items():
        found, witness = events.get(event_name, []), witnesses[name]
        saved._require(len(found) <= 1 and bool(found) == (witness is not None), "$", "Command event/witness presence differs")
        if not found:
            continue
        saved._require(witness["time_s"] == witness["state"]["time_s"] == found[0]["time_s"]
                       and _same_full(witness["state"], found[0].get("state", {})), "$", "Command pose/time differs from event")
        actual = _state(witness["state"], vehicle, flags, dynamics)
        command = dynamics.command_from_dict(witness["command"])
        dynamics._validate_pair(actual, vehicle, command)
        count, throttle = witness.get("requested_main_count"), witness.get("requested_throttle")
        saved._require(type(count) is int and 0 <= count <= 3 and saved._number(throttle) and 0 <= throttle <= 1,
                       "$", "Command exceeds unchanged three sea-level engines")
        for i, target in enumerate(command.engines[:6]):
            saved._require(target.enabled is (i < count and throttle > 0)
                           and target.throttle == (throttle if i < count else 0.), "$", "Main command differs from bounded requested group")
        if name == "deorbit":
            saved._require(count == 3 and throttle == profile["guidance"]["deorbit_throttle"], "$", "Deorbit group/throttle changed")
        if name == "flip" and settings is not None:
            observed = dynamics.observe(actual, vehicle)
            saved._require(found[0].get("settings") == settings
                           and found[0].get("budget_trigger_override") is True
                           and found[0].get("descending") is True
                           and saved._near(found[0].get("actual_altitude_m"), observed["altitude_m"], 1e-5)
                           and observed["altitude_m"] < settings["flip_altitude_m"]
                           and sum(a*b for a, b in zip(actual.r_eci_m, actual.v_eci_mps)) < 0,
                           "$", "Experimental flip is not the registered descending altitude trigger")
    order = [witnesses[name]["time_s"] for name in COMMANDS if witnesses[name] is not None]
    saved._require(order == sorted(order), "$", "Return command order regressed")
    if witnesses["deorbit_cutoff"] is not None:
        saved._require(witnesses["deorbit"] is not None, "$", "Cutoff without an ignition command")
    if witnesses["flip"] is not None:
        saved._require(witnesses["deorbit_cutoff"] is not None, "$", "Flip precedes deorbit cutoff")
    loads = metadata.get("first_load_witnesses")
    saved._require(type(loads) is dict and set(loads) == {"deorbit", "landing"}, "$", "Missing observed-load table")
    for name, witness in loads.items():
        request = witnesses["deorbit" if name == "deorbit" else "flip"]
        if witness is None:
            if request is not None:
                for sample in record["samples"]:
                    if sample["time_s"] >= request["time_s"]:
                        observation = dynamics.observe(_state(sample, vehicle, flags, dynamics), vehicle)
                        saved._require(not any(row["thrust_n"] > 1000. for row in observation["engine_loads"][:3]),
                                       "$", "Missing load witness contradicts a recorded post-request load")
            continue
        saved._require(request is not None and request["time_s"] <= witness["time_s"] == witness["state"]["time_s"] <= record["final_state"]["time_s"],
                       "$", "Observed load precedes its request or follows termination")
        actual = _state(witness["state"], vehicle, flags, dynamics)
        thrusts = [row["thrust_n"] for row in dynamics.observe(actual, vehicle)["engine_loads"][:6]]
        stored = witness.get("main_engine_thrust_n")
        saved._require(type(stored) is list and len(stored) == 6
                       and all(saved._near(a, b, 1e-5) for a, b in zip(stored, thrusts))
                       and any(value > 1000. for value in thrusts[:3])
                       and witness.get("positive_main_count") == sum(value > 1000. for value in thrusts)
                       and witness.get("threshold_n") == 1000.
                       and witness.get("meaning") == "first_at_or_after_request_positive_observed_load_not_proven_ignition",
                       "$", "Observed load/count/claim differs from actual saved actuators")
    retained = record.get("retained_return")
    saved._require(type(retained) is dict and retained.get("policy_id") == "trimmed_state_terminal_v4"
                   and retained.get("landing_verified") is False and retained.get("starship_vehicle_validated") is False,
                   "$", "Retained-return policy or claim changed")
    controller = record.get("return_controller")
    saved._require(type(controller) is dict and controller.get("record") == retained
                   and controller.get("policy") == "trimmed_state_terminal_v4"
                   and controller.get("released_count") == 0 and controller.get("registered") is False
                   and controller.get("allocation_enabled") is True
                   and controller.get("phase") == record["outcome"]["phase"]
                   and controller.get("return_time_s") == metadata["return_request_time_s"], "$", "Controller scope/history differs")
    activation, trigger = retained.get("activation"), retained.get("trigger")
    acts, triggers = events.get("retained_return_activated", []), events.get("retained_return_terminal_trigger", [])
    saved._require(bool(events.get("return_requested")) == (activation is not None)
                   and len(acts) == (1 if activation is not None else 0)
                   and len(triggers) == (1 if trigger is not None else 0)
                   and (witnesses["flip"] is not None) == (trigger is not None)
                   and retained.get("status") == ("triggered" if trigger else "active" if activation else "not_activated")
                   and type(retained.get("evaluation_count")) is int and retained["evaluation_count"] >= 0,
                   "$", "Retained activation/trigger history contradicts actual events")
    for key in ("activation", "trigger"):
        receipt = retained.get(key)
        if receipt is None:
            continue
        state = receipt["state"]
        _state(state, vehicle, flags, dynamics)
        saved._require(receipt["time_s"] == state["time_s"]
                       and any(_same_full(state, sample) and sample.get("retained_return") == key for sample in record["samples"]),
                       "$", "Retained receipt is not linked to a saved actual observation")
        if key == "activation":
            saved._require(receipt.get("payload_retained_count") == 26
                           and receipt.get("payload_retained_mass_kg") == 26*profile["payload"]["mass_each_kg"]
                           and state["phase"] == "orbital_coast" and state["time_s"] == metadata["return_request_time_s"]
                           and acts[0].get("policy_id") == retained["policy_id"] and acts[0]["time_s"] == state["time_s"],
                           "$", "Retained mass/activation time or phase differs")
        else:
            saved._require(receipt["budget"] == _observed_budget(state, vehicle, flags, dynamics, profile)
                           and (settings is not None or receipt["budget"]["trigger"] is True)
                           and state["phase"] == "ballistic_return" and retained["evaluation_count"] >= 1
                           and triggers[0].get("policy_id") == retained["policy_id"]
                           and triggers[0]["time_s"] == state["time_s"] == witnesses["flip"]["time_s"]
                           and saved._near(triggers[0].get("required_altitude_m"), receipt["budget"]["required_altitude_m"], 1e-5),
                           "$", "Terminal budget differs from actual state and frozen vehicle")
    binding = retained.get("fidelity_experiment")
    saved._require(binding is None if settings is None else binding == {"policy_id": "ship_entry_fidelity_v1", "settings": settings},
                   "$", "Candidate mode silently changed the baseline or lost its binding")
    diagnostics = retained.get("fidelity_trigger_diagnostics")
    if diagnostics is not None:
        saved._require(settings is not None and diagnostics.get("explicit_flip_altitude_m") == settings["flip_altitude_m"],
                       "$", "Budget diagnostic changed the explicit flip setting")
        first = diagnostics.get("first_budget_trigger")
        if first is not None:
            saved._require(first["budget"] == _observed_budget(first["state"], vehicle, flags, dynamics, profile)
                           and first["budget"]["trigger"] is True, "$", "First budget diagnostic is not source-bound")


def verify_record(record: Mapping, profile: Mapping, origin: Mapping, expected_settings: Mapping | None = None,
                  dt_scale: float = 1., *, expected_integration_substeps: int | None = None) -> dict:
    """Validate stored consistency; a finite impact or unresolved coast may pass.

    A refined held-control clock needs an explicit registered expectation. The
    default validates only the historical combined integration/control clock.
    Neither mode grants mission authority or establishes a numerical error bound.
    """
    from . import starship_sixdof as dynamics, starship_sixdof_verifier as saved, starship_physics as geometry
    from .starship_sixdof_mission import vehicle as make_vehicle
    from .starship_ship_fidelity import ShipFidelitySettings

    result = {"schema": "missionos.starship_ship_fidelity_verification.v1", "passed": False, "issues": [],
              "scope": "saved state/actuator/geometry/command consistency; no integration replay or real-vehicle qualification"}
    try:
        saved._json_tree(record)
        saved._json_tree(origin)
        saved._require(expected_integration_substeps is None or
                       type(expected_integration_substeps) is int and expected_integration_substeps in (1, 2, 4),
                       "$", "Unregistered numerical integration refinement")
        clock_fields = {
            "numerical_clock_policy": "fixed_control_hold_refined_plant_v1",
            "integration_substeps": expected_integration_substeps,
            "controller_polling_uses_control_hold_period": True,
            "allocator_horizon_uses_control_hold_period": True,
            "command_held_during_integration_substeps": True,
            "integration_steps_counts_control_holds": True,
            "integration_refinement_is_error_bound": False,
        }
        clock_metadata = record.get("ship_fidelity", {})
        if expected_integration_substeps is None:
            saved._require(not any(key in clock_metadata for key in clock_fields),
                           "$", "New numerical clock requires an explicit registered expectation")
        else:
            saved._require(type(dt_scale) in (int, float) and dt_scale == 1.
                           and type(clock_metadata.get("dt_scale")) in (int, float)
                           and clock_metadata.get("dt_scale") == 1.
                           and all(type(clock_metadata.get(key)) is type(value) and clock_metadata.get(key) == value
                                   for key, value in clock_fields.items()),
                           "$", "Numerical clock policy, substeps or held-control semantics differ")
        saved._require(record.get("scenario") == "ship_fidelity_return" and record.get("origin") == origin,
                       "$", "Wrong return scenario or actual source origin")
        saved._require(origin.get("basis") == "actual_post_insertion_screen_continuation_v1"
                       and origin.get("source_phase") == "ascent_screen_shutdown"
                       and origin.get("retained_count") == 26 and origin["state"]["time_s"] == 1537.
                       and origin.get("qualified_return") is False and origin.get("mission_authority_granted") is False
                       and origin.get("physical_execution") is False, "$", "Origin is outside source-bound unqualified scope")
        profile_bytes = (json.dumps(profile, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()
        saved._require(origin.get("profile_sha256") == hashlib.sha256(profile_bytes).hexdigest(), "$", "Origin profile differs")
        vehicle = dynamics.vehicle_from_dict(origin["vehicle"])
        saved._require(record.get("final_vehicle") == origin["vehicle"] == _wire(asdict(make_vehicle(profile, payload_count=26))),
                       "$", "Actual final vehicle or retained payload changed")
        flags = [row["available"] for row in origin["state"]["engine_states"]]
        saved._require([i for i, flag in enumerate(flags) if not flag] == [3], "$", "Registered unavailable RVac not preserved")
        first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship", wind_profile=profile)
        saved._require(_same_full(first, origin["state"]) and _same_full(record["initial_state"], origin["state"])
                       and first["phase"] == "ascent_screen_shutdown"
                       and _same_full(last, record["final_state"]), "$", "Initial/final pose, actuators or actual source phase changed")
        previous = None
        for sample in record["samples"]:
            actual = _state(sample, vehicle, flags, dynamics)
            saved._require(saved._near(geometry.ecef_to_geodetic(actual.r_eci_m)[2], sample["altitude_m"], 1e-5)
                           and saved._near(sample["mass_kg"], vehicle.dry_mass_kg+sample["propellant_kg"], 1e-5),
                           "$", "Saved altitude/mass differs from actual state")
            if previous is not None:
                saved._require(sample["propellant_kg"] <= previous["propellant_kg"]+1e-8, "$", "Propellant increased without a supply")
                if sample["time_s"] == previous["time_s"]:
                    saved._require(_same_full(sample, previous), "$", "Zero-time pose/actuator reset")
            previous = sample
        final = _state(record["final_state"], vehicle, flags, dynamics)
        observed = dynamics.observe(final, vehicle)
        outcome = record["outcome"]
        termination = outcome["termination"]
        saved._require(termination in TERMINATIONS, "$", "Unknown or numerical termination")
        saved._require(outcome.get("phase") == last["phase"] and outcome.get("payload_released_count") == 0
                       and outcome.get("six_dof_integrated") is True and outcome.get("attitude_prescribed") is False
                       and outcome.get("starship_vehicle_validated") is False
                       and type(outcome.get("integration_steps")) is int and outcome["integration_steps"] >= 0,
                       "$", "Outcome phase, inventory or simulator claim differs")
        saved._require(saved._near(outcome.get("start_time_s"), first["time_s"])
                       and saved._near(outcome.get("end_time_s"), last["time_s"])
                       and saved._near(outcome.get("duration_s"), last["time_s"]-first["time_s"])
                       and 0 <= last["time_s"]-first["time_s"] <= 6000.+1e-7
                       and saved._near(outcome.get("final_altitude_m"), observed["altitude_m"], 1e-5)
                       and saved._near(outcome.get("final_ground_speed_mps"), last["ground_speed_mps"], 1e-5),
                       "$", "Outcome clock or final observation differs")
        if termination == "time_limit":
            saved._require(saved._near(outcome["duration_s"], 6000.), "$", "Early unresolved stop without physical cause")
        if termination == "angular_rate_envelope_exceeded":
            saved._require(geometry.norm(final.omega_body_rad_s) > 5., "$", "Angular stop not supported by actual rate")
        events = saved._events(record.get("events"), "$.events", first["time_s"], last["time_s"])
        saved._require(len(events.get(termination, [])) == 1
                       and saved._near(events[termination][0]["time_s"], last["time_s"]), "$", "Termination event differs from final state")
        handoff = events.get("post_insertion_return_continuation", [])
        saved._require(len(handoff) == 1 and handoff[0]["time_s"] == first["time_s"]
                       and handoff[0].get("source_record_sha256") == origin["source_record_sha256"]
                       and handoff[0].get("source_phase") == origin["source_phase"]
                       and _same_full(handoff[0].get("state_before", {}), origin["state"])
                       and _same_full(handoff[0].get("state_after", {}), origin["state"]), "$", "Post-insertion handoff resets state or loses source")
        metadata = record.get("ship_fidelity")
        settings = ShipFidelitySettings(**expected_settings).record() if expected_settings is not None else None
        saved._require(type(metadata) is dict and metadata.get("schema") == "missionos.starship_ship_fidelity.v1"
                       and metadata.get("policy_id") == ("ship_entry_fidelity_v1" if settings is not None else "baseline_trimmed_return")
                       and metadata.get("settings") == settings and metadata.get("dt_scale") == dt_scale
                       and dt_scale in (1., .5) and metadata.get("retained_count") == 26
                       and metadata.get("source_phase") == "ascent_screen_shutdown"
                       and metadata.get("return_request_time_s") == first["time_s"]+60., "$", "Return mode, settings or clock changed")
        saved._require(metadata.get("effective_integration") == {key: profile["integration"][key]*dt_scale for key in ("powered_dt_s", "coast_dt_s")}
                       and metadata.get("allocation_interval_uses_effective_nominal_control_step") is True,
                       "$", "Controller/allocator and plant integration settings differ")
        for key, value in (("qualification_refreshed", False), ("mission_authority_granted", False), ("physical_execution", False),
                           ("deorbit_duration_forced", False), ("reported_eleven_seconds_is_planned_not_flown", True)):
            saved._require(metadata.get(key) is value, "$", "Return cannot claim qualification, grant or forced observed burn")
        requested = events.get("return_requested", [])
        saved._require(len(requested) == (1 if last["time_s"] >= metadata["return_request_time_s"] else 0), "$", "Return request presence differs")
        if requested:
            saved._require(requested[0]["time_s"] == metadata["return_request_time_s"], "$", "Return request clock changed")
        _witnesses(record, metadata, vehicle, flags, events, dynamics, saved, profile, settings)
        receipt = outcome.get("contact_receipt")
        contact_speed = None
        if termination in {"surface_impact", "low_speed_surface_contact"}:
            saved._require(last.get("contact") is True and events[termination][0].get("contact") == receipt,
                           "$", "Actual contact receipt/event differs")
            contact_speed = saved._contact(receipt, last, "$.contact", profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"])
            saved._require((contact_speed > 5.) == (termination == "surface_impact"), "$", "Contact termination mislabels speed")
        else:
            saved._require(receipt is None and not any(row.get("contact") is True for row in record["samples"]), "$", "Contact claimed before actual contact")
        result.update(passed=True, exact_post_insertion_handoff=True, retained_count=26,
                      inherited_availability_preserved=True, termination=termination,
                      contact_point_speed_mps=contact_speed, real_vehicle_qualified=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["issues"].append({"code": "invalid_ship_fidelity_record", "detail": str(exc)})
    return result


def load_reference(root: str | Path, registration: Mapping | None = None) -> dict:
    root = Path(root)
    reg = registration or common._read_json(root/REGISTRATION)
    for name, digest in reg["input_sha256"].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
            raise ValueError("registered_ship_input_hash_mismatch:"+name)
    fit, nonfit = reg["fit_data"], reg["non_fitted_curve"]
    if fit["file"] != nonfit["file"] or fit["elapsed_s_inclusive"] != [9841., 11217.] or nonfit["elapsed_s_inclusive"] != [11218., 11298.]:
        raise ValueError("unregistered_ship_reference_window")
    if (reg["reference_alignment"]["reference_time_origin_s"] != REFERENCE_EPOCH
            or reg["reference_alignment"]["simulation_anchor_altitude_m"] != ANCHOR_ALTITUDE_M
            or reg["reference_alignment"]["maximum_anchor_bracket_s"] != 2.1
            or any(definition["bin_time_origin_s"] != REFERENCE_EPOCH or definition["maximum_interpolation_gap_s"] != 2.1
                   for definition in (fit, nonfit))):
        raise ValueError("unregistered_ship_alignment_or_gap")
    result = {"schema": "missionos.starship_ship_fidelity_reference.v1", "registration": dict(reg),
              "reference_epoch_s": REFERENCE_EPOCH, "missing_values_filled": False, "quarantine_applied": False,
              "speed_frame": "surface_relative_working_assumption", "altitude_datum": "unknown_broadcast_datum",
              "relative_anchor_does_not_identify_real_epoch_or_orbit": True}
    series = {"fit": {"altitude": [], "speed": []}, "nonfit": {"altitude": [], "speed": []}}
    low = []
    with (root/fit["file"]).open(newline="") as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            time = common._number(float(row["t_plus_s"]), "reference_time")
            group = "fit" if 9841. <= time <= 11217. else "nonfit" if 11218. <= time <= 11298. else None
            if group is None:
                continue
            if row["body"] != "ship":
                raise ValueError("ship_reference_body_mismatch")
            for field, name, scale in (("displayed_altitude_km", "altitude", 1000.), ("displayed_speed_kmh", "speed", 1/3.6)):
                if row[field] == "":
                    continue
                value = common._number(float(row[field]), "reference_value")*scale
                series[group][name].append({"time_s": time-REFERENCE_EPOCH, "observed": value,
                    "reference_time_s": time, "source_line": line})
            if row["displayed_altitude_km"] and row["displayed_speed_kmh"] and .5 <= float(row["displayed_altitude_km"]) <= 1.:
                low.append({"reference_time_s": time, "time_s": time-REFERENCE_EPOCH, "source_line": line,
                            "altitude_m": float(row["displayed_altitude_km"])*1000., "speed_mps": float(row["displayed_speed_kmh"])/3.6})
    for group, definition, expected in (("fit", fit, (1356, 1316, 276, 275)), ("nonfit", nonfit, (70, 70, 16, 17))):
        counts = {"altitude_points": len(series[group]["altitude"]), "speed_points": len(series[group]["speed"]),
                  "altitude_bins": len({int(point["time_s"]//5) for point in series[group]["altitude"]}),
                  "speed_bins": len({int(point["time_s"]//5) for point in series[group]["speed"]})}
        if tuple(counts.values()) != expected or any(definition["expected_"+name] != value for name, value in counts.items()) or definition["bin_width_s"] != 5.:
            raise ValueError("frozen_ship_reference_mask_mismatch")
        result[group] = {**series[group], "counts": counts}
    if [row["reference_time_s"] for row in low] != list(range(11292, 11299)):
        raise ValueError("frozen_ship_low_altitude_mask_mismatch")
    result["low_altitude_display_speed_diagnostic"] = low
    return result


def _anchor(samples, record):
    cutoffs = [event for event in record["events"] if event["event"] == "deorbit_cutoff_command"]
    if len(cutoffs) > 1:
        raise ValueError("duplicate_ship_deorbit_cutoff")
    if not cutoffs:
        return None
    start = common._number(cutoffs[0]["time_s"], "deorbit_cutoff")
    for left, right in zip(samples, samples[1:]):
        if (left["time_s"] < start or not left["altitude_m"] >= ANCHOR_ALTITUDE_M >= right["altitude_m"]
                or left["altitude_m"] <= right["altitude_m"]):
            continue
        gap = right["time_s"]-left["time_s"]
        if gap > 2.1:
            raise ValueError("anchor_sample_gap_exceeded")
        crossing = left["time_s"]+(ANCHOR_ALTITUDE_M-left["altitude_m"])/(right["altitude_m"]-left["altitude_m"])*gap
        return {"time_s": crossing, "height_m": ANCHOR_ALTITUDE_M,
                "time_bracket_s": [left["time_s"], right["time_s"]], "altitude_bracket_m": [left["altitude_m"], right["altitude_m"]],
                "basis": "first_saved_descending_crossing_after_deorbit_cutoff_linear_bracket_no_clock_fit"}
    return None


def _point_series(samples, points, field):
    rows, bins = [], {}
    for point in points:
        predicted = common._interpolate(samples, point["time_s"], field)
        residual = predicted-point["observed"]
        square = residual*residual
        if not math.isfinite(square):
            raise ValueError("ship_residual_overflow")
        bin_id = int(point["time_s"]//5)
        bins.setdefault(bin_id, []).append(square)
        rows.append({**point, "simulated": predicted, "residual": residual, "bin_id": bin_id})
    means = [{"bin_id": key, "point_count": len(values), "mean_squared_residual": math.fsum(values)/len(values)} for key, values in sorted(bins.items())]
    return {"point_count": len(rows), "bin_count": len(means), "points": rows, "bins": means,
            "rmse": math.sqrt(math.fsum(row["mean_squared_residual"] for row in means)/len(means)) if means else None}


def _curve(samples, reference, *, upper):
    lower = min(point["time_s"] for name in ("altitude", "speed") for point in reference[name])
    for left, right in zip(samples, samples[1:]):
        if right["time_s"] > lower and left["time_s"] < upper and right["time_s"]-left["time_s"] > 2.1:
            raise ValueError("fit_sample_gap_exceeded")
    available, missing, series = {}, {}, {}
    for name, field in (("altitude", "altitude_m"), ("speed", "ground_speed_mps")):
        points = [point for point in reference[name] if samples and samples[0]["time_s"] <= point["time_s"] <= samples[-1]["time_s"]]
        missing[name] = [{"time_s": point["time_s"], "reference_time_s": point["reference_time_s"], "source_line": point["source_line"]}
                         for point in reference[name] if point not in points]
        partial = _point_series(samples, points, field) if points else None
        available[name+"_points"] = len(points)
        available[name+"_bins"] = partial["bin_count"] if partial else 0
        series[name] = partial
    complete = bool(samples) and samples[0]["time_s"] <= 0 and samples[-1]["time_s"] >= upper and not any(missing.values())
    return {"coverage_complete": complete, "required_counts": reference["counts"], "available_counts": available,
            "missing_points": missing, **series, "partial_rmse_is_not_rankable": not complete}


def _physical(record, reg):
    from . import starship_physics as geometry, starship_sixdof as dynamics
    state = record["final_state"]
    axis = dynamics.rotate(tuple(state["q_body_to_eci"]), (0., 0., 1.))
    receipt = record["outcome"].get("contact_receipt")
    up = tuple(receipt["surface_normal_eci"]) if receipt is not None else geometry.local_frame(
        geometry.State3D(state["time_s"], tuple(state["r_eci_m"]), tuple(state["v_eci_mps"]), state["propellant_kg"]))[0]
    tilt = math.degrees(math.acos(max(-1., min(1., geometry.dot(axis, up)))))
    rate = geometry.norm(state["omega_body_rad_s"])
    speed = receipt["surface_relative_speed_mps"] if receipt is not None else None
    gate = reg["contact_limits"]
    if gate != {"speed_mps": 5., "tilt_deg": 5., "body_rate_rad_s": .02, "reserve_kg": 28000.}:
        raise ValueError("unchanged_ship_contact_limits_required")
    checks = {"actual_contact": receipt is not None,
              "non_overlap": receipt is not None and receipt.get("initial_overlap") is False,
              "descending_contact": receipt is not None and receipt["surface_normal_speed_mps"] <= 0,
              "contact_speed": speed is not None and speed <= gate["speed_mps"],
              "tilt": tilt <= gate["tilt_deg"], "rate": rate <= gate["body_rate_rad_s"],
              "reserve": state["propellant_kg"] >= gate["reserve_kg"]}
    return {"accepted": all(checks.values()), "gates": checks, "contact_point_speed_mps": speed,
            "tilt_deg": tilt, "rate_rad_s": rate, "final_fuel_kg": state["propellant_kg"],
            "contact_time_s": receipt["event_time_s"] if receipt else None, "used_for_ranking": False,
            "real_vehicle_or_landing_validated": False}


def score_record(record: Mapping, reference: Mapping, registration: Mapping | None = None) -> dict:
    """Use the entire fixed training mask, including uncovered points as missing."""
    reg = registration or reference["registration"]
    result = {"schema": "missionos.starship_ship_fidelity_metrics.v1", "record_valid": False,
              "category": "invalid_record", "fit_admissible": False}
    try:
        samples = common._samples(record, True)
        termination = record["outcome"]["termination"]
        if termination not in TERMINATIONS:
            raise ValueError("unknown_or_numerical_ship_termination")
        anchor = _anchor(samples, record)
        relative = [{**row, "time_s": row["time_s"]-anchor["time_s"]} for row in samples] if anchor else []
        fit = _curve(relative, reference["fit"], upper=1376.)
        nonfit = _curve(relative, reference["nonfit"], upper=1457.)
        nonfit["used_for_ranking"] = False
        low = []
        for point in reference["low_altitude_display_speed_diagnostic"]:
            covered = bool(relative) and relative[0]["time_s"] <= point["time_s"] <= relative[-1]["time_s"]
            low.append({**point, "covered": covered,
                "simulated_speed_mps": common._interpolate(relative, point["time_s"], "ground_speed_mps") if covered else None})
        physical = _physical(record, reg)
        witnesses = record["ship_fidelity"]["command_witnesses"]
        ignition, cutoff, flip = (witnesses[name] for name in COMMANDS)
        flip_diag = None
        if flip is not None:
            time = flip["time_s"]
            index = bisect_left([row["time_s"] for row in samples], time)
            before = samples[max(0, index-1)]
            flip_diag = {"time_s": time, "altitude_m": record["retained_return"]["trigger"]["state"]["altitude_m"],
                         "fuel_kg": flip["state"]["propellant_kg"], "preceding_saved_altitude_m": before["altitude_m"],
                         "budget": record["retained_return"]["trigger"]["budget"], "used_for_ranking": False}
        result.update(record_valid=True, fit_admissible=fit["coverage_complete"],
            category="valid_fit" if fit["coverage_complete"] else "valid_physical_negative",
            termination=termination, anchor=anchor, training_curve=fit, altitude=fit["altitude"] if fit["coverage_complete"] else None,
            speed=fit["speed"] if fit["coverage_complete"] else None, non_fitted_curve=nonfit,
            low_altitude_display_speed_diagnostic={"points": low, "observed_flip_not_identified": True, "used_for_ranking": False},
            physical_recovery=physical, flip_diagnostic=flip_diag,
            deorbit_diagnostic={"ignition_command_s": ignition["time_s"] if ignition else None,
                "cutoff_command_s": cutoff["time_s"] if cutoff else None,
                "command_interval_s": cutoff["time_s"]-ignition["time_s"] if ignition and cutoff else None,
                "eleven_seconds_is_planned_not_observed": True, "used_for_ranking": False},
            relative_anchor_is_conditional_not_real_vehicle_validation=True)
        if not fit["coverage_complete"]:
            result["reason"] = "registered_horizon_or_physical_end_prevents_full_reference_coverage_no_extrapolation"
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["reason"] = str(exc)
    return result


def select_candidate(results: Mapping[str, Mapping], registration: Mapping) -> dict:
    order = [row["id"] for row in registration["candidate_grid"]]
    if set(results) != {"B0", *order}:
        raise ValueError("complete_registered_ship_cohort_required")
    if any(row.get("record_valid") is not True for row in results.values()):
        raise ValueError("invalid_ship_record_stops_selection")
    baseline = results["B0"]
    comparable = baseline.get("fit_admissible") is True
    ba = common._number(baseline["altitude"]["rmse"], "baseline_altitude_rmse") if comparable else None
    bs = common._number(baseline["speed"]["rmse"], "baseline_speed_rmse") if comparable else None
    if comparable and (ba <= 0 or bs <= 0):
        raise ValueError("nonpositive_ship_baseline_denominator")
    gate = registration["metrics"]["calibration_gate"]
    rows, selected = [], None
    for name in order:
        metrics = results[name]
        row = {"id": name, "fit_admissible": metrics.get("fit_admissible") is True,
               "passes_all_four_gates": False, "altitude_ratio": None, "speed_ratio": None, "score": None}
        if row["fit_admissible"]:
            altitude = common._number(metrics["altitude"]["rmse"], "candidate_altitude_rmse")
            speed = common._number(metrics["speed"]["rmse"], "candidate_speed_rmse")
            if altitude < 0 or speed < 0:
                raise ValueError("negative_ship_candidate_rmse")
            ratios = (altitude/ba, speed/bs) if comparable else (None, None)
            checks = {"absolute_altitude": altitude <= gate["maximum_altitude_rmse_m"],
                      "absolute_speed": speed <= gate["maximum_speed_rmse_mps"],
                      "relative_altitude": comparable and ratios[0] <= gate["maximum_altitude_rmse_fraction_of_baseline"],
                      "relative_speed": comparable and ratios[1] <= gate["maximum_speed_rmse_fraction_of_baseline"]}
            row.update(altitude_rmse_m=altitude, speed_rmse_mps=speed, altitude_ratio=ratios[0], speed_ratio=ratios[1],
                       gates=checks, passes_all_four_gates=all(checks.values()), score=.5*sum(ratios) if comparable else None)
            if row["passes_all_four_gates"] and (selected is None or row["score"] < selected["score"]):
                selected = row
        rows.append(row)
    return {"schema": "missionos.starship_ship_fidelity_selection.v1", "selected_id": selected["id"] if selected else None,
            "selected_score": selected["score"] if selected else None, "candidates": rows, "baseline_comparable": comparable,
            "basis": "fixed_training_mask_all_four_gates_fit_only_registered_id_ties",
            "nonfit_contact_fuel_or_convergence_used_for_ranking": False,
            "incomplete_baseline_makes_relative_ratios_null": not comparable}


def compare_resolution(full_record: Mapping, half_record: Mapping, registration: Mapping) -> dict:
    """Unranked same-origin dt check; it never alters any acceptance margin."""
    from . import starship_sixdof_verifier as saved
    saved._json_tree(full_record)
    saved._json_tree(half_record)
    if (full_record["origin"] != half_record["origin"]
            or full_record["ship_fidelity"]["settings"] != half_record["ship_fidelity"]["settings"]
            or full_record["ship_fidelity"]["dt_scale"] != 1. or half_record["ship_fidelity"]["dt_scale"] != .5):
        raise ValueError("resolution_comparison_requires_same_actual_origin_and_settings")
    full, half = (record["outcome"].get("contact_receipt") for record in (full_record, half_record))
    result = {"schema": "missionos.starship_ship_resolution_comparison.v1", "both_contacted": full is not None and half is not None,
              "full_termination": full_record["outcome"]["termination"], "half_termination": half_record["outcome"]["termination"],
              "used_for_ranking": False, "acceptance_margin_enlarged": False, "real_vehicle_validated": False,
              "contact_distance_m": None, "contact_time_difference_s": None, "contact_velocity_difference_mps": None,
              "contact_speed_difference_mps": None}
    a, b = full_record["final_state"], half_record["final_state"]
    quaternion_dot = abs(math.fsum(x*y for x, y in zip(a["q_body_to_eci"], b["q_body_to_eci"])))
    result.update(final_fuel_difference_kg=b["propellant_kg"]-a["propellant_kg"],
                  final_attitude_difference_deg=math.degrees(2*math.acos(max(-1., min(1., quaternion_dot)))),
                  final_attitude_difference_basis="ECI_body_quaternion_at_different_final_epochs")
    if not result["both_contacted"]:
        return result
    def fixed(receipt, field="point_eci_m"):
        angle = 7.292115e-5*receipt["event_time_s"]
        c, s = math.cos(angle), math.sin(angle)
        x, y, z = receipt[field]
        return (c*x+s*y, -s*x+c*y, z)
    a, b = fixed(full), fixed(half)
    cross = (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    angle = math.atan2(math.sqrt(math.fsum(value*value for value in cross)), math.fsum(x*y for x, y in zip(a, b)))
    full_velocity, half_velocity = (fixed(receipt, "surface_relative_velocity_mps") for receipt in (full, half))
    result.update(contact_distance_m=6378137.*angle,
        contact_time_difference_s=half["event_time_s"]-full["event_time_s"],
        contact_velocity_difference_mps=math.sqrt(math.fsum((a-b)**2 for a, b in zip(full_velocity, half_velocity))),
        contact_speed_difference_mps=half["surface_relative_speed_mps"]-full["surface_relative_speed_mps"],
        contact_velocity_difference_basis="ECEF_surface_relative_vectors_each_rotated_from_its_own_ECI_contact_epoch",
        contact_distance_basis="Earth_fixed_spherical_arc_6378137m_not_inertial_chord")
    return result
