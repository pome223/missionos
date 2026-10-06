"""Independent saved-record checks; no guidance/integrator or forecast execution."""
from __future__ import annotations

from . import starship_booster_recovery_verifier as c
from .starship_capture_diagnostics import gate_margins
from .starship_landing_context import request


def verify_forecast(run, snapshot, profile, config, *, delay_s, duration_s):
    result = {"schema": "missionos.starship_landing_forecast_verification.v1", "passed": False,
              "predicted_handoff_eligible": False, "issues": [], "prediction_is_execution": False,
              "catch_supported": False, "mission_completed": False, "physical_execution": False,
              "forecast_dynamics_reexecuted": False, "production_policy_admitted": False}
    try:
        c._json(run)
        c._json(snapshot)
        c._profile_and_config(profile, config)
        expected = request(snapshot, profile, config, delay_s, duration_s)
        record, outcome = run["recovery_record"], run["outcome"]
        c._require(run.get("scenario") == "booster_return" and run.get("body_id") == "booster"
                   and run.get("guidance_policy") == c._POLICY and record.get("policy_id") == c._POLICY
                   and record.get("schema") == "missionos.starship_booster_recovery.v1"
                   and record.get("guidance_configuration") == c._GUIDANCE
                   and record.get("forecast_only") is True and record.get("landing_start_forecast") == expected
                   and record.get("development_cutoff") is None and record.get("development_fin_allocation") is None
                   and record.get("development_landing_probe") is None
                   and record.get("full_coast_prediction_count") == 0 and record.get("cutoff_predictions") == [],
                   "forecast_binding", "Only the exact forecast request, context and fixed policy may be checked")
        forbidden = ("physical_execution", "physical_execution_invoked", "catch_verified", "mission_completed",
                     "launch_connected", "launch_connected_catch_supported", "state_reset", "attitude_prescribed",
                     "starship_vehicle_validated", "landing_hardware_validated", "simulated_catch_supported",
                     "missionos_dispatch", "dispatch_authority_created", "execution_authorized", "prediction_is_execution",
                     "production_policy_admitted", "model_value_demonstrated")
        for section in (run, record, outcome):
            c._require(all(section.get(k, False) is False for k in forbidden), "claim_boundary", "Forecast cannot claim execution or support")
        c._require(record.get("physical_execution") is False and record.get("catch_verified") is False
                   and outcome.get("six_dof_integrated") is True and outcome.get("booster_return_6dof_implemented") is True
                   and outcome.get("orbit_gate_reached", False) is False
                   and type(outcome.get("payload_released_count", 0)) is int and outcome.get("payload_released_count", 0) == 0,
                   "claim_boundary", "Expected finite six-DOF forecast")
        initial = snapshot["state"]
        for item in (run["booster_separation_state"], run["initial_state"], record["input_separation_state"]):
            c._same_state(initial, item, "origin_binding")
        points, samples = record["checkpoints"], run["samples"]
        c._require(type(points) is list and 1 <= len(points) <= 1000 and len(points) == len(samples),
                   "record", "Bounded forecast checkpoints required")
        phases = ("recovery_entry_coast", "recovery_landing_13", "recovery_landing_5", "recovery_landing_3")
        previous, phase_index = None, 0
        for i, (point, sample) in enumerate(zip(points, samples)):
            state, phase = point["state"], point["phase"]
            c._state(state, profile)
            c._require(sample.get("body_id") == "booster" and phase in phases and phases.index(phase) >= phase_index and sample.get("phase") == phase
                       and point["time_s"] == state["time_s"], "phase", "Forecast clock or phase went backwards")
            phase_index = phases.index(phase)
            c._same_state(state, sample, "sample_binding")
            c._command(point["command"], phase, state, profile)
            c._trim_diagnostics(point.get("navigation"), profile)
            c._require(i == len(points)-1 or point["command"] is not None and sample["command"] == point["command"],
                       "command_binding", "Missing intermediate finite command")
            mass, com, rate, _ = c._flow_and_centroid(state, profile)
            c._require(c._near(sample["mass_kg"], mass) and c._near(sample["com_z_m"], com[2]),
                       "mass_properties", "Forecast mass must follow actual fuel")
            c._compare_vector(point["com_rate_body_mps"], rate, "mass_properties")
            c._compare_vector(sample["com_rate_body_mps"], rate, "mass_properties")
            if previous is None:
                c._same_state(initial, state, "origin_binding")
            else:
                c._continuity(previous, state, profile)
            previous = state
        final, start = points[-1]["state"], initial["time_s"]
        c._same_state(final, run["final_state"], "final_binding")
        elapsed = final["time_s"]-start
        ground_speed = c._norm(c._sub(final["v_eci_mps"], c._cross([0., 0., c._ROTATION], final["r_eci_m"])))
        c._require(c._near(outcome.get("final_ground_speed_mps"), ground_speed)
                   and c._near(samples[-1].get("ground_speed_mps"), ground_speed), "outcome", "Terminal speed must bind the physical velocity")
        c._require(0 <= elapsed <= duration_s+1e-8 and final["time_s"] <= snapshot["deadline_s"]+1e-8
                   and record["requested_duration_s"] == duration_s and record["resolved_duration_s"] == duration_s
                   and outcome["start_time_s"] == start and outcome["end_time_s"] == final["time_s"]
                   and c._near(outcome["duration_s"], elapsed) and outcome["phase"] == points[-1]["phase"],
                   "forecast_budget", "Forecast cannot reset or extend its inherited deadline")
        steps = outcome["integration_steps"]
        c._require(type(steps) is int and len(points)-1 <= steps <= 901 and steps*.25+1e-7 >= elapsed,
                   "forecast_budget", "Forecast exceeded its step budget")
        events, previous_t, stages = run["events"], start, []
        for event in events:
            t, name = event["time_s"], event["event"]
            c._require(previous_t <= t <= final["time_s"] and name in {
                "booster_return_start", "landing_stage_requested", "catch_handoff", "surface_contact", "time_limit", "angular_rate_envelope_exceeded"},
                "events", "Unordered or unsupported forecast event")
            previous_t = t
            if name == "landing_stage_requested":
                stages.append(event["requested_engine_count"])
                if delay_s is not None:
                    c._require(t+1e-8 >= start+delay_s, "events", "Burn requested before the declared waiting time")
        termination = outcome["termination"]
        c._require(stages in ([], [13], [13, 5], [13, 5, 3])
                   and events[0]["event"] == "booster_return_start" and events[0]["time_s"] == start
                   and termination in {"catch_handoff", "surface_contact", "time_limit", "angular_rate_envelope_exceeded"}
                   and events[-1]["event"] == termination and events[-1]["time_s"] == final["time_s"],
                   "events", "Forecast stages and terminal event differ")
        if delay_s is not None:
            requests = [e for e in events if e["event"] == "landing_stage_requested"]
            # .25 s is the largest macrostep in this fixed policy; the frozen
            # low-altitude cases use .1 s. A forecast cannot silently turn an
            # exact scheduled request into an indefinitely deferred action.
            if requests:
                c._require(requests[0]["requested_engine_count"] == 13
                           and start+delay_s-1e-8 <= requests[0]["time_s"] <= start+delay_s+.25000001,
                           "scheduled_burn", "First burn request must occur at the scheduled time within one maximum macrostep")
            else:
                c._require(termination in ("surface_contact", "angular_rate_envelope_exceeded")
                           and final["time_s"] < start+delay_s, "scheduled_burn", "Missing scheduled burn request after its due time")
        for point in points:
            if point["phase"].startswith("recovery_landing_"):
                stage = int(point["phase"].rsplit("_", 1)[1])
                c._require(any(e["event"] == "landing_stage_requested" and e["requested_engine_count"] == stage
                              and e["time_s"] <= point["time_s"] for e in events), "events", "Stage command lacks an earlier request")
        c._require(termination != "time_limit" or c._near(elapsed, duration_s), "forecast_budget", "Truncated horizon")
        c._require(termination != "angular_rate_envelope_exceeded" or c._norm(final["omega_body_rad_s"]) > 5,
                   "outcome", "Body-rate exit needs a measured exceeded bound")
        c._require((type(run["contact"]) is dict and run["contact"].get("contact") is True) if termination == "surface_contact"
                   else run["contact"] is None, "outcome", "Contact receipt disagrees with termination")
        actual, handoff = c._arrival(final, profile, config), record["handoff"]
        c._observe_handoff(handoff["observation"], actual, final, config)
        c._require(handoff["limits"] == handoff["observation"]["limits"] and handoff["time_s"] == final["time_s"]
                   and handoff["eligible"] is (termination == "catch_handoff"), "handoff", "Predicted handoff binding differs")
        eligible = bool(termination == "catch_handoff")
        if eligible:
            c._require(actual["eligible"] and points[-1]["phase"].startswith("recovery_landing_"), "handoff", "Predicted pins/fuel miss the actual gate")
            c._same_state(final, handoff["state"], "handoff")
        else:
            c._require(handoff["state"] is None, "handoff", "Failed forecast cannot manufacture a handoff state")
        result.update(passed=True, predicted_handoff_eligible=eligible,
                      terminal_margins=gate_margins(actual), checkpoint_count=len(points))
    except c._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError, AttributeError):
        result["issues"].append({"code": "record", "detail": "Malformed bounded forecast or origin context"})
    return result
