"""Verify observed clearance, with at most one extra second for a clearing route."""

import math
import time


def verify_clearance(observe, tick, sim_time, context_stamp, current_exposure):
    started = sim_time()
    wall_deadline = time.monotonic() + 6
    initial = observed = observe()
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (initial, current_exposure)):
        raise ValueError("finite observed exposure required")
    # This extension is justified by new observations, not a five-second model forecast.
    eligible = 0.06 < initial <= 0.25 and current_exposure - initial >= 0.1
    deadline = min(started + 1.0, context_stamp + 5.0)
    samples = [dict(sim_s=started, exposure=initial)]
    reason = "already_clear" if initial <= 0.06 else "insufficient_observed_clearance_progress"
    if eligible:
        reason = "bounded_extension_exhausted"
        while observed > 0.06 and sim_time() < deadline and time.monotonic() < wall_deadline:
            tick()
            observed = observe()
            if not math.isfinite(observed) or not 0 <= observed <= 1:
                raise ValueError("finite observed exposure required")
            samples.append(dict(sim_s=sim_time(), exposure=observed))
            if observed > initial + 0.05:
                reason = "observed_clearance_progress_reversed"
                break
        if observed <= 0.06:
            reason = "cleared_during_bounded_extension"
    return dict(
        observed_exposure=observed,
        initial_observed_exposure=initial,
        threshold=0.06,
        passed=observed <= 0.06,
        extension_eligible=eligible,
        extension_limit_sim_s=1.0,
        extension_elapsed_sim_s=sim_time() - started,
        reason=reason,
        observations=samples,
    )
