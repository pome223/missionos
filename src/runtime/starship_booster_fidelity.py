"""Typed opt-in Stage2c settings, not a mission or recovery authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from numbers import Real

POLICY_ID = "fidelity_schedule_v1"
BOOSTBACK_OPEN_S = 147.
BOOSTBACK_CUTOFF_S = 187.


def landing_preview_admission(preview):
    """Only a completed arrest supplies an ignition stopping distance.

    A preview ending at empty fuel or its finite horizon has travelled a
    distance, but has not shown that the booster can stop. Such a distance must
    never postpone braking. The resulting immediate request still passes the
    ordinary attitude/alignment, finite-engine and actuator constraints; it is
    impact mitigation, not a feasible landing or a recovery authorization.
    Older/incomplete receipts are likewise unqualified rather than assumed to
    have reached their requested terminal speed.
    """
    if type(preview) is not dict:
        raise ValueError("invalid_booster_landing_preview")
    for field in ("estimated_stopping_height_m", "estimated_final_speed_mps"):
        value = preview.get(field)
        if value is not None and (not isinstance(value, Real) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            raise ValueError("invalid_booster_landing_preview")
    distance = preview.get("estimated_stopping_height_m")
    final_speed = preview.get("estimated_final_speed_mps")
    complete = bool(preview.get("burn_fuel_feasible") is True
                and preview.get("estimate_termination") == "arrested"
                and preview.get("estimated_stopping_height_is_valid") is True
                and distance is not None and final_speed is not None and final_speed <= 2.)
    return {"schema": "missionos.starship_booster_landing_preview_admission.v1",
            "complete_arrest_prediction": complete,
            "usable_stopping_height_m": float(distance) if complete else None,
            "immediate_braking_request": not complete,
            "reason": "completed_arrest_prediction" if complete else "no_complete_arrest_prediction",
            "prediction_is_execution": False, "recovery_authority_granted": False}


@dataclass(frozen=True)
class BoosterFidelitySettings:
    boostback_velocity_tau_s: float = 25.
    landing_ignition_margin_m: float = 300.

    def __post_init__(self):
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in
                (self.boostback_velocity_tau_s, self.landing_ignition_margin_m))
                or self.boostback_velocity_tau_s not in (25., 35.)
                or self.landing_ignition_margin_m not in (150., 300., 600.)):
            raise ValueError("invalid_registered_booster_fidelity_settings")

    def record(self):
        return asdict(self)

    def limit_step(self, time_s, interval_s):
        for boundary in (BOOSTBACK_OPEN_S, BOOSTBACK_CUTOFF_S):
            if time_s < boundary < time_s+interval_s:
                return boundary-time_s
        return interval_s


def record_first_loads(state, vehicle, observed, commands, witnesses):
    """Record measured plant loads after a request, without advancing the state.

    Positive load is an observation, not proof of an ignition transition. In
    particular residual shutdown thrust can exist at the same time as a request.
    """
    main_loads = [load["thrust_n"] for engine, load in zip(vehicle.engines, observed["engine_loads"])
                  if "main" in engine.name]
    for name in ("boostback", "landing"):
        request = commands[name]
        if request is None or witnesses[name] is not None:
            continue
        if state.time_s < request["time_s"]:
            raise ValueError("fidelity_load_witness_precedes_command")
        eligible = main_loads[3:] if name == "boostback" else main_loads[:13]
        if any(value > 1000. for value in eligible):
            witnesses[name] = {"time_s": state.time_s, "state": asdict(state),
                "main_engine_thrust_n": main_loads,
                "positive_main_count": sum(value > 1000. for value in main_loads),
                "threshold_n": 1000.,
                "meaning": "first_post_request_positive_observed_load_not_proven_ignition"}
