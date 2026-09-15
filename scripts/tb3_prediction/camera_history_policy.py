"""Independent image-history baseline and shared approaching-obstacle screen.

No model weights, scenario label, actor state or future image is consumed.
"""

import argparse
import json
import math
from pathlib import Path
import statistics
import time

if __package__:
    from .image_motion_guard import check_images
else:
    from image_motion_guard import check_images


def approaching(bounds, timestamps):
    if not bounds or bounds[-1] is None:
        return dict(approaching=False, reason="no_current_obstacle_silhouette")
    velocities = []
    for side in (0, 1):
        points = [
            (t, b[side]) for t, b in zip(timestamps, bounds) if b is not None and 0 < b[side] < 223
        ]
        if len(points) < 2 or points[-1][0] - points[0][0] < 0.5:
            continue
        mt = statistics.mean(t for t, _ in points)
        mx = statistics.mean(x for _, x in points)
        velocity = sum((t - mt) * (x - mx) for t, x in points) / sum(
            (t - mt) ** 2 for t, _ in points
        )
        velocities.append(velocity)
    if not velocities:
        return dict(approaching=False, reason="no_observed_direction")
    velocity = statistics.median(velocities)
    left, right = bounds[-1]
    enters = (left >= 112 and velocity < 0 and left + velocity * 4 <= 112) or (
        right <= 112 and velocity > 0 and right + velocity * 4 >= 112
    )
    return dict(
        approaching=enters,
        velocity_px_s=velocity,
        reason="observed_motion_toward_route" if enters else "no_observed_entry",
        horizon_s=4,
    )


def assessment(images, timestamps):
    motion = check_images(images, timestamps)
    return dict(motion=motion, incoming=approaching(motion["observed_bounds_px"], timestamps))


def history_decision(current, assessed):
    if not math.isfinite(current) or not 0 <= current <= 1:
        raise ValueError("finite exposure required")
    hazard = current > 0.06 or assessed["incoming"]["approaching"]
    return (
        "direct"
        if not hazard
        else "wait"
        if assessed["motion"]["wait_consistent"] is True
        else "detour"
    )


def learned_decision(current, forecast, assessed):
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (current, forecast)):
        raise ValueError("finite current and predicted exposure required")
    hazard = current > 0.06 or assessed["incoming"]["approaching"] or forecast > 0.2
    if not hazard:
        return "direct"
    if assessed["motion"]["wait_consistent"] is True:
        return "wait"
    if forecast <= 0.2 and assessed["motion"]["wait_consistent"] is not False:
        return "wait"
    return "detour"


def selection_basis(candidate, assessed, learned):
    if candidate == "direct":
        return "no_hazard"
    supported = assessed["motion"]["wait_consistent"]
    if supported is not None:
        return (
            "observed_image_motion_clearance"
            if supported
            else "observed_image_motion_contradiction"
        )
    return "learned_future_mask" if learned else "conservative_unknown_extent"


def select_history(request, output=None):
    from PIL import Image
    import numpy as np

    started = time.monotonic()
    if set(request) != {"policy", "context", "timestamps"} or request["policy"] != "image_history":
        raise ValueError("only camera history, timestamps and image_history policy accepted")
    ts = request["timestamps"]
    if (
        len(request["context"]) != 4
        or len(ts) != 4
        or not all(isinstance(t, (int, float)) and math.isfinite(t) for t in ts)
        or any(abs(b - a - 0.5) > 0.05 for a, b in zip(ts, ts[1:]))
    ):
        raise ValueError("four aligned 2 Hz frames required")
    images = [Image.open(p).convert("RGB") for p in request["context"]]
    rgb = np.asarray(images[-1].resize((224, 224))).astype(float)
    crop = rgb[56:190, 78:146]
    current = float(
        (
            (crop[:, :, 0] > 51)
            & (crop[:, :, 0] > 1.5 * crop[:, :, 1])
            & (crop[:, :, 0] > 1.5 * crop[:, :, 2])
        ).mean()
    )
    assessed = assessment(images, ts)
    candidate = history_decision(current, assessed)
    return dict(
        candidate=candidate,
        selection_basis=selection_basis(candidate, assessed, learned=False),
        source="image_history_projection",
        current_exposure=current,
        predicted_exposure=None,
        image_motion_consistency=assessed["motion"],
        approaching_obstacle=assessed["incoming"],
        learned_wam_invoked=False,
        prediction_horizon_s=4,
        wait_time_origin="last_context_timestamp",
        post_wait_observation_required=True,
        threshold=0.06,
        safety_probability=None,
        latency_wall_s=time.monotonic() - started,
        provenance=None,
        hypothetical_action=[0, 0, 0],
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--request", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = select_history(json.loads(a.request.read_text()))
    a.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
