"""Stored-output checks and preregistered timing-only Stage 2c selection.

Every function is observational. No simulator step, provider call or hardware
operation is performed. Matching command times never implies physical recovery
or agreement with the flown vehicle; the targets are approximate planned times.
"""
from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

from . import starship_ascent_metrics as common


REGISTRATION = "examples/spaceflight/starship-booster-fidelity-registration-v1.json"
POLICY = "fidelity_schedule_v1"
COMMANDS = {"boostback": "booster_boostback_ignition_command",
            "boostback_cutoff": "booster_boostback_cutoff_command", "landing": "booster_landing_burn_requested"}
STATE_FIELDS = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                "propellant_kg", "engine_states", "flap_angles_rad")


def _wire(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _state(value, dynamics):
    return dynamics.state_from_dict({key: value[key] for key in STATE_FIELDS})


def _state_and_flags(value, vehicle, expected_flags, dynamics):
    actual = _state(value, dynamics)
    flags = [engine.available for engine in actual.engine_states]
    if flags != expected_flags:
        raise ValueError("inherited_engine_availability_changed")
    command = dynamics.Command6DOF(tuple(dynamics.EngineCommand() for _ in vehicle.engines), actual.flap_angles_rad)
    dynamics._validate_pair(actual, vehicle, command)
    return actual


def load_reference(root: str | Path, registration: Mapping | None = None) -> dict:
    root = Path(root)
    registration = registration or common._read_json(root / REGISTRATION)
    for relative, expected in registration["input_sha256"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("registered_booster_input_hash_mismatch:" + relative)
    definition = registration["non_fitted_curve"]
    lower, upper = definition["elapsed_s_inclusive"]
    altitude, speed, lines = [], [], []
    with (root / definition["file"]).open(newline="") as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            time = float(row["t_plus_s"])
            if not lower <= time <= upper:
                continue
            if row["body"] != "super_heavy":
                raise ValueError("booster_reference_body_mismatch")
            lines.append(line)
            for field, points, scale in (("displayed_altitude_km", altitude, 1000.),
                                          ("displayed_speed_kmh", speed, 1 / 3.6)):
                if row[field] == "":
                    continue
                value = common._number(float(row[field]), "booster_reference") * scale
                points.append({"time_s": time, "observed": value, "source_line": line})
    counts = {"altitude_points": len(altitude), "speed_points": len(speed),
              "altitude_bins": len({int(row["time_s"] // 5) for row in altitude}),
              "speed_bins": len({int(row["time_s"] // 5) for row in speed})}
    expected = {name: definition["expected_" + name] for name in counts}
    if counts != expected or counts != {"altitude_points": 124, "speed_points": 115, "altitude_bins": 25, "speed_bins": 25}:
        raise ValueError("frozen_booster_curve_mask_mismatch")
    if (lower, upper) != (246., 369.) or definition["bin_width_s"] != 5. or lines != list(range(51, 175)):
        raise ValueError("unregistered_booster_curve_window")
    return {"schema": "missionos.starship_booster_curve_reference.v1", "altitude": altitude, "speed": speed,
            "counts": counts, "registration": dict(registration), "used_for_ranking": False,
            "missing_values_filled": False, "quarantine_applied": False,
            "speed_frame": "surface_relative_working_assumption", "altitude_datum": "unknown_broadcast_datum"}


def _fidelity(record, metadata, expected_settings, profile, vehicle, flags, events, dynamics, saved):
    from .starship_booster_fidelity import BoosterFidelitySettings
    settings = BoosterFidelitySettings(**expected_settings).record()
    saved._require(type(metadata) is dict and metadata.get("policy_id") == POLICY and metadata.get("settings") == settings,
                   "$", "Fidelity settings differ from registered candidate")
    saved._require(metadata.get("boostback_window") == {"opens_at_s": 147., "cutoff_at_s": 187.}, "$", "Fixed boostback window changed")
    for key, value in (("separation_state_preserved", True), ("availability_preserved", True),
                       ("physical_execution", False), ("mission_authority_granted", False)):
        saved._require(metadata.get(key) is value, "$", "Fidelity handoff or authority claim differs")
    saved._require(metadata.get("requested_boostback_group_count") == 33
                   and metadata.get("maximum_landing_group_count") == 13
                   and metadata.get("initial_available_main_count") == sum(flags[:33]), "$", "Declared engine groups differ from actual frozen capability")
    witnesses = metadata.get("command_witnesses")
    saved._require(type(witnesses) is dict and set(witnesses) == set(COMMANDS), "$", "Missing bounded command-witness table")
    for name, event_name in COMMANDS.items():
        found, witness = events.get(event_name, []), witnesses[name]
        saved._require(len(found) <= 1 and bool(found) == (witness is not None), "$", "Command witness/event presence differs")
        if not found:
            continue
        saved._require(witness["time_s"] == found[0]["time_s"] == witness["state"]["time_s"], "$", "Command timestamp differs from its saved state/event")
        actual = _state_and_flags(witness["state"], vehicle, flags, dynamics)
        command = dynamics.command_from_dict(witness["command"])
        dynamics._validate_pair(actual, vehicle, command)
        count, throttle = witness.get("requested_main_count"), witness.get("requested_throttle")
        saved._require(type(count) is int and 0 <= count <= 33 and saved._number(throttle) and 0 <= throttle <= 1,
                       "$", "Invalid requested main-engine group or throttle")
        if name == "boostback":
            saved._require(147. <= witness["time_s"] < 187. and count == 33, "$", "Boostback requested outside fixed guarded window")
        elif name == "boostback_cutoff":
            saved._require(witness["time_s"] <= 187., "$", "Boostback cutoff exceeds fixed window")
        else:
            saved._require(count <= 13, "$", "Landing command exceeds frozen gimballed engine group")
        for index, target in enumerate(command.engines[:33]):
            saved._require(target.enabled is (index < count and throttle > 0)
                           and target.throttle == (throttle if index < count else 0.), "$", "Saved main command differs from requested bounded group")
    loads = metadata.get("first_load_witnesses")
    saved._require(type(loads) is dict and set(loads) == {"boostback", "landing"}, "$", "Missing bounded observed-load table")
    for name, witness in loads.items():
        if witness is None:
            request = witnesses[name]
            if request is not None:
                for sample in record["samples"]:
                    if sample["time_s"] < request["time_s"]:
                        continue
                    observation = dynamics.observe(_state(sample, dynamics), vehicle)
                    thrusts = [row["thrust_n"] for engine, row in zip(vehicle.engines, observation["engine_loads"]) if "main" in engine.name]
                    eligible = thrusts[3:] if name == "boostback" else thrusts[:13]
                    saved._require(not any(value > 1000. for value in eligible), "$", "Missing load witness contradicts recorded positive post-request thrust")
            continue
        request = witnesses[name]
        saved._require(request is not None and request["time_s"] <= witness["time_s"] == witness["state"]["time_s"] <= record["final_state"]["time_s"],
                       "$", "Observed main load precedes request or exceeds actual flight")
        actual = _state_and_flags(witness["state"], vehicle, flags, dynamics)
        observed = dynamics.observe(actual, vehicle)
        thrusts = [load["thrust_n"] for engine, load in zip(vehicle.engines, observed["engine_loads"]) if "main" in engine.name]
        stored = witness.get("main_engine_thrust_n")
        saved._require(type(stored) is list and len(stored) == len(thrusts)
                       and all(saved._near(a, b, 1e-5) for a, b in zip(stored, thrusts)), "$", "Observed main load differs from actual saved actuator state")
        eligible = thrusts[3:] if name == "boostback" else thrusts[:13]
        saved._require(any(value > 1000. for value in eligible)
                       and witness.get("positive_main_count") == sum(value > 1000. for value in thrusts)
                       and witness.get("threshold_n") == 1000.
                       and witness.get("meaning") == "first_post_request_positive_observed_load_not_proven_ignition",
                       "$", "First-load count, positive group or evidence class differs")


def verify_record(record: Mapping, profile: Mapping, actual_origin: Mapping, expected_settings: Mapping | None = None) -> dict:
    """Check exact inherited state and physical record; a valid impact may pass."""
    from . import starship_sixdof as dynamics, starship_sixdof_verifier as saved, starship_physics as geometry
    from .starship_sixdof_mission import vehicle as make_vehicle
    from .starship_sixdof_booster import _configuration

    result = {"schema": "missionos.starship_booster_fidelity_verification.v1", "passed": False, "issues": [],
              "scope": "saved state/command/load/geometry consistency; no integration replay or real-vehicle qualification"}
    try:
        saved._json_tree(record)
        saved._json_tree(actual_origin)
        first, last, outcome, events, contact_speed = saved._run_basics(record, "$.booster", [0], profile, body_id="booster")
        saved._require(record.get("scenario") == "booster_return" and record.get("body_id") == "booster", "$", "Wrong continuation body or scenario")
        saved._require(record.get("booster_separation_state") == actual_origin
                       and all(first.get(key) == actual_origin.get(key) == record["initial_state"].get(key) for key in STATE_FIELDS),
                       "$", "Actual separation pose/rates/fuel/actuators changed")
        saved._require(all(last.get(key) == record["final_state"].get(key) for key in STATE_FIELDS),
                       "$", "Final actuator/spool state differs from final physical sample")
        saved._require(saved._near(outcome.get("start_time_s"), first["time_s"])
                       and saved._near(outcome.get("end_time_s"), last["time_s"])
                       and last["time_s"] <= first["time_s"] + 1200. + 1e-7, "$", "Continuation duration differs from registered actual origin")
        if outcome["termination"] == "time_limit":
            saved._require(saved._near(last["time_s"] - first["time_s"], 1200.), "$", "Nonphysical early horizon stop")
        saved._require(outcome.get("physical_execution") is False and outcome.get("catch_verified") is False
                       and outcome.get("landing_hardware_validated") is False, "$", "Continuation cannot claim hardware/catch execution")
        for key in ("active_return_site", "mission_director", "trouble_control", "development_coast", "development_coast_from_separation"):
            saved._require(record.get(key) is None, "$", "Fidelity cannot borrow unrelated mission or development scopes")
        vehicle = make_vehicle(profile, "booster")
        saved._require(record.get("final_vehicle") == _wire(asdict(vehicle)), "$", "Frozen booster vehicle or wind differs")
        flags = [row["available"] for row in actual_origin["engine_states"]]
        previous = None
        for sample in record["samples"]:
            actual = _state_and_flags(sample, vehicle, flags, dynamics)
            altitude = geometry.ecef_to_geodetic(actual.r_eci_m)[2]
            saved._require(saved._near(altitude, sample["altitude_m"], 1e-5)
                           and saved._near(sample["mass_kg"], vehicle.dry_mass_kg + sample["propellant_kg"], 1e-5), "$", "Altitude/mass differs from actual saved state")
            if previous is not None:
                saved._require(sample["propellant_kg"] <= previous["propellant_kg"] + 1e-8, "$", "Propellant increased during a single-body continuation")
                if sample["time_s"] == previous["time_s"]:
                    saved._require(all(sample.get(key) == previous.get(key) for key in STATE_FIELDS), "$", "Zero-time state or actuator reset")
            previous = sample
        final = _state_and_flags(record["final_state"], vehicle, flags, dynamics)
        observed = dynamics.observe(final, vehicle)
        saved._require(saved._near(last["mass_kg"], observed["mass_kg"], 1e-5), "$", "Final mass differs from actual vehicle")
        recovery = saved._booster_envelope(record, profile, last, "$.booster")
        peak_witness = record.get("runner_booster_peak_witness")
        saved._require(type(peak_witness) is dict, "$", "Missing all-tick numerical peak witness")
        peak = peak_witness.get("state")
        saved._state(peak, "$.booster.peak")
        _state_and_flags(peak, vehicle, flags, dynamics)
        peak_altitude = geometry.ecef_to_geodetic(tuple(peak["r_eci_m"]))[2]
        saved._require(first["time_s"] <= peak["time_s"] <= last["time_s"]
                       and saved._near(outcome["max_altitude_m"], peak_altitude, 1e-5)
                       and peak_altitude >= max(sample["altitude_m"] for sample in record["samples"]) - 1e-5,
                       "$", "Recorded maximum differs from saved numerical peak state")
        for event_name in COMMANDS.values():
            saved._require(len(events.get(event_name, [])) <= 1, "$", "Repeated return phase command")
        saved._require(outcome.get("boostback_started") is bool(events.get(COMMANDS["boostback"]))
                       and outcome.get("coast_started") is bool(events.get(COMMANDS["boostback_cutoff"])), "$", "Phase flags differ from actual command events")
        expected_policy = "fixed_v1" if expected_settings is None else POLICY
        configuration = _configuration(profile)
        if expected_settings is not None:
            configuration.update(expected_settings)
        saved._require(record.get("guidance_policy") == expected_policy and record.get("guidance_configuration") == configuration,
                       "$", "Guidance or frozen configuration differs from registered mode")
        metadata = record.get("booster_fidelity")
        if expected_settings is None:
            saved._require(metadata is None, "$", "Baseline must remain the existing fixed_v1 path")
        else:
            _fidelity(record, metadata, expected_settings, profile, vehicle, flags, events, dynamics, saved)
        result.update(passed=True, exact_separation_handoff=True, inherited_availability_preserved=True,
                      physical_recovery_accepted=recovery, contact_point_speed_mps=contact_speed,
                      termination=outcome["termination"], source_bound_peak_not_independent_reintegration=True)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["issues"].append({"code": "invalid_booster_fidelity_record", "detail": str(exc)})
    return result


def _timing(record, registration):
    definition = registration["timing_fit"]
    targets = definition["targets"]
    if set(targets) != set(COMMANDS.values()) or definition["aggregation"] != "mean_absolute_error":
        raise ValueError("unregistered_booster_timing_metric")
    rows = []
    for name, target in targets.items():
        found = [event for event in record["events"] if event["event"] == name]
        if len(found) > 1:
            raise ValueError("duplicate_booster_timing_event")
        time = common._number(found[0]["time_s"], "command_time") if found else None
        error = time - target if time is not None else None
        rows.append({"event": name, "target_s": target, "actual_s": time, "signed_error_s": error,
                     "absolute_error_s": abs(error) if error is not None else None,
                     "gate_passed": error is not None and abs(error) <= definition["maximum_individual_error_s"]})
    complete = all(row["actual_s"] is not None for row in rows)
    return {"admissible": complete, "gates_passed": complete and all(row["gate_passed"] for row in rows),
            "mean_absolute_error_s": math.fsum(row["absolute_error_s"] for row in rows) / len(rows) if complete else None,
            "events": rows, "basis": "approximate_planned_command_times_not_observed_real_ignition",
            "missing_events_are_valid_negative": True, "contact_or_outcome_used_for_ranking": False}


def score_record(record: Mapping, reference: Mapping, registration: Mapping | None = None) -> dict:
    registration = registration or reference["registration"]
    result = {"schema": "missionos.starship_booster_fidelity_metrics.v1", "record_valid": False, "category": "invalid_record"}
    try:
        samples = common._samples(record, True)
        outcome = record["outcome"]
        if outcome["termination"] not in {"surface_contact", "angular_rate_envelope_exceeded", "time_limit"}:
            raise ValueError("unknown_or_numerical_booster_termination")
        timing = _timing(record, registration)
        lower, upper = registration["non_fitted_curve"]["elapsed_s_inclusive"]
        complete = samples[0]["time_s"] <= lower and samples[-1]["time_s"] >= upper
        curve = {"coverage_complete": complete, "used_for_ranking": False, "required_counts": reference["counts"],
                 "altitude": None, "speed": None}
        if complete:
            curve.update(altitude=common._series(samples, reference["altitude"], "altitude_m"),
                         speed=common._series(samples, reference["speed"], "ground_speed_mps"))
        elif outcome["termination"] in {"surface_contact", "angular_rate_envelope_exceeded"} and samples[-1]["time_s"] < upper:
            curve["reason"] = "physical_termination_prevents_complete_frozen_curve_coverage_no_extrapolation"
            curve["missing_points"] = {name: [{"source_line": point["source_line"], "time_s": point["time_s"]}
                for point in reference[name] if not samples[0]["time_s"] <= point["time_s"] <= samples[-1]["time_s"]]
                for name in ("altitude", "speed")}
        else:
            raise ValueError("unexplained_booster_curve_coverage_defect")
        check = registration["non_fitted_booster_check"]
        if check["descending_height_m"] != 40000.:
            raise ValueError("unregistered_descending_height")
        adapted = {"non_fitted_booster_check": {
            "apex_gate": {"target_m": check["apex_target_m"], "maximum_absolute_residual_m": check["apex_tolerance_m"]},
            "descending_40000m_gate": {"reference_elapsed_interval_s": check["descending_observed_interval_s"],
                                       "maximum_distance_to_reference_interval_s": check["descending_maximum_interval_distance_s"]}}}
        derived = common.score_booster(record, adapted)
        if not derived["record_valid"]:
            raise ValueError("invalid_derived_booster_geometry:" + derived.get("reason", "unknown"))
        contact = record.get("contact")
        contact_time = contact.get("event_time_s") if contact is not None else None
        accepted = outcome.get("simulated_contact_envelope_met") is True
        result.update(record_valid=True, category="valid_recovery" if accepted else "valid_physical_negative",
                      termination=outcome["termination"], timing_fit=timing, descent_curve=curve,
                      non_fitted_booster=derived,
                      physical_recovery={"accepted": accepted, "contact_point_speed_mps": contact.get("surface_relative_speed_mps") if contact else None,
                          "com_ground_speed_mps": outcome.get("final_ground_speed_mps"), "tilt_deg": outcome.get("final_tilt_deg"),
                          "rate_rad_s": outcome.get("final_body_rate_rad_s"), "final_fuel_kg": samples[-1]["propellant_kg"],
                          "used_for_ranking": False, "real_vehicle_or_catch_verified": False},
                      planned_landing_end_diagnostic={"planned_s": registration["timing_fit"]["planned_landing_end_s"],
                          "actual_contact_s": contact_time,
                          "difference_s": contact_time - registration["timing_fit"]["planned_landing_end_s"] if contact_time is not None else None,
                          "planned_end_is_not_observed_contact": True, "used_for_ranking": False})
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["reason"] = str(exc)
    return result


def select_candidate(results: Mapping[str, Mapping], registration: Mapping) -> dict:
    order = [row["id"] for row in registration["candidate_grid"]]
    if set(results) != {"B0", *order}:
        raise ValueError("complete_registered_booster_cohort_required")
    if any(row.get("record_valid") is not True for row in results.values()):
        raise ValueError("invalid_booster_record_stops_selection")
    selected, rows = None, []
    for candidate_id in order:
        timing = results[candidate_id]["timing_fit"]
        if type(timing.get("admissible")) is not bool or type(timing.get("gates_passed")) is not bool:
            raise ValueError("invalid_booster_timing_admission_flag")
        row = {"id": candidate_id, "eligible": timing["admissible"] and timing["gates_passed"],
               "mean_absolute_error_s": timing["mean_absolute_error_s"]}
        if row["eligible"] and common._number(row["mean_absolute_error_s"], "timing_MAE") < 0:
            raise ValueError("negative_booster_timing_error")
        if row["eligible"] and (selected is None or row["mean_absolute_error_s"] < selected["mean_absolute_error_s"]):
            selected = row
        rows.append(row)
    return {"schema": "missionos.starship_booster_fidelity_selection.v1", "selected_id": selected["id"] if selected else None,
            "selected_mean_absolute_error_s": selected["mean_absolute_error_s"] if selected else None, "candidates": rows,
            "basis": "three_planned_command_times_only_all_individual_gates_then_MAE_registered_order_ties",
            "descent_apex_contact_fuel_or_40km_used_for_ranking": False,
            "selected_timing_does_not_imply_physical_recovery": True}
