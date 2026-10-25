"""M1 simulation authority, sensor estimates and bounded recovery operations.

The two circular study areas are engineering targets calibrated from the earlier
opportunity screen. They are frozen before these scenarios, not surveyed ocean
clearances or evidence of cross-range target guidance. No real flight authority.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

from .starship_operational_trials import REGISTERED_CASES, is_trial, trial_manifest, delivered_notice

SCENARIOS = {
    **{f"sixdof_m1_{case}": case for case in REGISTERED_CASES},
    "sixdof_m1_orbital_planning": "comparison_orbital_update",
    "sixdof_m1_normal": "normal",
    "sixdof_m1_replan": "recovery_update",
    "sixdof_m1_timeout": "decision_timeout",
    "sixdof_m1_event_normal": "event_normal",
    "sixdof_m1_event_tradeoff": "event_tradeoff",
    "sixdof_m1_event_timeout": "event_timeout",
    **{
        f"sixdof_m1_comparison_{name}": f"comparison_{name}"
        for name in ("normal", "update", "late", "tool_failure", "timeout")
    },
}
SCOPE = "local_simulation_and_bounded_return_replanning"
CONFIG = "examples/spaceflight/starship-m1-operations.json"
COMPARISON_CONFIG = "examples/spaceflight/starship-comparison-operations.json"
COMPARISON_CASES = frozenset(
    f"comparison_{name}" for name in ("normal", "update", "late", "tool_failure", "timeout")
)
MODE_ENV = "MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE"
ROOT = Path(__file__).resolve().parents[2]
EXTRA_SOURCES = (
    "src/runtime/starship_orbital_window.py", "src/runtime/starship_concurrent.py", "src/runtime/starship_booster_coast_probe.py",
    "src/runtime/starship_reference_ids.py",
    "src/intelligence/starship_reference_protocol.py",
    "src/intelligence/starship_flight_supervisor.py",
    "src/intelligence/space_ops_investigator.py",
    "src/runtime/starship_replanning.py",
    "src/runtime/starship_call_limits.py",
    "src/runtime/starship_operational_trials.py",
    "src/runtime/starship_replanning_executor.py",
    "src/runtime/starship_m2_contract.py",
    "src/runtime/starship_return_time_search.py",
    "src/runtime/starship_replanning_events.py",
    "src/runtime/starship_replanning_comparison.py",
    "src/runtime/starship_decision_evidence.py",
    "src/runtime/starship_model_diagnostics.py",
    "src/runtime/starship_replanning_verifier.py",
    "src/intelligence/starship_replanning.py",
    "src/runtime/starship_return_prediction.py",
    "src/runtime/starship_artifacts.py",
    "src/runtime/starship_mission_control.py",
    "src/intelligence/starship_mission_planner.py",
    "scripts/run_starship_m1.py",
    "scripts/summarize_starship_models.py",
    "scripts/run_starship_mission_worker.py",
    CONFIG,
    COMPARISON_CONFIG,
)


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def source_hashes():
    from .starship_mission_director import source_hashes as director_sources

    return {
        **director_sources(ROOT),
        **{f: sha256((ROOT / f).read_bytes()).hexdigest() for f in EXTRA_SOURCES},
    }


def contract(mode, case=None, *, legacy_call_limits=False, authority_gap_reasoning=False):
    if type(authority_gap_reasoning) is not bool:
        raise ValueError("invalid_authority_gap_reasoning_optin")
    if isinstance(case, str) and case.startswith(("op_trial_", "op_scope_")) and not is_trial(case):
        raise ValueError("operational_trial_not_registered")
    if mode not in ("fixture", "live"):
        raise ValueError("m1_mode_not_configured")
    result = {
        "schema": "missionos.return_replanning_authority.v1",
        "scope": SCOPE,
        "mode": mode,
        "operations": json.loads((ROOT / CONFIG).read_text()),
        "maximum_jev_calls": 12,
        "maximum_llm_calls": 2,
        "maximum_forecasts": 12,
        "prediction_checks": "three_observation_samples_plus_half_step_center_v1",
        "maximum_workers": 2,
        "forecast_timeout_s": 180.0,
        "batch_timeout_s": 600.0,
        "decision_timeout_s": 35.0,
        "maximum_plan_revisions": 4,
        "maximum_delay_s": 6000.0,
        "monitor_interval_s": 1200.0,
        "actions": [
            "evaluate_returns",
            "keep_plan",
            "wait_for_update",
            "refresh_observation",
            "select_nominal",
            "select_next_orbit",
        ],
        "fallback": "first_currently_admissible_candidate_else_bounded_coast_unresolved",
        "physical_execution_authorized": False,
    }

    if (
        case in ("event_normal", "event_tradeoff", "event_timeout")
        or case in COMPARISON_CASES
        or case == "comparison_orbital_update" or is_trial(case)
    ):
        result.update(maximum_jev_calls=16, maximum_forecasts=20)
        result["event_supervision"] = {
            "scope": "post_deployment_orbital_coast_only",
            "maximum_coast_body_rate_rad_s": 0.02,
            "deadline_margin_s": 60.0,
            "urgent_response": "inhibit_return_and_record_unresolved_stop",
            "commitment_response": "preserve_currently_admissible_booking_without_model_wait",
            "objective": "Compare estimated recovery-service completion time and waiting resources; preserve the original plan when an update does not change that tradeoff. Service forecasts are uncertain estimates, never area clearance.",
        }
    if case in COMPARISON_CASES or case == "comparison_orbital_update" or is_trial(case):
        cfg = json.loads((ROOT / COMPARISON_CONFIG).read_text())
        result["operations"] = cfg["operations"]
        result["comparison_preparation"] = deepcopy(cfg["comparison_preparation"])
        # This fault is an approved test boundary, never an observed fact or
        # advice sent to the policy. Live tool failure is not simulated silently.
        if case == "comparison_tool_failure":
            if mode != "fixture":
                raise ValueError("comparison_tool_failure_fixture_only")
            result["comparison_preparation"]["synthetic_tool_failure"] = "cancel_on_material_update"
    if case == "comparison_orbital_update" or is_trial(case):
        from .starship_orbital_window import CONFIG as ORBITAL_WINDOW

        result["orbital_planning_window"] = deepcopy(ORBITAL_WINDOW)
        result["provider_budget_policy"] = "independent_provider_reservations_v1"
        # Explicit null removes the DeepSeek count cap for new live grants.
        # Historical and fixture grants keep their recorded finite budgets.
        result["maximum_llm_calls"] = 1 if legacy_call_limits or mode == "fixture" else None
        result["comparison_preparation"]["deep_reasoning"].update(
            protocol="ids_orbital_low_16384",
            reasoning_effort="low",
            maximum_output_tokens=16384,
        )
    if is_trial(case):
        result["operational_trial"] = {
            "schema": "missionos.operational_trial_binding.v1",
            "trial_id": case, "manifest_sha256": digest(trial_manifest(case)),
            "scope": "published_recovery_notices_only",
        }
    if authority_gap_reasoning:
        from .starship_orbital_window import AUTHORITY_GAP_POLICY, reserved_decision_s
        result["authority_gap_reasoning"] = deepcopy(AUTHORITY_GAP_POLICY)
        reserved_decision_s(result)  # Reject opt-in without the approved longer window.
    if mode == "live" and case == "event_normal" and not legacy_call_limits:
        # Current M2/M3 ordinary returns keep Jev-mediated decisions and their
        # original 35-second deadline; DeepSeek escalation has no count cap.
        result["maximum_llm_calls"] = None
        result["deepseek_call_budget_policy"] = "unlimited_jev_escalations_v1"
    return result


def estimate(state):
    """Synthetic bounded navigation/gauge/actuator telemetry, not hidden mass.

    Plant truth is used only inside this sensor adapter. Quantization error is
    bounded explicitly; the policy never receives engine truth or future faults.
    Finite actuator positions are telemetered under the stated perfect actuator
    telemetry assumption. This is not a validated navigation filter.
    """
    from dataclasses import asdict

    s = asdict(state)
    for key, quantum in (("r_eci_m", 1.0), ("v_eci_mps", 0.001), ("omega_body_rad_s", 1e-6)):
        s[key] = [round(x / quantum) * quantum for x in s[key]]
    q = [round(x, 6) for x in s["q_body_to_eci"]]
    size = math.sqrt(sum(x * x for x in q))
    s["q_body_to_eci"] = [x / size for x in q]
    s["propellant_kg"] = round(s["propellant_kg"] / 10) * 10.0
    # Health and finite positions are perfect synthetic observed channels.
    # Preserve a reported failure; never rewrite health to nominal.
    return json.loads(
        json.dumps(
            {
                "schema": "missionos.ship_return_prediction_origin.v1",
                "basis": "synthetic_navigation_estimate_v1",
                "retained_count": 0,
                "state": s,
            },
            allow_nan=False,
        )
    )


def uncertainty_origins(origin):
    """Three preregistered joint perturbations, NOT exhaustive box coverage."""
    values = []
    for sign in (0, -1, 1):
        row = deepcopy(origin)
        s = row["state"]
        for key, offsets in (
            ("r_eci_m", [2.0, -2.0, 1.0]),
            ("v_eci_mps", [0.002, -0.002, 0.001]),
            ("omega_body_rad_s", [1e-5] * 3),
        ):
            s[key] = [x + sign * d for x, d in zip(s[key], offsets)]
        s["propellant_kg"] += sign * 100.0
        a = sign * math.radians(0.01) / 2
        c, t = math.cos(a), math.sin(a)
        w, x, y, z = s["q_body_to_eci"]
        s["q_body_to_eci"] = [w * c - x * t, w * t + x * c, y * c + z * t, z * c - y * t]
        values.append(row)
    return values


def notices(case, time_s):
    """Only messages already issued; no scenario or future schedule to the AI."""
    if is_trial(case):
        notice = delivered_notice(case, time_s)
        if notice is None:
            raise ValueError("operational_notice_not_yet_delivered")
        return notice
    if isinstance(case, str) and case.startswith(("op_trial_", "op_scope_")):
        raise ValueError("operational_trial_not_registered")
    if case in COMPARISON_CASES or case == "comparison_orbital_update":
        cfg = json.loads((ROOT / COMPARISON_CONFIG).read_text())
        # Reuse a labelled development input, never a new held-out scenario.
        schedule = cfg["evaluation_conditions"][
            "comparison_update" if case == "comparison_orbital_update" else case
        ]
        updated = time_s >= schedule["notice_update_at_s"]
        revised = updated and schedule["material_update"]
        forecast = cfg["observed_notice_values"]
        return {
            "sequence": 2 if updated else 1,
            "issued_at_s": schedule["notice_update_at_s"] if updated else 900.0,
            "expires_at_s": 12000.0,
            "areas": {"east": [4500.0, 5600.0], "west": [10000.0, 11000.0]},
            "text": (
                "Recovery coordinator correction: both study areas retain their explicit operating clearance. The eastern post-contact service-delay estimate has increased and remains uncertain; the western estimate is unchanged. Compare estimated completion intervals and the candidate-specific waiting resource margins. These estimates neither grant clearance nor report completed servicing."
                if revised
                else "Recovery coordinator: both study areas retain their explicit operating clearance. Eastern and western post-contact service-delay estimates are unchanged. These estimates neither grant clearance nor report completed servicing."
            ),
            "recovery_service_forecast": {
                "basis": "synthetic_uncertain_estimate_not_authority_or_observed_completion",
                "east_delay_after_contact_s": deepcopy(
                    forecast["revised_east_delay_after_contact_s"]
                    if revised
                    else forecast["initial_east_delay_after_contact_s"]
                ),
                "west_delay_after_contact_s": deepcopy(forecast["west_delay_after_contact_s"]),
            },
        }
    if case in ("event_normal", "event_tradeoff", "event_timeout"):
        updated = time_s >= 1450.0
        revised = updated and case != "event_normal"
        return {
            "sequence": 2 if updated else 1,
            "issued_at_s": 1450.0 if updated else 900.0,
            "expires_at_s": 12000.0,
            "areas": {"east": [4500.0, 5600.0], "west": [10000.0, 11000.0]},
            "text": (
                "Recovery coordinator correction: both study areas retain their explicit operating clearance. Eastern post-contact servicing is now forecast to resume much later, with an uncertain delay. The western service forecast is unchanged. Compare total estimated completion and the resource cost of a later return; the forecast is not a clearance or a measured outcome."
                if revised
                else "Recovery coordinator: both study areas are cleared for the stated windows. Eastern servicing is expected promptly after contact. This published status update does not change the operating conditions."
            ),
            "recovery_service_forecast": {
                "basis": "synthetic_uncertain_estimate_not_authority_or_observed_completion",
                "east_delay_after_contact_s": [7200.0, 14400.0] if revised else [600.0, 1200.0],
                "west_delay_after_contact_s": [1200.0, 2400.0],
            },
        }
    if case == "normal":
        return {
            "sequence": 1,
            "issued_at_s": 900.0,
            "expires_at_s": 12000.0,
            "text": "Recovery coordinator: primary eastern study area is available. Preserve the planned return; backup western area is also staffed.",
            "areas": {"east": [4500.0, 5600.0], "west": [10000.0, 11000.0]},
        }
    if time_s < 4800.0:
        return {
            "sequence": 1,
            "issued_at_s": 950.0,
            "expires_at_s": 4800.0,
            "text": "Recovery coordinator: eastern recovery area withdrawn because support assets are unavailable. Western backup staffing remains unconfirmed. Obtain an updated status before committing to that area. No clearance is granted by this notice.",
            "areas": {},
        }
    return {
        "sequence": 2,
        "issued_at_s": 4800.0,
        "expires_at_s": 12000.0,
        "text": "Recovery coordinator update: eastern area remains unavailable. Western backup team confirms availability for the stated interval. Check a fresh trajectory, waiting resources and arrival time before changing the return plan.",
        "areas": {"west": [10000.0, 11000.0]},
    }


def resources(envelope, now_s, scheduled_s):
    cfg = envelope["operations"]["waiting"]
    waited = max(0.0, now_s - scheduled_s)
    return {
        "elapsed_wait_s": waited,
        "energy_remaining_kwh": cfg["initial_energy_kwh"] - waited * cfg["power_kw"] / 3600.0,
        "temperature_upper_k": cfg["initial_temperature_k"] + waited * cfg["warming_k_per_s"],
        "propellant_loss_upper_kg": waited * cfg["loss_upper_kg_per_s"],
        "valid_until_s": scheduled_s + envelope["maximum_delay_s"],
    }
