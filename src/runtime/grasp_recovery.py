"""Bounded contact-guided closure; never open an unsupported object.

Measurements must describe the current control tick in world metres / newtons.
This primitive establishes a grasp qualification, not mission completion.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class GraspEvidence:
    time_seconds: float
    pad_forces_n: tuple[float, float]
    # Projection of object-minus-pad-midpoint onto the measured closing axis.
    centering_error_m: tuple[float, float, float]
    object_in_hand_m: tuple[float, float, float]
    object_in_hand_rotation: tuple[float, ...]  # row-major rotation matrix
    upright: bool

    def validate(self):
        values = (
            self.time_seconds,
            *self.pad_forces_n,
            *self.centering_error_m,
            *self.object_in_hand_m,
            *self.object_in_hand_rotation,
        )
        if (
            len(self.pad_forces_n) != 2
            or len(self.centering_error_m) != 3
            or len(self.object_in_hand_m) != 3
            or len(self.object_in_hand_rotation) != 9
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
            or self.time_seconds < 0
            or min(self.pad_forces_n) < 0
            or type(self.upright) is not bool
        ):
            raise ValueError("invalid grasp evidence")
        r = self.object_in_hand_rotation
        rows = [r[i : i + 3] for i in (0, 3, 6)]
        if any(
            abs(sum(a * b for a, b in zip(rows[i], rows[j])) - (i == j)) > 1e-5
            for i in range(3)
            for j in range(3)
        ):
            raise ValueError("invalid grasp rotation")
        det = (
            r[0] * (r[4] * r[8] - r[5] * r[7])
            - r[1] * (r[3] * r[8] - r[5] * r[6])
            + r[2] * (r[3] * r[7] - r[4] * r[6])
        )
        if abs(det - 1) > 1e-5:
            raise ValueError("invalid grasp rotation")


class GraspRecovery:
    """At most 5 mm hand travel, 5 mm object drift, 0.5 s loaded stable closure.

    close-only is the same verifier with zero translation. No contact means stop;
    an unseen / stale measurement cannot grant permission to continue.
    """

    def __init__(self, initial, *, recenter: bool):
        self.initial = initial
        self.recenter = recenter
        self.window = []
        self.last_time = None

    def update(self, obs):
        g = obs.grasp
        if g is None:
            return "grasp_evidence_missing", None
        g.validate()
        if self.last_time is not None and g.time_seconds <= self.last_time:
            return "grasp_evidence_stale", None
        if self.last_time is not None and g.time_seconds - self.last_time > 0.100001:
            return "grasp_evidence_gap", None
        self.last_time = g.time_seconds
        if not obs.preserved or not g.upright:
            return "grasp_preservation_or_tilt_stop", None
        if (
            math.dist(obs.hand_m, self.initial.hand_m) > 0.005
            or math.dist(obs.object_m, self.initial.object_m) > 0.005
        ):
            return "grasp_motion_limit", None
        if max(g.pad_forces_n) < 0.5:
            return "grasp_no_loaded_contact", None
        if min(g.pad_forces_n) >= 0.5 and obs.held:
            self.window.append(g)
            first = self.window[0]
            angle = math.acos(
                max(
                    -1.0,
                    min(
                        1.0,
                        (
                            sum(
                                a * b
                                for a, b in zip(
                                    first.object_in_hand_rotation, g.object_in_hand_rotation
                                )
                            )
                            - 1
                        )
                        / 2,
                    ),
                )
            )
            if math.dist(
                first.object_in_hand_m, g.object_in_hand_m
            ) > 0.001 or angle > math.radians(2):
                self.window = [g]
            elif g.time_seconds - first.time_seconds >= 0.5 - 1e-9:
                return "qualified", None
            return "running", (0.0, 0.0, 0.0)
        self.window = []
        delta = (0.0, 0.0, 0.0)
        if self.recenter:
            length = math.sqrt(sum(v * v for v in g.centering_error_m))
            if length > 0.005:
                return "grasp_centering_out_of_bounds", None
            scale = min(1.0, 0.0005 / max(length, 1e-12))
            delta = tuple(v * scale for v in g.centering_error_m)
            target = tuple(v + d for v, d in zip(obs.hand_m, delta))
            if math.dist(target, self.initial.hand_m) > 0.005:
                return "grasp_motion_limit", None
        return "running", delta
