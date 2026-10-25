"""Pure, source-bound desired attitude references from a saved Booster flight.

No state initialization, control request, dynamics, acceptance or qualification
is performed. A normalized cubic Hermite quaternion supplies mutually
consistent attitude, body rate and body acceleration. Flaps are reference
setpoints only; an executor must retain actual finite actuators and feedback.
"""

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import math
from numbers import Real
import re

EARTH_ROTATION_RAD_S = 7.292115e-5
MAXIMUM_CLOCK_S = 10000.0
MAXIMUM_NODES = 100000


def _scalar(value, name, *, nonnegative=False):
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError("invalid_reference_" + name)
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid_reference_" + name) from exc
    if not math.isfinite(result) or nonnegative and result < 0:
        raise ValueError("invalid_reference_" + name)
    return result


def _vector(value, length, name):
    if type(value) is not tuple or len(value) != length:
        raise ValueError("immutable_reference_" + name + "_required")
    return tuple(_scalar(x, name) for x in value)


def _sha(value):
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ValueError("explicit_reference_sha256_required")
    return value


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _scale(a, s):
    return tuple(x * s for x in a)


def _multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _conjugate(q):
    return (q[0], -q[1], -q[2], -q[3])


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ReferenceNode:
    time_s: float
    q_body_to_eci: tuple[float, ...]
    omega_body_rad_s: tuple[float, ...]
    flap_angles_rad: tuple[float, ...]

    def __post_init__(self):
        clock = _scalar(self.time_s, "time", nonnegative=True)
        if clock > MAXIMUM_CLOCK_S:
            raise ValueError("reference_clock_out_of_bounds")
        q = _vector(self.q_body_to_eci, 4, "quaternion")
        norm = math.hypot(*q)
        if abs(norm - 1.0) > 1e-6:
            raise ValueError("unit_reference_quaternion_required")
        rates = _vector(self.omega_body_rad_s, 3, "body_rate")
        if type(self.flap_angles_rad) is not tuple or not 1 <= len(self.flap_angles_rad) <= 128:
            raise ValueError("immutable_bounded_reference_flaps_required")
        flaps = tuple(_scalar(x, "flap") for x in self.flap_angles_rad)
        object.__setattr__(self, "time_s", clock)
        object.__setattr__(self, "q_body_to_eci", _scale(q, 1 / norm))
        object.__setattr__(self, "omega_body_rad_s", rates)
        object.__setattr__(self, "flap_angles_rad", flaps)

    def record(self):
        return {
            "time_s": self.time_s,
            "q_body_to_eci": list(self.q_body_to_eci),
            "omega_body_rad_s": list(self.omega_body_rad_s),
            "flap_angles_rad": list(self.flap_angles_rad),
        }


