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

SCENARIOS = {
    "sixdof_m1_normal": "normal",
    "sixdof_m1_replan": "recovery_update",
    "sixdof_m1_timeout": "decision_timeout",
}
SCOPE = "local_simulation_and_bounded_return_replanning"
CONFIG = "examples/spaceflight/starship-m1-operations.json"
MODE_ENV = "MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE"
ROOT = Path(__file__).resolve().parents[2]
EXTRA_SOURCES = (
    "src/runtime/starship_replanning.py",
    "src/runtime/starship_replanning_executor.py",
    "src/runtime/starship_replanning_verifier.py",
    "src/intelligence/starship_replanning.py",
    "src/runtime/starship_return_prediction.py",
    "src/runtime/starship_artifacts.py",
    "src/runtime/starship_mission_control.py",
    "src/intelligence/starship_mission_planner.py",
    "scripts/run_starship_m1.py",
    "scripts/run_starship_mission_worker.py",
    CONFIG,
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


def contract(mode):
    if mode not in ("fixture", "live"):
        raise ValueError("m1_mode_not_configured")
    return {
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
