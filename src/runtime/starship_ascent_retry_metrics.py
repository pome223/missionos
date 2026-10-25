"""Saved-record checks for the separately registered final Stage 2b retry.

No plant step, provider call, authority grant, or modification of the immutable
v2 experiment is performed here. The original full 0..491 observation masks
and four fit gates remain the ranking criteria after an earlier actual cutoff.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Mapping

from . import starship_ascent_metrics as v2


REGISTRATION = "examples/spaceflight/starship-ascent-path-registration-v3.json"
POLICY = "guidance_outcome_v3"
EXTRA_PHYSICAL_FAILURES = frozenset({"ascent_time_limit_exceeded", "ascent_propulsion_unavailable"})
PHYSICAL_FAILURES = v2.PHYSICAL_FAILURES | EXTRA_PHYSICAL_FAILURES
validate_baseline = v2.validate_baseline
score_booster = v2.score_booster


def load_reference(root: str | Path, registration: Mapping | None = None) -> dict:
    """Reuse the accepted v2 masks, then bind the new immutable registration."""
    root = Path(root)
    registration = registration or v2._read_json(root / REGISTRATION)
    reference = v2.load_reference(root)
    for relative, expected in registration.get("input_sha256", {}).items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"retry_input_hash_mismatch:{relative}")
    reference["registration"] = dict(registration)
    reference["retry_preserves_complete_accepted_masks"] = True
    return reference


def _orbit(state, geometry):
    return geometry.orbital_elements(geometry.State3D(state["time_s"], tuple(state["r_eci_m"]),
                                    tuple(state["v_eci_mps"]), state["propellant_kg"]))


def _main_kind(phase):
    return "booster" if phase == "stack_ascent" else "ship"


def _availability(state, kind, schedule, *, before_event=False):
    main_count = 33 if kind == "booster" else 6
    engines = state.get("engine_states")
    if not isinstance(engines, list) or len(engines) < main_count:
        raise ValueError("missing_actual_main_engine_history")
    time = state["time_s"]
    unavailable = {row["engine_name"] for row in schedule if row["time_s"] < time
                   or row["time_s"] == time and not before_event}
    for index, engine in enumerate(engines[:main_count]):
        if type(engine.get("available")) is not bool:
            raise ValueError("engine_availability_must_be_boolean")
        expected = f"{kind}_main_{index}" not in unavailable
        if engine["available"] is not expected:
            raise ValueError("engine_availability_disagrees_with_registered_schedule")


def _shutdowns(record, screen, schedule, last_time, saved):
    if (not isinstance(schedule, list) or len(schedule) > 2
            or len({row.get("engine_name") for row in schedule}) != len(schedule)
            or len({row.get("engine_name", "").split("_", 1)[0] for row in schedule}) != len(schedule)
            or len({row.get("time_s") for row in schedule}) != len(schedule)):
        raise ValueError("invalid_registered_shutdown_schedule")
    if [row["time_s"] for row in schedule] != sorted(row["time_s"] for row in schedule):
        raise ValueError("unordered_shutdown_schedule")
    for row in schedule:
        time = v2._number(row.get("time_s"), "shutdown_time")
        engine = row.get("engine_name")
        allowed = {f"booster_main_{i}" for i in range(33)} | {f"ship_main_{i}" for i in (3, 4, 5)}
        if set(row) != {"engine_name", "time_s"} or engine not in allowed or not 0 < time < 800:
            raise ValueError("unregistered_engine_shutdown")
    expected = [row for row in schedule if row["time_s"] <= last_time]
    receipts = screen.get("shutdown_receipts")
    events = [event for event in record["events"] if event["event"] == "ascent_screen_engine_unavailable"]
    saved._require(type(receipts) is list and len(receipts) == len(expected) == len(events),
                   "$", "Reached shutdown schedule differs from receipts/events")
    for specification, receipt, event in zip(expected, receipts, events):
        time, name = specification["time_s"], specification["engine_name"]
        saved._require(receipt.get("engine_name") == name
                       and receipt.get("scheduled_at_s") == receipt.get("applied_at_s") == event["time_s"] == time
                       and receipt.get("engine_id_classification") == "registered_representative_not_operator_engine_identifier"
                       and event.get("shutdown") == receipt, "$", "Outage identity/time/provenance differs")
        before, after = event.get("state_before"), event.get("state_after")
        saved._state(before, "$.shutdown.before")
        saved._state(after, "$.shutdown.after")
        saved._require(before["time_s"] == after["time_s"] == time, "$", "Outage changes physical time")
        index = int(name.rsplit("_", 1)[1])
        if len(before.get("engine_states", [])) <= index:
            raise ValueError("outage_index_missing_from_actual_state")
        expected_after = deepcopy(before)
        saved._require(expected_after["engine_states"][index].get("available") is True,
                       "$", "Outage target was already unavailable")
        expected_after["engine_states"][index]["available"] = False
        saved._require(after == expected_after, "$", "Outage changed spool, pose, fuel, rates or another engine")
        saved._require(receipt.get("engine_state_before") == before["engine_states"][index]
                       and receipt.get("engine_state_after") == after["engine_states"][index],
                       "$", "Outage receipt differs from actual actuator state")
        kind = name.split("_", 1)[0]
        _availability(before, kind, schedule, before_event=True)
        _availability(after, kind, schedule)
    for sample in record["samples"]:
        _availability(sample, _main_kind(sample["phase"]), schedule)
    separation = record.get("booster_separation_state")
    if separation is not None:
        _availability(separation, "booster", schedule)
    _availability(record["final_state"], _main_kind(record["outcome"]["phase"]), schedule)


def verify_ascent_record(record: Mapping, *, finite_mode: bool = True,
                         expected_settings: Mapping | None = None, profile: Mapping | None = None,
                         shutdown_schedule: list | None = None) -> dict:
    """Verify the actual retry states and commands without running a flight.

    All v3 cases, including B0, use the same outage/guidance-outcome mode. Only
    the two registered offsets differ. Engine IDs are representative simulator
    choices; the public display establishes no corresponding physical ID.
    """
    from . import starship_sixdof as dynamics
    from . import starship_physics as geometry
    from . import starship_sixdof_verifier as saved

    result = {"schema": "missionos.starship_ascent_retry_record_verification.v1", "passed": False,
              "issues": [], "scope": "stored retry state/observation/schedule consistency; no dynamics reexecution or vehicle qualification"}
    try:
        saved._json_tree(record)
        first, last = saved._samples(record.get("samples"), "$.samples", [0], "ship")
        saved._require(finite_mode is True and record.get("scenario") == "launch", "$", "Retry requires explicit physical launch screen")
        saved._require(first["time_s"] == 0 and saved._same_state(first, record.get("initial_state")), "$", "Initial state differs")
        saved._state(record.get("final_state"), "$.final_state")
        saved._require(saved._same_state(last, record["final_state"]), "$", "Final state differs")
        for sample in record["samples"]:
            altitude = geometry.ecef_to_geodetic(tuple(sample["r_eci_m"]))[2]
            saved._require(saved._near(altitude, sample["altitude_m"], 1e-5), "$", "Altitude differs from actual position")
        outcome = record.get("outcome", {})
        termination = outcome.get("termination")
        allowed = {"time_limit", "angular_rate_envelope_exceeded", "surface_impact", "low_speed_surface_contact",
                   "ascent_reserve_exhausted", "insertion_gate_reached", "insertion_gate_unreached"} | EXTRA_PHYSICAL_FAILURES
        saved._require(termination in allowed, "$", "Unknown or numerical retry termination")
        for key, value in (("six_dof_integrated", True), ("attitude_prescribed", False),
                           ("starship_vehicle_validated", False), ("booster_return_invoked", False)):
            saved._require(outcome.get(key) is value, "$", f"Incorrect {key}")
        saved._require(record.get("booster_run") is None and not record.get("satellites")
                       and outcome.get("payload_released_count") == 0, "$", "Retry cannot dispatch mission continuations")
        saved._require(saved._near(outcome.get("duration_s"), last["time_s"])
                       and outcome.get("phase") == last["phase"]
                       and type(outcome.get("integration_steps")) is int and outcome["integration_steps"] > 0,
                       "$", "Duration, phase or step evidence differs")
        events = saved._events(record.get("events"), "$.events", 0., last["time_s"])
        terminal_name = ("time_limit" if termination == "insertion_gate_reached" else
                         "ascent_screen_final_cutoff_command" if termination == "insertion_gate_unreached" else
                         "ascent_screen_physical_failure" if termination in EXTRA_PHYSICAL_FAILURES | {"ascent_reserve_exhausted"} else termination)
        terminal = events.get(terminal_name, [])
        saved._require(len(terminal) == 1 and saved._near(terminal[0]["time_s"], last["time_s"]), "$", "Termination lacks final physical event")
        contact = termination in {"surface_impact", "low_speed_surface_contact"}
        saved._require(last.get("contact", False) is contact, "$", "Contact flag differs from termination")
        if contact:
            receipt = outcome.get("contact_receipt")
            saved._require(type(receipt) is dict and receipt.get("event_time_s") == last["time_s"], "$", "Contact lacks timed physical receipt")
        stages = events.get("stage_separation", [])
        saved._require(len(stages) <= 1, "$", "Repeated actual separation")
        if stages:
            separation = v2._separation(record)
            saved._state(record["booster_separation_state"], "$.booster_separation_state")
            saved._require(saved._near(stages[0]["time_s"], separation["time_s"]), "$", "Actual separation event/state differs")
            for key, bound in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1),
                               ("angular_momentum_residual_kg_m2_s", 1.)):
                saved._require(saved._number(stages[0].get(key)) and abs(stages[0][key]) < bound, "$", "Separation conservation receipt exceeds unchanged bound")
            result["actual_separation"] = separation
        else:
            saved._require(record.get("booster_separation_state") is None and termination in PHYSICAL_FAILURES
                           and last["time_s"] < 491, "$", "Missing actual separation")
        final = dynamics.state_from_dict(record["final_state"])
        vehicle = dynamics.vehicle_from_dict(record["final_vehicle"])
        observed = dynamics.observe(final, vehicle)
        observed["ground_speed_mps"] = observed.get("ground_speed_mps", observed["air_speed_mps"])
        for key in ("altitude_m", "ground_speed_mps", "mass_kg"):
            saved._require(saved._near(last.get(key), observed[key], 1e-5), "$", "Final observation differs from actual state/vehicle")
        for key in ("altitude_m", "ground_speed_mps"):
            saved._require(saved._near(outcome.get("final_" + key), last[key]), "$", "Final outcome differs from saved observation")
        screen = record.get("ascent_screen")
        wanted = {key: value for key, value in dict(expected_settings or {}).items() if key != "duration_s"}
        saved._require(type(screen) is dict and screen.get("policy") == POLICY
                       and screen.get("settings") == wanted and wanted.get("policy") == POLICY
                       and wanted.get("finite_mode") is True, "$", "Retry settings/policy differ from registration")
        expected_duration = dict(expected_settings or {}).get("duration_s", 1537.)
        saved._require(expected_duration == 1537. and last["time_s"] <= expected_duration
                       and (termination != "time_limit" or last["time_s"] == expected_duration),
                       "$", "Retry record horizon differs from registered1537-second screen")
        schedule = wanted.get("shutdowns") if shutdown_schedule is None else shutdown_schedule
        saved._require(schedule == wanted.get("shutdowns") == screen.get("shutdown_schedule"), "$", "Outage schedule differs from registered settings")
        for key in ("mission_dispatch", "return_qualified", "payload_release_authorized", "physical_execution", "propulsion_reference_connected"):
            saved._require(screen.get(key) is False, "$", "Retry cannot grant mission/qualified-return authority")
        _shutdowns(record, screen, schedule, last["time_s"], saved)
        witnesses = screen.get("transition_witnesses")
        saved._require(type(witnesses) is list, "$", "Missing transition continuity")
        for witness in witnesses:
            saved._require(witness.get("state_reset") is False
                           and witness.get("state_before") == witness.get("state_after")
                           and witness.get("state_after_origin") in ("next_physics_tick_before_command", "final_retained_state"),
                           "$", "Transition resets state or lacks actual next-tick witness")
            saved._state(witness.get("state_before"), "$.transition.state")
            saved._require(witness["time_s"] == witness["state_before"]["time_s"], "$", "Transition time differs")
        if profile is None:
            profile = v2._read_json(Path(__file__).resolve().parents[2] / "examples/spaceflight/starship-sixdof-profile.json")
        first_cutoff = screen.get("first_cutoff_measurement")
        first_commands = events.get("ascent_screen_first_cutoff_command", [])
        saved._require(len(first_commands) <= 1 and bool(first_commands) == (first_cutoff is not None), "$", "Actual first cutoff receipt/event differs")
        if first_commands:
            event = first_commands[0]
            saved._require(first_cutoff.get("time_s") == event["time_s"] and first_cutoff.get("state") == event.get("state")
                           and first_cutoff.get("reason") == "measured_bound_orbit_perigee_gate", "$", "First cutoff is not the measured guidance gate")
            orbit = _orbit(first_cutoff["state"], geometry)
            saved._require(first_cutoff.get("orbit") == orbit and orbit["status"] == "bound"
                           and orbit["perigee_altitude_m"] >= profile["guidance"]["target_perigee_m"]
                           and first_cutoff["state"]["propellant_kg"] > profile["ship"]["ascent_reserve_kg"],
                           "$", "First cutoff lacks measured perigee/fuel headroom")
            saved._require(any(w["time_s"] == event["time_s"] and w["from_phase"] == "ship_ascent"
                               and w["to_phase"] == "suborbital_coast" for w in witnesses), "$", "Actual first cutoff lacks continuity witness")
        else:
            maximum_ascent_time = profile["guidance"]["max_ascent_s"]
            final_powered_step = profile.get("integration", {}).get("powered_dt_s", .2)
            saved._require(termination in PHYSICAL_FAILURES and last["time_s"] <= maximum_ascent_time + final_powered_step + 1e-7,
                           "$", "Ascent continued without an actual guidance cutoff or physical stop")
        for name, time in (("ascent_screen_restart_command", 1517.), ("ascent_screen_final_cutoff_command", 1536.)):
            found = events.get(name, [])
            if last["time_s"] > time:
                saved._require(len(found) == 1, "$", "Missing finite restart/final cutoff before later actual state")
            elif last["time_s"] < time:
                saved._require(not found, "$", "Finite restart/final cutoff precedes actual boundary")
            else:
                # At the exact boundary a reserve stop can precede transition,
                # while a rate-envelope stop can follow its command before any
                # plant advance. Preserve that actual event order, not a rule
                # inferred solely from the final termination label.
                before_transition_stops = {"ascent_reserve_exhausted", "surface_impact", "low_speed_surface_contact"}
                saved._require(len(found) <= 1 and (found or termination in before_transition_stops),
                               "$", "Exact finite boundary lacks transition or physical stop")
            if found:
                saved._require(found[0]["time_s"] == time and any(w["time_s"] == time
                               and w["state_before"] == found[0].get("state") for w in witnesses),
                               "$", "Finite boundary lacks actual exact-time state")
        cutoff = screen.get("cutoff_measurement")
        final_commands = events.get("ascent_screen_final_cutoff_command", [])
        if final_commands:
            event = final_commands[0]
            saved._require(type(cutoff) is dict and cutoff.get("time_s") == 1536.
                           and cutoff.get("state") == event.get("state") and cutoff.get("orbit") == event.get("orbit"), "$", "Final cutoff measurement differs from event")
            orbit = _orbit(cutoff["state"], geometry)
            reached = orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= profile["guidance"]["target_perigee_m"]
            saved._require(cutoff["orbit"] == orbit and cutoff.get("gate_reached") is reached, "$", "Final cutoff gate differs from measured orbit")
            saved._require(event.get("termination") == ("insertion_gate_reached" if reached else "insertion_gate_unreached"),
                           "$", "Final cutoff event classification differs from actual gate")
            if not reached:
                saved._require(termination == "insertion_gate_unreached" and last["time_s"] == 1536.,
                               "$", "Failed final gate must stop at actual cutoff state")
        else:
            saved._require(cutoff is None, "$", "Final cutoff measurement before command")
        gate_reached = cutoff is not None and cutoff.get("gate_reached") is True
        saved._require(outcome.get("orbit_gate_reached") is gate_reached
                       and screen.get("insertion_gate_reached") is gate_reached,
                       "$", "Insertion claim differs from actual final-cutoff gate")
        saved._require(screen.get("final_orbit") == _orbit(record["final_state"], geometry), "$", "Final orbit differs from actual state")
        if termination == "insertion_gate_reached":
            saved._require(last["time_s"] == 1537. and cutoff is not None and cutoff.get("gate_reached") is True
                           and outcome.get("orbit_gate_reached") is True and screen.get("insertion_gate_reached") is True,
                           "$", "Insertion claim lacks actual shutdown continuation")
        if termination == "ascent_reserve_exhausted":
            saved._require(record["final_state"]["propellant_kg"] <= profile["ship"]["ascent_reserve_kg"], "$", "Reserve-protection stop lacks actual reserve crossing")
        if termination == "ascent_time_limit_exceeded":
            saved._require(last["time_s"] + 1e-7 >= profile["guidance"]["max_ascent_s"],
                           "$", "Ascent time-limit stop precedes actual time limit")
        result.update(passed=True, termination=termination, physical_failure=termination in PHYSICAL_FAILURES,
                      deferred_booster_honestly_recorded=True, no_491_cutoff_timing_gate=True,
                      measured_final_insertion_gate_reached=gate_reached,
                      shutdown_continuation_completed=termination == "insertion_gate_reached",
                      first_cutoff_time_s=first_cutoff["time_s"] if first_cutoff else None)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["issues"].append({"code": "invalid_ascent_retry_record", "detail": str(exc)})
    return result


def cutoff_timing_diagnostic(record: Mapping, observed_interval=None, tolerance_s=5.) -> dict:
    cutoff = record.get("ascent_screen", {}).get("first_cutoff_measurement")
    time = cutoff["time_s"] if cutoff else None
    result = {"used_for_ranking": False, "actual_cutoff_time_s": time, "planned_anchor_s": 491.,
              "difference_from_plan_s": time - 491. if time is not None else None,
              "simulator_quantity": "first_cutoff_command_not_physical_spool_completion",
              "plan_is_observed_event": False, "observed_gate_evaluated": False, "observed_gate_passed": None}
    previous = time
    shutdown_sample = {"method": "first_post_command_sample_with_nonempty_available_main_throttles_below_1pct",
                       "used_for_ranking": False, "time_s": None, "preceding_sample_time_s": None,
                       "sample_bracket_width_s": None, "available_main_count": None,
                       "physical_spool_completion_time_established": False}
    if time is not None:
        for sample in record.get("samples", []):
            if sample["time_s"] <= time:
                continue
            available = [engine["throttle"] for engine in sample.get("engine_states", [])[:6]
                         if engine.get("available") is True]
            if available and max(available) < .01:
                shutdown_sample.update(time_s=sample["time_s"], preceding_sample_time_s=previous,
                                       sample_bracket_width_s=sample["time_s"] - previous,
                                       available_main_count=len(available))
                break
            previous = sample["time_s"]
    result["available_main_throttle_below_1pct"] = shutdown_sample
    if observed_interval is not None:
        if not isinstance(observed_interval, (list, tuple)) or len(observed_interval) != 2:
            raise ValueError("registered_observed_cutoff_interval_required")
        lower, upper = (v2._number(value, "observed_cutoff_interval") for value in observed_interval)
        tolerance = v2._number(tolerance_s, "observed_cutoff_tolerance")
        if lower > upper or tolerance < 0:
            raise ValueError("invalid_observed_cutoff_interval")
        distance = max(lower - time, time - upper, 0.) if time is not None else None
        result.update(observed_gate_evaluated=True, observed_interval_s=[lower, upper],
                      observed_interval_distance_s=distance, observed_gate_passed=distance is not None and distance <= tolerance,
                      observed_comparison_class="RVac_display_cessation_engineering_proxy_not_all_main_engine_cutoff",
                      matching_this_proxy_is_not_vehicle_validation=True)
    return result


def score_ascent(record: Mapping, reference: Mapping) -> dict:
    """Keep the complete accepted fit; an early burn-only subset is diagnostic."""
    result = v2.score_ascent(record, reference)
    termination = record.get("outcome", {}).get("termination")
    if termination in EXTRA_PHYSICAL_FAILURES:
        result["physical_failure"] = True
        if result.get("last_sample_time_s", 491.) < 491 and result.get("reason") == "fit_window_coverage_missing":
            result.update(record_valid=True, category="physical_failure_before_fit", fit_admissible=False,
                          reason="registered_physical_failure_prevents_complete_fit_coverage")
        if result.get("fit_admissible"):
            result["downstream_physical_failure"] = True
    result["retry_preserves_full_0_to_491_ranking_window"] = True
    result["measured_final_insertion_gate_reached"] = record.get("outcome", {}).get("orbit_gate_reached") is True
    result["shutdown_continuation_completed"] = termination == "insertion_gate_reached"
    timing_registration = reference.get("registration", {}).get("non_fitted_ascent_cutoff_check", {})
    result["cutoff_timing"] = cutoff_timing_diagnostic(record, timing_registration.get("observed_interval_s"),
        timing_registration.get("maximum_distance_to_interval_s", 5.))
    cutoff = record.get("ascent_screen", {}).get("first_cutoff_measurement")
    if cutoff is not None and result.get("record_valid"):
        samples = v2._samples(record, True)
        end = min(491., cutoff["time_s"])
        diagnostic = {"used_for_ranking": False, "end_time_s": end, "mask": "accepted_points_at_or_before_actual_first_cutoff_only"}
        for name, field in (("altitude", "altitude_m"), ("speed", "ground_speed_mps")):
            points = [point for point in reference[name] if point["time_s"] <= end]
            diagnostic[name] = v2._series(samples, points, field) if points else None
        result["burn_to_cutoff_diagnostic"] = diagnostic
    return result


def select_candidate(baseline: Mapping, candidates: Mapping[str, Mapping], registration: Mapping | None = None) -> dict:
    registration = registration or v2._read_json(Path(__file__).resolve().parents[2] / REGISTRATION)
    result = v2.select_candidate(baseline, candidates, registration)
    result["burn_to_cutoff_or_planned_timing_used_for_ranking"] = False
    return result


def choose_stage2c_seed(selection: Mapping, baseline: Mapping, candidates: Mapping[str, Mapping], *,
                       baseline_carry_forward_authorized: bool = False) -> dict:
    """A registered B0 continuation is distinct from passing the ascent fit."""
    selected = selection.get("selected_id")
    if selected is not None:
        metric = candidates[selected]
        eligible = [row for row in selection.get("candidates", []) if row.get("id") == selected
                    and row.get("passes_all_four_gates") is True]
        if not metric.get("record_valid") or not metric.get("fit_admissible") or len(eligible) != 1:
            raise ValueError("continuation_fit_claim_requires_actual_four_gate_selection")
        return {"case_id": selected, "fit_pass": True, "basis": "registered_four_gate_fit_selection",
                "separation": metric["separation"], "qualified_or_mission_ready": False}
    if baseline_carry_forward_authorized is not True:
        raise ValueError("negative_fit_baseline_continuation_requires_explicit_registered_authority")
    validate_baseline(baseline)
    return {"case_id": "B0", "fit_pass": False, "basis": "explicit_baseline_continuation_after_negative_fit",
            "separation": baseline["separation"], "qualified_or_mission_ready": False,
            "negative_fit_preserved": True}
