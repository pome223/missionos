"""Optional sampled reentry warning, not confirmed occupancy or entry authority."""

from __future__ import annotations

import math


def possible_reentry(config, request, forecast):
    """Use supported image motion toward the pad and a future center crossing.

    This is an advisory reason to reobserve at the normal bounded cadence. It
    does not turn unknown occupancy into a clear/occupied fact, persist stale
    forecasts, relax current Rules or provide a collision guarantee.
    """
    if not config["world"]["pad_state_advisory"].get("reentry_risk_advisory", False):
        return False
    if not forecast["supported"]:
        return False
    detections = forecast.get("detections", [])
    if len(detections) != 16:
        raise ValueError("Reentry warning requires the full image history")
    points = []
    for d in detections:
        p = d.get("xyz_m", [])
        if (
            d.get("supported") is not True
            or len(p) != 3
            or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in p)
        ):
            raise ValueError("Invalid supported image localization")
        points.append(p)
    radius = config["world"]["pad_queue"]["pad_exclusion_radius_m"]
    # A visible inward trend excludes static/outgoing actors. This threshold is
    # fixed before the follow-up acquisition; it is not an accuracy/adoption gate.
    if math.hypot(*points[-5][:2]) - math.hypot(*points[-1][:2]) < 0.25:
        return False
    current = request["observations"][-1]
    lead = current["queue_lead"]["xyz"]
    pad = config["world"]["pad_queue"]["pad_xyz_m"]
    if math.dist(lead[:2], pad[:2]) <= radius:
        return False  # already occupied: current Rules handle this
    crossing = False
    for item in forecast["forecasts"]:
        p = item.get("xyz_relative_to_pad_m", [])
        if (
            not isinstance(p, list)
            or len(p) != 3
            or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in p)
        ):
            raise ValueError("Invalid supported future position")
        if item["stamp_ns"] / 1e9 > current["sim_s"] and math.hypot(*p[:2]) <= radius:
            crossing = True
    return crossing
