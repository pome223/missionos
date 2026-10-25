"""Pure opt-in vertical landing request; no plant or authority admission.

The fixed deceleration is a controller design assumption. The feedforward uses
COM vertical velocity as an approximation to hull-clearance rate; it does not
account for rotating hull geometry or moving COM and proves no stopping bound.
"""

import math
from numbers import Real


REFERENCE_DECELERATION_MPS2 = 8.0
MINIMUM_VERTICAL_FORCE_ACCELERATION_MPS2 = 0.5


def terminal_descent_request(
    clearance_m,
    vertical_speed_mps,
    gravity_mps2,
    target_downward_speed_mps,
    velocity_tau_s,
):
    """Return a requested thrust acceleration and canonical numerical receipt.

    Actual hull clearance sets the square-root reference. Below the surface the
    clamped reference has zero feedforward; the actual negative clearance and
    measured speed are retained. No achieved state or force is assigned here.
    """
    values = (
        clearance_m,
        vertical_speed_mps,
        gravity_mps2,
        target_downward_speed_mps,
        velocity_tau_s,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
        for value in values
    ):
        raise ValueError("terminal_descent_requires_finite_real_measurements")
    height, speed, gravity, target, tau = map(float, values)
    if gravity <= 0.0 or target <= 0.0 or tau <= 0.0:
        raise ValueError("terminal_descent_requires_positive_gravity_target_and_tau")
    reference_height = max(0.0, height)
    reference_speed = math.sqrt(
        target * target + 2.0 * REFERENCE_DECELERATION_MPS2 * reference_height
    )
    desired = -reference_speed
    feedforward = (
        -REFERENCE_DECELERATION_MPS2 * speed / reference_speed
        if height >= 0.0
        else 0.0
    )
    feedback = (desired - speed) / tau
    raw = gravity + feedforward + feedback
    acceleration = max(MINIMUM_VERTICAL_FORCE_ACCELERATION_MPS2, raw)
    if not all(math.isfinite(x) for x in (reference_speed, feedforward, feedback, raw)):
        raise ValueError("terminal_descent_nonfinite_derived_request")
    receipt = {
        "schema": "missionos.booster_terminal_energy_descent.v1",
        "clearance_m": height,
        "reference_height_m": reference_height,
        "vertical_speed_mps": speed,
        "gravity_mps2": gravity,
        "target_downward_speed_mps": target,
        "velocity_tau_s": tau,
        "reference_deceleration_mps2": REFERENCE_DECELERATION_MPS2,
        "reference_speed_mps": reference_speed,
        "desired_vertical_speed_mps": desired,
        "approximate_com_vertical_feedforward_mps2": feedforward,
        "feedback_acceleration_mps2": feedback,
        "raw_vertical_force_acceleration_mps2": raw,
        "minimum_vertical_force_acceleration_mps2": MINIMUM_VERTICAL_FORCE_ACCELERATION_MPS2,
        "vertical_force_acceleration_mps2": acceleration,
        "uses_com_vertical_speed_approximation": True,
        "stopping_guarantee": False,
    }
    return acceleration, receipt
