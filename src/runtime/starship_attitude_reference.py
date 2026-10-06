"""Continuous roll reference for a commanded thrust axis, not vehicle motion.

Each update minimally rotates the previous target frame onto the new body +Z
direction. Its transverse axes therefore do not depend on a fixed direction
whose projection can vanish. This is a local parallel-transport construction;
it does not add torque, limit physical body rates or guarantee control tracking.
"""
from __future__ import annotations

import math

from . import starship_physics as env
from . import starship_sixdof as dyn

REFERENCE_POLICY = "parallel_transport_v1"
CONDITIONED_REFERENCE_POLICY = "conditioned_geographic_v1"
MINIMUM_REFERENCE_PROJECTION = .1  # Projection amplification limited to ten.


class ParallelTransportFrame:
    """Stateful target frame. An initial target supplies the initial roll choice.

    A directly antipodal axis command has no unique minimal rotation. In that
    case the previous target's +X is the deterministic rotation axis and the
    diagnostic marks the ambiguity. No future samples or observed trajectory
    fitting are used. A discontinuous axis command remains discontinuous.
    """

    def __init__(self, initial_target_quaternion):
        self.quaternion = dyn.normalize_quaternion(initial_target_quaternion)
        self.diagnostics = self._diagnostics(0., False)

    def _diagnostics(self, angle, antipodal):
        return {"policy_id": REFERENCE_POLICY,
                "target_q_body_to_eci": list(self.quaternion),
                "axis_step_rad": angle, "antipodal_fallback": bool(antipodal)}

    def target(self, axis):
        if len(axis) != 3 or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                    and math.isfinite(x) for x in axis):
            raise ValueError("target axis must contain three finite numbers")
        length = env.norm(axis)
        if not math.isfinite(length) or length < 1e-12:
            raise ValueError("target axis must be nonzero and finite")
        desired = env.scale(axis, 1/length)
        previous = dyn.rotate(self.quaternion, (0., 0., 1.))
        cross = env.cross(previous, desired)
        sine = env.norm(cross)
        cosine = max(-1., min(1., env.dot(previous, desired)))
        angle = math.atan2(sine, cosine)
        antipodal = sine < 1e-12 and cosine < 0
        if antipodal:
            rotation_axis = dyn.rotate(self.quaternion, (1., 0., 0.))
            delta = (0., *rotation_axis)
        elif sine < 1e-12:
            delta = (1., 0., 0., 0.)
        else:
            rotation_axis = env.scale(cross, 1/sine)
            delta = (math.cos(angle/2), *env.scale(rotation_axis, math.sin(angle/2)))
        updated = dyn.normalize_quaternion(dyn.quaternion_multiply(delta, self.quaternion))
        if sum(a*b for a, b in zip(updated, self.quaternion)) < 0:
            updated = tuple(-x for x in updated)
        self.quaternion = updated
        self.diagnostics = self._diagnostics(angle, antipodal)
        return updated


class ConditionedGeographicFrame:
    """Keep geographic roll except while bridging its ill-conditioned region.

    Pure parallel transport changes the entire entry bank history. This helper
    instead preserves the supplied legacy frame while the projected geographic
    reference is well conditioned. Reacquisition after transport is rate
    limited by a predeclared existing-controller timescale, not a flight fit.
    """

    def __init__(self, initial_target_quaternion, time_s, *, maximum_roll_rate_rad_s):
        if (not math.isfinite(time_s) or not math.isfinite(maximum_roll_rate_rad_s)
                or maximum_roll_rate_rad_s <= 0):
            raise ValueError("finite time and positive roll rate are required")
        self.frame = ParallelTransportFrame(initial_target_quaternion)
        self.time_s = time_s
        self.maximum_roll_rate_rad_s = maximum_roll_rate_rad_s
        self.bridging = False
        self.diagnostics = {}

    def target(self, axis, reference, preferred_target_quaternion, *, time_s, force_bridge=False,
               defer_geographic_reacquisition=False):
        if (type(defer_geographic_reacquisition) is not bool
                or defer_geographic_reacquisition and force_bridge is not True):
            raise ValueError("deferred geographic roll requires an explicit forced bridge")
        if not math.isfinite(time_s) or time_s < self.time_s:
            raise ValueError("target time must be finite and monotonic")
        preferred = dyn.normalize_quaternion(preferred_target_quaternion)
        reference = tuple(reference)
        if len(reference) != 3 or not all(math.isfinite(x) for x in reference) or env.norm(reference) < 1e-12:
            raise ValueError("reference must be a finite nonzero vector")
        dt = time_s-self.time_s
        transported = self.frame.target(axis)
        z = dyn.rotate(transported, (0., 0., 1.))
        ref = env.scale(reference, 1/env.norm(reference))
        projected = env.add(ref, env.scale(z, -env.dot(ref, z)))
        projection_norm = env.norm(projected)
        conditioned = projection_norm >= MINIMUM_REFERENCE_PROJECTION
        self.bridging = self.bridging or force_bridge or not conditioned
        roll_error, roll_step = 0., 0.
        if defer_geographic_reacquisition:
            # Only the requested +Z is transported.  Geographic roll remains
            # deferred even when its projection is well conditioned, so a
            # powered near-half-turn frame change cannot choose the other arc.
            target, mode = transported, "transport_deferred_geographic_roll"
        elif not self.bridging:
            # The caller's legacy quaternion is already unit length. Keep it
            # bit-identical rather than silently changing the well-conditioned
            # flight through a second numerical normalization.
            target = tuple(preferred_target_quaternion)
            mode = "legacy_geographic"
        elif not conditioned:
            target, mode = transported, "transport_ill_conditioned"
        else:
            x = dyn.rotate(transported, (1., 0., 0.))
            wanted = dyn.rotate(preferred, (1., 0., 0.))
            roll_error = math.atan2(env.dot(z, env.cross(x, wanted)), env.dot(x, wanted))
            limit = self.maximum_roll_rate_rad_s*dt
            roll_step = max(-limit, min(limit, roll_error))
            if abs(roll_error) <= limit:
                target, self.bridging, mode = tuple(preferred_target_quaternion), False, "geographic_reacquired"
            else:
                delta = (math.cos(roll_step/2), *env.scale(z, math.sin(roll_step/2)))
                target = dyn.normalize_quaternion(dyn.quaternion_multiply(delta, transported))
                mode = "bounded_roll_reacquisition"
        self.frame.quaternion = target
        self.time_s = time_s
        self.diagnostics = {**self.frame.diagnostics, "policy_id": CONDITIONED_REFERENCE_POLICY,
                            "target_q_body_to_eci": list(target), "mode": mode,
                            "reference_projection_norm": projection_norm,
                            "minimum_reference_projection": MINIMUM_REFERENCE_PROJECTION,
                            "roll_error_rad": roll_error, "roll_step_rad": roll_step,
                            "maximum_roll_rate_rad_s": self.maximum_roll_rate_rad_s,
                            "elapsed_s": dt}
        if defer_geographic_reacquisition:
            self.diagnostics["geographic_roll_deferred"] = True
        return target
