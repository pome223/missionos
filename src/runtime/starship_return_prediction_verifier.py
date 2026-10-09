"""Saved-suffix reproduction and kinematic checks, never flight admission.

These checks do not call the propagator/controller. Agreement with the same
plant is reproducibility only; it cannot establish independent model accuracy.
"""
from __future__ import annotations

import json
import math


def compare_saved_suffix(prediction, reference, request):
    try:
        json.dumps([prediction, reference, request], allow_nan=False)
        samples, expected = prediction["samples"], reference["samples"]
        # All saved physical fields, including actuators, are compared exactly
        # on shared timestamps. No trajectory error tolerance is widened.
        indexed = {(s["time_s"], s["phase"]): s for s in expected}
        matched = [s for s in samples if (s["time_s"], s["phase"]) in indexed]
        physical_equal = all(all(s[k] == indexed[s["time_s"], s["phase"]][k]
                                 for k in request["origin"]["state"]) for s in matched)
        contact = prediction["outcome"]["contact_receipt"]
        contact_consistent = contact is None
        if contact is not None:
            # Contact material point speed relative to the rotating surface.
            omega = 7.292115e-5
            x, y, _ = contact["point_eci_m"]
            surface = (-omega*y, omega*x, 0.)
            relative = [v-w for v, w in zip(contact["point_velocity_eci_mps"], surface)]
            contact_consistent = (math.isclose(math.sqrt(sum(v*v for v in relative)),
                contact["surface_relative_speed_mps"], rel_tol=1e-10, abs_tol=1e-10)
                and contact["event_time_s"] == prediction["final_state"]["time_s"]
                and contact["propellant_kg"] == prediction["final_state"]["propellant_kg"]
                and contact["q_body_to_eci"] == prediction["final_state"]["q_body_to_eci"]
                and contact["contact"] is True)
        event_names = {"return_requested", "deorbit_ignition_command", "deorbit_cutoff_command",
                       "entry_trim_prepared", "retained_return_terminal_trigger", "flip_and_landing_command"}
        def timeline(value):
            return [(e["event"], e["time_s"]) for e in value["events"] if e["event"] in event_names]
        times = [s["time_s"] for s in samples]
        checks = {"origin_unchanged": prediction["origin"] == request["origin"],
            "first_state_matches_origin": all(samples[0][k] == v for k, v in request["origin"]["state"].items()),
            "candidate_time_bound": prediction["return_time_s"] == request["return_time_s"],
            "suffix_sample_coverage": len(matched) >= .95*len(samples) and len(matched) > 1,
            "shared_physical_samples_exact": physical_equal,
            "final_state_exact": prediction["final_state"] == reference["final_state"],
            "contact_receipt_exact": contact == reference["outcome"]["contact_receipt"],
            "contact_kinematics_consistent": contact_consistent,
            "phase_event_times_exact": timeline(prediction) == timeline(reference),
            "termination_matches": prediction["outcome"]["termination"] == reference["outcome"]["termination"],
            "time_advanced": times[-1] > times[0] and all(a <= b for a, b in zip(times, times[1:])),
            "fuel_nonincreasing": all(b["propellant_kg"] <= a["propellant_kg"]+1e-8 for a, b in zip(samples, samples[1:])),
            "no_execution_or_admission_claim": prediction["prediction_is_execution"] is False and prediction["runtime_admission"] is False}
        return {"passed": all(checks.values()), "checks": checks, "matched_sample_count": len(matched),
                "prediction_sample_count": len(samples), "same_model_reproduction_only": True,
                "runtime_admission": False, "independent_physics_validation": False}
    except (KeyError, ValueError, TypeError, IndexError, OverflowError) as error:
        return {"passed": False, "error": f"{type(error).__name__}: {error}", "runtime_admission": False}