@dataclass(frozen=True)
class ReferenceReceipt:
    source_sha256: str
    profile_sha256: str
    reference_data_sha256: str
    actual_time_s: float
    actual_cutoff_time_s: float
    reference_time_s: float
    reference_cutoff_time_s: float
    bracket_times_s: tuple[float, float]
    bracket_indices: tuple[int, int]
    interpolation_fraction: float
    right_node_hemisphere_flipped: bool
    earth_z_rotation_rad: float

    def __post_init__(self):
        for key in ("source_sha256", "profile_sha256", "reference_data_sha256"):
            _sha(getattr(self, key))
        for key in (
            "actual_time_s",
            "actual_cutoff_time_s",
            "reference_time_s",
            "reference_cutoff_time_s",
        ):
            value = _scalar(getattr(self, key), key, nonnegative=True)
            if value > MAXIMUM_CLOCK_S:
                raise ValueError("reference_receipt_clock_out_of_bounds")
            object.__setattr__(self, key, value)
        times = _vector(self.bracket_times_s, 2, "bracket_times")
        fraction = _scalar(self.interpolation_fraction, "fraction")
        rotation = _scalar(self.earth_z_rotation_rad, "earth_rotation")
        if (
            type(self.bracket_indices) is not tuple
            or len(self.bracket_indices) != 2
            or any(type(i) is not int or i < 0 for i in self.bracket_indices)
            or self.bracket_indices[1] != self.bracket_indices[0] + 1
            or not 0 <= times[0] < times[1] <= MAXIMUM_CLOCK_S
            or not times[0] <= self.reference_time_s <= times[1]
            or times[0] >= times[1]
            or self.actual_time_s < self.actual_cutoff_time_s
            or self.reference_time_s < self.reference_cutoff_time_s
            or not 0 <= fraction <= 1
            or abs(fraction - (self.reference_time_s - times[0]) / (times[1] - times[0])) > 1e-12
            or abs(
                self.reference_time_s
                - self.reference_cutoff_time_s
                - (self.actual_time_s - self.actual_cutoff_time_s)
            )
            > 1e-9
            or abs(
                rotation
                - EARTH_ROTATION_RAD_S * (self.actual_cutoff_time_s - self.reference_cutoff_time_s)
            )
            > 1e-12
            or type(self.right_node_hemisphere_flipped) is not bool
        ):
            raise ValueError("invalid_reference_receipt_bracket")
        object.__setattr__(self, "bracket_times_s", times)
        object.__setattr__(self, "interpolation_fraction", fraction)
        object.__setattr__(self, "earth_z_rotation_rad", rotation)

    def record(self):
        return {
            "schema": "missionos.booster_desired_reference_receipt.v1",
            **self.__dict__,
            "bracket_times_s": list(self.bracket_times_s),
            "bracket_indices": list(self.bracket_indices),
            "desired_reference_only": True,
            "actual_state_assigned": False,
            "control_authority_granted": False,
            "kinematics": "normalized cubic Hermite q with analytic qdot/qddot; C1 attitude, acceleration may jump at nodes",
            "node_acceleration_side": "right interval except final node uses left",
            "rate_frame": "reference body frame; transport to actual body is executor responsibility",
            "flap_reference_is_actual_actuator_state": False,
        }


@dataclass(frozen=True)
class DesiredBoosterReference:
    q_body_to_eci: tuple[float, ...]
    qdot_body_to_eci_per_s: tuple[float, ...]
    qddot_body_to_eci_per_s2: tuple[float, ...]
    omega_reference_body_rad_s: tuple[float, ...]
    alpha_reference_body_rad_s2: tuple[float, ...]
    flap_angles_reference_rad: tuple[float, ...]
    receipt: ReferenceReceipt

    def __post_init__(self):
        for key, count in (
            ("q_body_to_eci", 4),
            ("qdot_body_to_eci_per_s", 4),
            ("qddot_body_to_eci_per_s2", 4),
            ("omega_reference_body_rad_s", 3),
            ("alpha_reference_body_rad_s2", 3),
        ):
            object.__setattr__(self, key, _vector(getattr(self, key), count, key))
        if (
            type(self.receipt) is not ReferenceReceipt
            or type(self.flap_angles_reference_rad) is not tuple
            or not 1 <= len(self.flap_angles_reference_rad) <= 128
            or abs(math.hypot(*self.q_body_to_eci) - 1) > 1e-8
        ):
            raise ValueError("invalid_desired_reference_value")
        object.__setattr__(
            self,
            "flap_angles_reference_rad",
            tuple(_scalar(x, "reference_flap") for x in self.flap_angles_reference_rad),
        )

    def record(self):
        return {
            "q_body_to_eci": list(self.q_body_to_eci),
            "qdot_body_to_eci_per_s": list(self.qdot_body_to_eci_per_s),
            "qddot_body_to_eci_per_s2": list(self.qddot_body_to_eci_per_s2),
            "omega_reference_body_rad_s": list(self.omega_reference_body_rad_s),
            "alpha_reference_body_rad_s2": list(self.alpha_reference_body_rad_s2),
            "flap_angles_reference_rad": list(self.flap_angles_reference_rad),
            "receipt": self.receipt.record(),
        }


