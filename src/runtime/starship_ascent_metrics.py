"""Pure frozen-mask Stage 2b metrics; this module never advances a plant.

The recorded state, a conditional broadcast speed frame, and sampled numerical
apex are evidence limits. These checks do not qualify a vehicle or reconstruct
hidden physical states from outcome claims.
"""
from __future__ import annotations

from bisect import bisect_left
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


REGISTRATION = "examples/spaceflight/starship-ascent-path-registration-v2.json"
PHYSICAL_FAILURES = frozenset({
    "angular_rate_envelope_exceeded", "surface_impact", "low_speed_surface_contact",
    "surface_contact", "ascent_reserve_exhausted", "finite_ascent_reserve",
    "finite_insertion_failed", "orbit_insertion_failed", "insertion_unachieved",
    "ascent_physical_failure", "fuel_reserve_exhausted", "finite_insertion_unachieved",
    "insertion_gate_unreached",
})


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: finite number required")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name}: finite number required") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}: finite number required")
    return result


def _finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite_record_value")
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_tree(item)


def _read_json(path: Path) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def load_reference(root: str | Path) -> dict:
    """Load every accepted observation and verify the registration input hashes.

    Points retain CSV line numbers and quarantine receipt; no observation is
    filled, smoothed, relabeled, rejected by residual, or shifted in time.
    """
    root = Path(root)
    registration = _read_json(root / REGISTRATION)
    hashes = {}
    for relative, expected in registration["input_sha256"].items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"reference_input_hash_mismatch:{relative}")
        hashes[relative] = actual
    selection = _read_json(root / "docs/assets/starship-reference-v2-20261018/reference.json")
    acceptance = _read_json(root / selection["acceptance_record"]["path"])
    if selection["status"] != "accepted_for_bounded_engineering_evaluation" or acceptance["user_reply"] != "はい":
        raise ValueError("reference_not_accepted")
    fit = registration["fit_data"]
    quarantines = _read_json(root / fit["quarantine"]["file"])["proposed_field_quarantine"]
    expected_lines = set(fit["quarantine"]["source_lines"])
    declared = {item["line"]: item for item in quarantines}
    if set(declared) != expected_lines or len(declared) != 11 or len(quarantines) != 11:
        raise ValueError("quarantine_mask_mismatch")
    altitude, speed, excluded = [], [], []
    start, end = fit["elapsed_s_inclusive"]
    rows_in_window = 0
    with (root / fit["file"]).open(newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            time = float(row["t_plus_s"])
            if not start <= time <= end:
                continue
            rows_in_window += 1
            if row["body"] != fit["body"]:
                raise ValueError("reference_body_mismatch")
            for field, destination, factor in (("displayed_altitude_km", altitude, 1000.),
                                                ("displayed_speed_kmh", speed, 1 / 3.6)):
                if line in declared and field == "displayed_altitude_km":
                    item = declared[line]
                    if (item["source"] != fit["file"] or item["field"] != field
                            or item["t_plus_s"] != time or item["replacement_value"] is not None
                            or not row[field] or float(row[field]) != item["stored_value"]):
                        raise ValueError("quarantine_source_mismatch")
                    excluded.append({"source_line": line, "time_s": time, "field": field})
                    continue
                if row[field] == "":
                    continue
                value = _number(float(row[field]), "reference") * factor
                destination.append({"time_s": time, "observed": value, "source_line": line})
    counts = {"altitude_points": len(altitude), "speed_points": len(speed),
              "altitude_bins": len({int(p["time_s"] // 5) for p in altitude}),
              "speed_bins": len({int(p["time_s"] // 5) for p in speed})}
    if counts != {"altitude_points": 145, "speed_points": 442, "altitude_bins": 36, "speed_bins": 96}:
        raise ValueError("frozen_mask_count_mismatch")
    if rows_in_window != fit["rows_in_window"] or len(excluded) != 11:
        raise ValueError("frozen_mask_coverage_mismatch")
    return {"schema": "missionos.starship_ascent_reference.v1", "altitude": altitude,
            "speed": speed, "excluded_altitude_fields": excluded, "counts": counts,
            "input_sha256": hashes, "registration": registration,
            "speed_frame": "surface_relative_working_assumption", "altitude_datum": "unknown_broadcast_datum"}


def _samples(record: Mapping, require_speed: bool) -> list[dict]:
    _finite_tree(record)
    samples = record.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("missing_samples")
    separation_times = {event.get("time_s") for event in record.get("events", [])
                        if event.get("kind") == "stage_separation" or event.get("event") == "stage_separation"}
    result = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("sample_object_required")
        time = _number(sample.get("time_s"), "sample_time")
        altitude = _number(sample.get("altitude_m"), "sample_altitude")
        fuel = _number(sample.get("propellant_kg"), "sample_propellant")
        if time < 0 or fuel < 0:
            raise ValueError("negative_time_or_fuel")
        speed = _number(sample.get("ground_speed_mps"), "sample_speed") if require_speed else None
        if speed is not None and speed < 0:
            raise ValueError("negative_speed")
        value = {"time_s": time, "altitude_m": altitude, "ground_speed_mps": speed,
                 "propellant_kg": fuel, "phase": sample.get("phase"), "contact": sample.get("contact") is True}
        if result and time < result[-1]["time_s"]:
            raise ValueError("sample_time_reversed")
        if result and time == result[-1]["time_s"]:
            prior = result[-1]
            same = all(prior[key] == value[key] for key in ("altitude_m", "ground_speed_mps", "propellant_kg"))
            if not same and time not in separation_times:
                raise ValueError("unexplained_duplicate_time")
            # A genuine instantaneous split has two body datums; use the final
            # post-split sample, never an average or invented trajectory point.
            result[-1] = value
        else:
            result.append(value)
    return result


def _interpolate(samples: list[dict], time: float, field: str, max_gap: float = 2.1) -> float:
    index = bisect_left([item["time_s"] for item in samples], time)
    if index < len(samples) and samples[index]["time_s"] == time:
        return samples[index][field]
    if index == 0 or index == len(samples):
        raise ValueError("fit_coverage_missing")
    left, right = samples[index - 1], samples[index]
    gap = right["time_s"] - left["time_s"]
    if gap > max_gap:
        raise ValueError("fit_sample_gap_exceeded")
    return left[field] + (time - left["time_s"]) / gap * (right[field] - left[field])


def _series(samples: list[dict], points: list[dict], field: str) -> dict:
    residuals, bins = [], {}
    for point in points:
        simulated = _interpolate(samples, point["time_s"], field)
        residual = simulated - point["observed"]
        squared = residual * residual
        if not math.isfinite(squared):
            raise ValueError("residual_overflow")
        bin_id = int(point["time_s"] // 5)
        bins.setdefault(bin_id, []).append(squared)
        residuals.append(dict(point, simulated=simulated, residual=residual, bin_id=bin_id))
    means = [{"bin_id": key, "point_count": len(values), "mean_squared_residual": math.fsum(values) / len(values)}
             for key, values in sorted(bins.items())]
    if not means:
        raise ValueError("empty_fit_series")
    subsets = {}
    for name, lower, upper in (("0_to_before_140", 0, 140), ("140_to_491_inclusive", 140, 492),
                               ("480_to_491_inclusive", 480, 492)):
        selected = [r for r in residuals if lower <= r["time_s"] < upper]
        group = {}
        for row in selected:
            group.setdefault(row["bin_id"], []).append(row["residual"] ** 2)
        subsets[name] = {"point_count": len(selected), "bin_count": len(group),
                         "rmse": math.sqrt(math.fsum(math.fsum(v) / len(v) for v in group.values()) / len(group)) if group else None}
    return {"rmse": math.sqrt(math.fsum(row["mean_squared_residual"] for row in means) / len(means)),
            "point_count": len(points), "bin_count": len(means), "points": residuals, "bins": means, "subsets": subsets}


def _separation(record: Mapping) -> dict:
    state = record.get("booster_separation_state")
    if not isinstance(state, dict):
        raise ValueError("missing_actual_separation_state")
    _number(state.get("time_s"), "separation_time")
    if _number(state.get("propellant_kg"), "separation_fuel") <= 0:
        raise ValueError("separation_fuel_not_positive")
    for field, width in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        values = state.get(field)
        if not isinstance(values, (list, tuple)) or len(values) != width:
            raise ValueError(f"invalid_separation_{field}")
        for value in values:
            _number(value, field)
    if not isinstance(state.get("engine_states"), (list, tuple)):
        raise ValueError("missing_separation_actuator_history")
    return {"time_s": state["time_s"], "sha256": hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}


def verify_ascent_record(record: Mapping, *, finite_mode: bool, expected_settings: Mapping | None = None,
                         profile: Mapping | None = None) -> dict:
    """Scoped deferred-ascent record checks, independent of whole-mission success.

    Reuse the strict saved-record verifier's state/kinematic primitives rather
    than deleting the real separation event to pass its whole-mission contract.
    The only dynamics operation is observing the saved final state/vehicle; no
    simulation step, atmosphere fitting, or command execution occurs.
    """
    from . import starship_sixdof as dynamics
    from . import starship_sixdof_verifier as saved
    from . import starship_physics as geometry

    result = {"schema": "missionos.starship_ascent_record_verification.v1", "passed": False,
              "issues": [], "deferred_booster_honestly_recorded": False,
              "scope": "saved physical-only ascent consistency; not dynamics reexecution or mission qualification"}
    try:
        saved._json_tree(record)
        first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship")
        for sample in record["samples"]:
            # WGS84 altitude is unchanged by the ECI/ECEF rotation about z.
            # Recompute this fit quantity from the saved position rather than
            # trusting an edited observation field or an outcome maximum.
            altitude = geometry.ecef_to_geodetic(tuple(sample["r_eci_m"]))[2]
            saved._require(saved._near(sample["altitude_m"], altitude, 1e-5), "$.samples",
                           "Altitude disagrees with saved inertial position")
        saved._require(record.get("scenario") == "launch", "$", "Only physical launch ascent is registered")
        saved._require(saved._same_state(first, record.get("initial_state")), "$", "Initial state differs")
        saved._state(record.get("final_state"), "$.final_state")
        saved._require(saved._same_state(last, record["final_state"]), "$", "Final state differs")
        outcome = record.get("outcome", {})
        termination = outcome.get("termination")
        allowed = {"time_limit", "numerical_failure", "angular_rate_envelope_exceeded", "surface_impact", "low_speed_surface_contact"}
        if finite_mode:
            allowed |= {"ascent_reserve_exhausted", "insertion_gate_reached", "insertion_gate_unreached"}
        saved._require(termination in allowed, "$", "Unknown ascent termination")
        saved._require(termination != "numerical_failure", "$", "Numerical failure stops cohort")
        for field, value in (("six_dof_integrated", True), ("attitude_prescribed", False),
                             ("starship_vehicle_validated", False), ("booster_return_invoked", False)):
            saved._require(outcome.get(field) is value, "$.outcome", f"Wrong {field}")
        saved._require(record.get("booster_run") is None and not record.get("satellites")
                       and outcome.get("payload_released_count") == 0, "$", "Deferred ascent cannot contain mission continuations")
        saved._require(saved._near(outcome.get("duration_s"), last["time_s"] - first["time_s"])
                       and first["time_s"] == 0, "$", "Duration differs from actual trajectory")
        saved._require(type(outcome.get("integration_steps")) is int and outcome["integration_steps"] > 0,
                       "$", "Missing integration step evidence")
        saved._require(outcome.get("phase") == last["phase"], "$", "Final phase differs")
        events = saved._events(record.get("events"), "$.events", first["time_s"], last["time_s"])
        terminal_name = ("time_limit" if termination == "insertion_gate_reached" else
                         "ascent_screen_final_cutoff_command" if termination == "insertion_gate_unreached" else
                         "ascent_screen_physical_failure" if termination == "ascent_reserve_exhausted" else termination)
        terminal = events.get(terminal_name, [])
        saved._require(len(terminal) == 1 and saved._near(terminal[0]["time_s"], last["time_s"]),
                       "$", "Termination lacks matching final event")
        is_contact = termination in {"surface_impact", "low_speed_surface_contact"}
        saved._require(last.get("contact", False) is is_contact, "$", "Contact flag disagrees with physical termination")
        saved._require(not is_contact or type(outcome.get("contact_receipt")) is dict
                       and outcome["contact_receipt"].get("event_time_s") == last["time_s"],
                       "$", "Physical contact lacks actual timed receipt")
        stages = events.get("stage_separation", [])
        saved._require(len(stages) <= 1, "$", "Repeated separation")
        if stages:
            separation = _separation(record)
            saved._state(record["booster_separation_state"], "$.booster_separation_state")
            saved._require(saved._near(separation["time_s"], stages[0]["time_s"]), "$", "Separated state/event time differs")
            for field, bound in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1),
                                 ("angular_momentum_residual_kg_m2_s", 1.)):
                saved._require(saved._number(stages[0].get(field)) and abs(stages[0][field]) < bound,
                               "$", "Separation conservation receipt exceeds unchanged bound")
            result["actual_separation"] = separation
        else:
            saved._require(record.get("booster_separation_state") is None, "$", "Separation state lacks event")
            saved._require(termination in PHYSICAL_FAILURES and last["time_s"] < 491,
                           "$", "Complete ascent lacks actual separation")
        final_state = dynamics.state_from_dict(record["final_state"])
        vehicle = dynamics.vehicle_from_dict(record["final_vehicle"])
        observation = dynamics.observe(final_state, vehicle)
        observation["ground_speed_mps"] = observation.get("ground_speed_mps", observation["air_speed_mps"])
        for field in ("altitude_m", "ground_speed_mps", "mass_kg"):
            saved._require(saved._near(last.get(field), observation.get(field), 1e-5), "$", f"Final {field} disagrees with saved state/vehicle")
        for field in ("final_altitude_m", "final_ground_speed_mps"):
            saved._require(saved._near(outcome.get(field), last[field.removeprefix("final_")]), "$", "Final outcome observation differs")
        screen = record.get("ascent_screen")
        if finite_mode:
            wanted = {key: value for key, value in dict(expected_settings or {}).items() if key != "duration_s"}
            saved._require(type(screen) is dict and screen.get("settings") == wanted,
                           "$", "Finite screen settings differ from registered candidate")
            for field in ("mission_dispatch", "return_qualified", "payload_release_authorized", "physical_execution", "propulsion_reference_connected"):
                saved._require(screen.get(field) is False, "$", f"Unexpected {field} grant")
            witnesses = screen.get("transition_witnesses")
            saved._require(type(witnesses) is list, "$", "Missing transition state continuity")
            for witness in witnesses:
                saved._require(witness.get("state_reset") is False and witness.get("state_before") == witness.get("state_after"),
                               "$", "Finite transition resets actual state or actuator history")
                saved._state(witness.get("state_before"), "$.transition.state")
                saved._require(witness["time_s"] == witness["state_before"]["time_s"], "$", "Transition time differs from actual state")
            timed = (("ascent_screen_first_cutoff_command", 491.), ("ascent_screen_restart_command", 1517.),
                     ("ascent_screen_final_cutoff_command", 1536.))
            for name, time in timed:
                occurrences = events.get(name, [])
                should_exist = (last["time_s"] > time or last["time_s"] == time
                                and (termination not in PHYSICAL_FAILURES or termination == "insertion_gate_unreached"))
                saved._require(len(occurrences) == int(should_exist), "$", f"Missing or extra {name}")
                if occurrences:
                    event = occurrences[0]
                    saved._require(event["time_s"] == time and any(w["time_s"] == time for w in witnesses),
                                   "$", "Scheduled transition lacks exact-time state witness")
            final_commands = events.get("ascent_screen_final_cutoff_command", [])
            measurement = screen.get("cutoff_measurement")
            if final_commands:
                cutoff = final_commands[0]
                saved._require(type(measurement) is dict and measurement.get("time_s") == 1536.
                               and measurement.get("state") == cutoff.get("state")
                               and measurement.get("orbit") == cutoff.get("orbit")
                               and measurement.get("gate_reached") is (cutoff.get("termination") == "insertion_gate_reached"),
                               "$", "Cutoff measurement differs from actual cutoff state/event")
                point = measurement["state"]
                orbit = geometry.orbital_elements(geometry.State3D(point["time_s"], tuple(point["r_eci_m"]),
                                                   tuple(point["v_eci_mps"]), point["propellant_kg"]))
                saved._require(measurement["orbit"] == orbit, "$", "Cutoff orbit differs from actual measured state")
                if profile is None:
                    profile = _read_json(Path(__file__).resolve().parents[2] / "examples/spaceflight/starship-sixdof-profile.json")
                reached = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= profile["guidance"]["target_perigee_m"]
                saved._require(measurement["gate_reached"] is reached, "$", "Cutoff perigee gate disagrees with actual state")
                final_orbit = geometry.orbital_elements(geometry.State3D(final_state.time_s, final_state.r_eci_m,
                                                         final_state.v_eci_mps, final_state.propellant_kg))
                saved._require(screen.get("final_orbit") == final_orbit, "$", "Post-shutdown orbit differs from final actual state")
            else:
                saved._require(measurement is None, "$", "Cutoff measurement precedes actual cutoff")
            if termination == "insertion_gate_reached":
                saved._require(last["time_s"] == 1537. and outcome.get("orbit_gate_reached") is True
                               and screen.get("insertion_gate_reached") is True,
                               "$", "Reached gate lacks actual finite shutdown continuation")
        else:
            wanted = dict(expected_settings or {})
            saved._require(screen is None and not wanted.get("finite_mode", False)
                           and wanted.get("stack_pitch_offset_deg", 0.) == wanted.get("ship_pitch_offset_deg", 0.) == 0.,
                           "$", "Baseline must retain legacy default path")
        result.update(passed=True, deferred_booster_honestly_recorded=True,
                      termination=termination, physical_failure=termination in PHYSICAL_FAILURES,
                      numerical_termination=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["issues"].append({"code": "invalid_ascent_record", "detail": str(exc)})
    return result


def score_ascent(record: Mapping, reference: Mapping) -> dict:
    """Separate invalid records, pre-fit physical failure, and downstream failure.

    Complete fit coverage remains admissible when a *later* physical insertion
    fails. A finite time-limit record with missing coverage is invalid rather
    than relabeled a physical failure. No outcome is silently repaired.
    """
    result = {"schema": "missionos.starship_ascent_metrics.v1", "record_valid": False,
              "fit_admissible": False, "category": "invalid_record"}
    try:
        samples = _samples(record, True)
        outcome = record.get("outcome")
        if not isinstance(outcome, dict) or not isinstance(outcome.get("termination"), str):
            raise ValueError("missing_termination")
        termination = outcome["termination"]
        if termination == "numerical_failure" or "solver" in termination or "serialization" in termination:
            raise ValueError("numerical_or_serialization_failure")
        failure = termination in PHYSICAL_FAILURES
        result.update(termination=termination, physical_failure=failure, last_sample_time_s=samples[-1]["time_s"])
        if samples[-1]["time_s"] < 491 and failure:
            result.update(record_valid=True, category="physical_failure_before_fit",
                          reason="registered_physical_failure_prevents_complete_fit_coverage")
            return result
        if samples[0]["time_s"] > 0 or samples[-1]["time_s"] < 491:
            raise ValueError("fit_window_coverage_missing")
        if any(sample["propellant_kg"] <= 0 for sample in samples if sample["time_s"] <= 491):
            raise ValueError("nonpositive_fit_fuel_without_early_failure")
        separation = _separation(record)
        if not 0 < separation["time_s"] <= 491:
            raise ValueError("separation_outside_fit_window")
        altitude = _series(samples, reference["altitude"], "altitude_m")
        speed = _series(samples, reference["speed"], "ground_speed_mps")
        result.update(record_valid=True, fit_admissible=True, category="valid_fit",
                      altitude=altitude, speed=speed, separation=separation,
                      downstream_physical_failure=failure,
                      insertion_reached=outcome.get("orbit_gate_reached") is True,
                      qualification_or_mission_readiness_claimed=False)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result.update(reason=str(exc))
    return result


def validate_baseline(baseline: Mapping) -> dict:
    """Stop before candidates if the new-source denominator is unavailable."""
    if not baseline.get("record_valid") or not baseline.get("fit_admissible"):
        raise ValueError("baseline_fit_not_admissible")
    ba = _number(baseline["altitude"]["rmse"], "baseline_altitude_rmse")
    bs = _number(baseline["speed"]["rmse"], "baseline_speed_rmse")
    if ba <= 0 or bs <= 0:
        raise ValueError("baseline_positive_rmse_required")
    return {"altitude_rmse_m": ba, "speed_rmse_mps": bs, "passed": True}


def select_candidate(baseline: Mapping, candidates: Mapping[str, Mapping], registration: Mapping | None = None) -> dict:
    """Rank only frozen fit metrics, using all four gates and registered ID ties."""
    if registration is None:
        registration = _read_json(Path(__file__).resolve().parents[2] / REGISTRATION)
    order = [candidate["id"] for candidate in registration["candidate_grid"]]
    if set(candidates) != set(order):
        raise ValueError("complete_registered_candidate_grid_required")
    values = validate_baseline(baseline)
    ba, bs = values["altitude_rmse_m"], values["speed_rmse_mps"]
    gate = registration["metrics"]["calibration_gate"]
    rows, selected = [], None
    for candidate_id in order:
        metrics = candidates[candidate_id]
        if not metrics.get("record_valid"):
            raise ValueError(f"invalid_candidate_stops_cohort:{candidate_id}")
        row = {"id": candidate_id, "fit_admissible": metrics.get("fit_admissible") is True,
               "passes_all_four_gates": False, "score": None}
        if row["fit_admissible"]:
            a = _number(metrics["altitude"]["rmse"], "candidate_altitude_rmse")
            s = _number(metrics["speed"]["rmse"], "candidate_speed_rmse")
            if a < 0 or s < 0:
                raise ValueError("negative_candidate_rmse")
            ratios = [a / ba, s / bs]
            checks = {"absolute_altitude": a <= gate["maximum_altitude_rmse_m"],
                      "absolute_speed": s <= gate["maximum_speed_rmse_mps"],
                      "relative_altitude": ratios[0] <= gate["maximum_altitude_rmse_fraction_of_baseline"],
                      "relative_speed": ratios[1] <= gate["maximum_speed_rmse_fraction_of_baseline"]}
            row.update(altitude_rmse_m=a, speed_rmse_mps=s, altitude_ratio=ratios[0], speed_ratio=ratios[1],
                       gates=checks, passes_all_four_gates=all(checks.values()), score=.5 * sum(ratios))
            if row["passes_all_four_gates"] and (selected is None or row["score"] < selected["score"]):
                selected = row
        rows.append(row)
    return {"schema": "missionos.starship_ascent_selection.v1", "selected_id": selected["id"] if selected else None,
            "selected_score": selected["score"] if selected else None, "candidates": rows,
            "basis": "fit_only_all_four_gates_registered_id_ties", "insertion_or_booster_used_for_ranking": False}


def score_booster(record: Mapping, registration: Mapping | None = None) -> dict:
    """Score sampled apex and first descending 40km crossing, never extrapolate."""
    if registration is None:
        registration = _read_json(Path(__file__).resolve().parents[2] / REGISTRATION)
    result = {"schema": "missionos.starship_booster_nonfit_metrics.v1", "record_valid": False,
              "joint_numeric_gates_passed": False, "recovery_accepted": False}
    try:
        samples = _samples(record, False)
        outcome = record.get("outcome", {})
        termination = outcome.get("termination")
        if not isinstance(termination, str) or termination == "numerical_failure" or "solver" in termination:
            raise ValueError("missing_or_numerical_termination")
        contact_index = next((index for index, item in enumerate(samples) if item["contact"]), None)
        # Include the bracketed contact endpoint for a descending crossing but
        # exclude it from the pre-contact sampled peak.
        prefix = samples[:contact_index] if contact_index is not None else samples
        if not prefix:
            raise ValueError("missing_precontact_samples")
        peak_index = max(range(len(prefix)), key=lambda index: prefix[index]["altitude_m"])
        peak = prefix[peak_index]
        crossing = None
        trace = samples[:contact_index + 1] if contact_index is not None else samples
        for left, right in zip(trace[peak_index:], trace[peak_index + 1:]):
            if left["altitude_m"] >= 40_000 >= right["altitude_m"] and right["altitude_m"] < left["altitude_m"]:
                fraction = (left["altitude_m"] - 40_000) / (left["altitude_m"] - right["altitude_m"])
                crossing = left["time_s"] + fraction * (right["time_s"] - left["time_s"])
                break
        checks = registration["non_fitted_booster_check"]
        integrated_max = _number(outcome.get("max_altitude_m"), "integrated_max_altitude_m")
        if integrated_max < max(item["altitude_m"] for item in samples) - 1e-5:
            raise ValueError("integrated_max_below_saved_samples")
        apex_residual = integrated_max - checks["apex_gate"]["target_m"]
        apex_passed = abs(apex_residual) <= checks["apex_gate"]["maximum_absolute_residual_m"]
        lower, upper = checks["descending_40000m_gate"]["reference_elapsed_interval_s"]
        distance = max(lower - crossing, crossing - upper, 0.) if crossing is not None else None
        crossing_passed = distance is not None and distance <= checks["descending_40000m_gate"]["maximum_distance_to_reference_interval_s"]
        resolved = contact_index is not None and termination == "surface_contact"
        result.update(record_valid=True, termination=termination, physical_return_resolved=resolved,
                      sampled_apex_altitude_m=peak["altitude_m"], sampled_apex_time_s=peak["time_s"],
                      integrated_max_altitude_m=integrated_max, apex_absolute_residual_m=abs(apex_residual), apex_gate_passed=apex_passed,
                      apex_gate_basis="source_bound_every_integration_step_maximum_stored_in_outcome_not_independently_reintegrated",
                      apex_resolution={"method": "maximum_precontact_recorded_sample_no_peak_fit",
                                       "previous_time_s": prefix[peak_index - 1]["time_s"] if peak_index else None,
                                       "next_time_s": prefix[peak_index + 1]["time_s"] if peak_index + 1 < len(prefix) else None},
                      descending_40000m_elapsed_s=crossing, crossing_interval_distance_s=distance,
                      descending_gate_passed=crossing_passed,
                      joint_numeric_gates_passed=apex_passed and crossing_passed and resolved,
                      recovery_accepted=resolved and outcome.get("simulated_contact_envelope_met") is True,
                      contact_time_s=samples[contact_index]["time_s"] if contact_index is not None else None,
                      recovery_and_fidelity_are_separate=True,
                      numeric_match_is_not_physical_or_real_vehicle_validation=True)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result.update(reason=str(exc))
    return result
