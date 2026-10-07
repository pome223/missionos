"""Offline finite six-DOF landing alternatives from a captured full context."""
from __future__ import annotations

from copy import deepcopy
import time

from .starship_landing_context import request


def forecast(snapshot, profile, config, *, delay_s=None, duration_s=90.):
    request(snapshot, profile, config, delay_s, duration_s)
    from .starship_booster_recovery import simulate_recovery
    before, clock = deepcopy(snapshot), time.monotonic()
    run = simulate_recovery(profile, deepcopy(snapshot["state"]), config, duration_s=duration_s,
                            _forecast=True, _landing_context=deepcopy(snapshot), _landing_delay_s=delay_s)
    if snapshot != before:
        raise RuntimeError("forecast_mutated_origin_context")
    return {"run": run, "forecast_wall_time_s": time.monotonic()-clock,
            "prediction_is_execution": False, "production_policy_admitted": False}