@dataclass(frozen=True)
class BoosterReference:
    nodes: tuple[ReferenceNode, ...]
    cutoff_time_s: float
    source_sha256: str
    profile_sha256: str
    maximum_sample_gap_s: float = 2.100001
    first_landing_request_s: float | None = None
    hemisphere_flips: tuple[bool, ...] = field(init=False)
    reference_data_sha256: str = field(init=False)

    def __post_init__(self):
        _sha(self.source_sha256)
        _sha(self.profile_sha256)
        cutoff = _scalar(self.cutoff_time_s, "cutoff", nonnegative=True)
        gap = _scalar(self.maximum_sample_gap_s, "maximum_gap", nonnegative=True)
        if (
            type(self.nodes) is not tuple
            or not 2 <= len(self.nodes) <= MAXIMUM_NODES
            or any(type(n) is not ReferenceNode for n in self.nodes)
            or not 0 < gap <= 10
            or not self.nodes[0].time_s <= cutoff < self.nodes[-1].time_s
        ):
            raise ValueError("invalid_reference_interval_inventory")
        if self.first_landing_request_s is not None:
            landing = _scalar(self.first_landing_request_s, "landing_time", nonnegative=True)
            if not cutoff <= landing <= self.nodes[-1].time_s:
                raise ValueError("reference_landing_time_out_of_bounds")
            object.__setattr__(self, "first_landing_request_s", landing)
        nodes = []
        flips = []
        for original in self.nodes:
            flip = bool(nodes and _dot(nodes[-1].q_body_to_eci, original.q_body_to_eci) < 0)
            node = (
                replace(original, q_body_to_eci=_scale(original.q_body_to_eci, -1))
                if flip
                else original
            )
            if nodes:
                delta = node.time_s - nodes[-1].time_s
                if (
                    not 0 < delta <= gap
                    or len(node.flap_angles_rad) != len(nodes[-1].flap_angles_rad)
                    or delta
                    * max(
                        math.hypot(*node.omega_body_rad_s), math.hypot(*nodes[-1].omega_body_rad_s)
                    )
                    > math.pi
                ):
                    raise ValueError("nonmonotone_gapped_or_ambiguous_reference_samples")
            nodes.append(node)
            flips.append(flip)
        object.__setattr__(self, "cutoff_time_s", cutoff)
        object.__setattr__(self, "maximum_sample_gap_s", gap)
        object.__setattr__(self, "nodes", tuple(nodes))
        object.__setattr__(self, "hemisphere_flips", tuple(flips))
        object.__setattr__(
            self,
            "reference_data_sha256",
            sha256(_canonical(self.record(include_digest=False))).hexdigest(),
        )

    def record(self, *, include_digest=True):
        return {
            "schema": "missionos.booster_desired_reference.v1",
            "source_sha256": self.source_sha256,
            "profile_sha256": self.profile_sha256,
            "reference_cutoff_time_s": self.cutoff_time_s,
            "first_landing_request_s": self.first_landing_request_s,
            "first_landing_request_is_phase_transition_not_ignition": True,
            "maximum_sample_gap_s": self.maximum_sample_gap_s,
            "nodes": [n.record() for n in self.nodes],
            "hemisphere_flips": list(self.hemisphere_flips),
            "pre_cutoff_node_is_bracketing_reference_only": self.nodes[0].time_s
            < self.cutoff_time_s,
            "desired_reference_only": True,
            "actual_state_assigned": False,
            "qualification_refreshed": False,
            **({"reference_data_sha256": self.reference_data_sha256} if include_digest else {}),
        }

    def at(self, actual_time_s, *, actual_cutoff_time_s):
        actual = _scalar(actual_time_s, "actual_time", nonnegative=True)
        cutoff = _scalar(actual_cutoff_time_s, "actual_cutoff", nonnegative=True)
        reference = self.cutoff_time_s + (actual - cutoff)
        if (
            actual > MAXIMUM_CLOCK_S
            or cutoff > MAXIMUM_CLOCK_S
            or actual < cutoff
            or reference < self.cutoff_time_s
            or reference > self.nodes[-1].time_s
        ):
            raise ValueError("reference_extrapolation_forbidden")
        index = min(
            len(self.nodes) - 2, bisect_right(tuple(n.time_s for n in self.nodes), reference) - 1
        )
        left, right = self.nodes[index : index + 2]
        h = right.time_s - left.time_s
        u = (reference - left.time_s) / h
        p0, p1 = left.q_body_to_eci, right.q_body_to_eci
        d0 = _scale(_multiply(p0, (0.0, *left.omega_body_rad_s)), 0.5)
        d1 = _scale(_multiply(p1, (0.0, *right.omega_body_rad_s)), 0.5)

        def combine(weights):
            return tuple(
                sum(w * x for w, x in zip(weights, values)) for values in zip(p0, d0, p1, d1)
            )

        p = combine(
            (
                2 * u**3 - 3 * u * u + 1,
                (u**3 - 2 * u * u + u) * h,
                -2 * u**3 + 3 * u * u,
                (u**3 - u * u) * h,
            )
        )
        dp = combine(
            (
                (6 * u * u - 6 * u) / h,
                3 * u * u - 4 * u + 1,
                (-6 * u * u + 6 * u) / h,
                3 * u * u - 2 * u,
            )
        )
        ddp = combine(((12 * u - 6) / h**2, (6 * u - 4) / h, (-12 * u + 6) / h**2, (6 * u - 2) / h))
        norm = math.hypot(*p)
        if norm < 1e-8:
            raise ValueError("degenerate_quaternion_reference_polynomial")
        nd = _dot(p, dp) / norm
        ndd = (_dot(dp, dp) + _dot(p, ddp)) / norm - _dot(p, dp) ** 2 / norm**3
        q = _scale(p, 1 / norm)
        qd = _add(_scale(dp, 1 / norm), _scale(p, -nd / norm**2))
        qdd = _add(
            _add(_scale(ddp, 1 / norm), _scale(dp, -2 * nd / norm**2)),
            _scale(p, 2 * nd**2 / norm**3 - ndd / norm**2),
        )
        angle = EARTH_ROTATION_RAD_S * (cutoff - self.cutoff_time_s)
        earth = (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))
        q, qd, qdd = (_multiply(earth, value) for value in (q, qd, qdd))
        omega = _scale(_multiply(_conjugate(q), qd)[1:], 2.0)
        alpha = _scale(_add(_multiply(_conjugate(qd), qd), _multiply(_conjugate(q), qdd))[1:], 2.0)
        flaps = tuple(
            (1 - u) * a + u * b for a, b in zip(left.flap_angles_rad, right.flap_angles_rad)
        )
        if any(
            not math.isfinite(x) for values in (q, qd, qdd, omega, alpha, flaps) for x in values
        ):
            raise ValueError("nonfinite_interpolated_reference")
        receipt = ReferenceReceipt(
            self.source_sha256,
            self.profile_sha256,
            self.reference_data_sha256,
            actual,
            cutoff,
            reference,
            self.cutoff_time_s,
            (left.time_s, right.time_s),
            (index, index + 1),
            float(u),
            self.hemisphere_flips[index + 1],
            angle,
        )
        return DesiredBoosterReference(q, qd, qdd, omega, alpha, flaps, receipt)


