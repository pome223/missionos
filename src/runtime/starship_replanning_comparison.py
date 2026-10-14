"""Decision-time comparison material; predictions are estimates, not outcomes.

All inputs are observations, approved accounting assumptions and already computed
forecasts. This module neither ranks plans nor reads future scenario messages.
"""

from .starship_replanning import digest, resources
from .starship_replanning_verifier import metrics, site_distance

SCHEMA = "missionos.m1_candidate_comparison.v1"
BASIS = "sampled_predictions_and_observed_notice_not_guaranteed_bounds"
LIMITS = [
    "four_sample_ranges_not_exhaustive_uncertainty_bounds",
    "waiting_accounted_until_burn_not_post_contact_service",
    "service_completion_is_uncertain_estimate_not_clearance_or_observed_outcome",
]


def candidate_material(candidate, envelope, notice, scheduled):
    """Expose each candidate's costs at its own burn, not current balances."""
    points = [metrics(trial) for trial in candidate["trials"]]
    waiting = resources(envelope, candidate["return_time_s"], scheduled)
    finite_contacts = len(points) == 4 and all(point is not None for point in points)
    ranges, areas = {}, {}
    if finite_contacts:
        ranges = {
            key: [min(point[key] for point in points), max(point[key] for point in points)]
            for key in ("time_s", "speed_mps", "tilt_deg", "body_rate_rad_s", "reserve_kg")
        }
        pad = envelope["operations"]["uncertainty"]
        forecast = notice.get("recovery_service_forecast", {})
        for name, area in envelope["operations"]["areas"].items():
            window = notice.get("areas", {}).get(name)
            if window is None or not all(
                site_distance(point, area) + pad["contact_footprint_padding_m"] <= area["radius_m"]
                and window[0] <= point["time_s"] - pad["contact_time_padding_s"]
                and point["time_s"] + pad["contact_time_padding_s"] <= window[1]
                and point["time_s"] <= notice["expires_at_s"]
                for point in points
            ):
                continue
            delay = forecast.get(name + "_delay_after_contact_s")
            areas[name] = {
                "service_delay_after_contact_s": delay,
                "estimated_completion_time_s": (
                    [ranges["time_s"][0] + delay[0], ranges["time_s"][1] + delay[1]]
                    if delay is not None
                    else None
                ),
            }
    loss = waiting["propellant_loss_upper_kg"]
    return {
        "schema": SCHEMA,
        "basis": BASIS,
        "candidate_sha256": digest(candidate),
        "notice_sha256": digest(notice),
        "return_time_s": candidate["return_time_s"],
        "waiting_at_burn": waiting,
        "sampled_contact_ranges": ranges,
        "reserve_after_loss_kg": (
            [value - loss for value in ranges["reserve_kg"]] if finite_contacts else None
        ),
        "area_service_estimates": areas,
        "contact_samples_available": finite_contacts,
        "limitations": LIMITS.copy(),
    }