def _parse(raw, *, maximum_bytes):
    if type(raw) is not bytes or len(raw) > maximum_bytes:
        raise ValueError("bounded_raw_json_bytes_required")

    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError("duplicate_reference_json_key")
            out[key] = value
        return out

    def constant(value):
        raise ValueError("nonfinite_reference_json")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def extract_booster_reference(
    source_json, profile_json, *, expected_source_sha256, expected_profile_sha256
):
    """Extract compact reference nodes; no private artifact is imported by path.

    The sole sample before cutoff is retained only when needed to bracket the
    desired cutoff reference. It is never claimed as an actual cutoff state.
    Original quaternions/rates/actual flap angles are source measurements here,
    but every interpolated value is a desired reference, not a new plant state.
    """
    _sha(expected_source_sha256)
    _sha(expected_profile_sha256)
    if (
        type(source_json) is not bytes
        or type(profile_json) is not bytes
        or sha256(source_json).hexdigest() != expected_source_sha256
        or sha256(profile_json).hexdigest() != expected_profile_sha256
    ):
        raise ValueError("reference_source_or_profile_sha256_mismatch")
    source = _parse(source_json, maximum_bytes=512 * 1024**2)
    profile = _parse(profile_json, maximum_bytes=2 * 1024**2)
    if (
        type(source) is not dict
        or type(source.get("booster_run")) is not dict
        or type(profile) is not dict
        or type(profile.get("booster")) is not dict
        or type(profile.get("actuators")) is not dict
    ):
        raise ValueError("saved_booster_and_profile_objects_required")
    booster = source["booster_run"]
    samples = booster.get("samples")
    events = booster.get("events")
    if type(events) is not list or any(type(e) is not dict for e in events):
        raise ValueError("saved_booster_events_required")
    cutoffs = [
        e.get("time_s") for e in events if e.get("event") == "booster_boostback_cutoff_command"
    ]
    if len(cutoffs) != 1 or not isinstance(samples, list) or not 2 <= len(samples) <= MAXIMUM_NODES:
        raise ValueError("unique_saved_booster_cutoff_and_samples_required")
    cutoff = _scalar(cutoffs[0], "cutoff", nonnegative=True)
    if any(
        type(s) is not dict
        or not {"time_s", "q_body_to_eci", "omega_body_rad_s", "flap_angles_rad"} <= set(s)
        or any(
            type(s.get(key)) is not list
            for key in ("q_body_to_eci", "omega_body_rad_s", "flap_angles_rad")
        )
        for s in samples
    ):
        raise ValueError("saved_reference_sample_fields_required")
    times = [_scalar(s["time_s"], "sample_time", nonnegative=True) for s in samples]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("nonmonotone_saved_reference_samples")
    first = max(0, bisect_right(times, cutoff) - 1)
    selected = samples[first:]
    if not selected[0]["time_s"] <= cutoff < selected[-1]["time_s"]:
        raise ValueError("saved_samples_do_not_bracket_cutoff")
    grid = profile["booster"].get("grid_fin_panels")
    angle = _scalar(
        profile["actuators"].get("grid_fin_limit_deg"), "grid_fin_limit", nonnegative=True
    )
    if type(grid) is not list or not 1 <= len(grid) <= 125 or angle >= 90:
        raise ValueError("bounded_profile_grid_fin_inventory_required")
    limits = (0.0, 0.0, 0.0, *(math.radians(angle) for _ in grid))
    nodes = []
    for sample in selected:
        node = ReferenceNode(
            sample["time_s"],
            tuple(sample["q_body_to_eci"]),
            tuple(sample["omega_body_rad_s"]),
            tuple(sample["flap_angles_rad"]),
        )
        if len(node.flap_angles_rad) != len(limits) or any(
            abs(a) > limit + 1e-10 for a, limit in zip(node.flap_angles_rad, limits)
        ):
            raise ValueError("saved_reference_flap_exceeds_profile_bound")
        nodes.append(node)
    landings = [
        _scalar(e.get("time_s"), "landing_time", nonnegative=True)
        for e in events
        if e.get("event") == "booster_landing_burn_requested"
    ]
    return BoosterReference(
        tuple(nodes),
        cutoff,
        expected_source_sha256,
        expected_profile_sha256,
        first_landing_request_s=min(landings) if landings else None,
    )
