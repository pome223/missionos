"""Bounded finite-engine capture of the fixed desired H0 coast reference.

Only requests and sticky pulse history are produced. Actual pose, rates, fuel,
availability, spools and gimbals remain inputs to the unchanged finite plant.
"""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math

from . import starship_sixdof as dyn
from .starship_booster_control import control_with_measured_tvc
from .starship_booster_lowq_capture import _checked_reference, _number

POLICY_ID = "fixed_h0_powered_reference_capture_v1"
MAXIMUM_DURATION_S = 15.0
MINIMUM_PROPELLANT_KG = 75000.0
MAXIMUM_ATTITUDE_ERROR_DEG = 1.0
MAXIMUM_RATE_ERROR_RAD_S = 0.001
GUARD_CLOCK_TOLERANCE_S = 1e-9
STOPPED = {"completed", "incomplete_timeout", "incomplete_reserve", "incomplete_no_center"}


def _state_sha256(state):
    return sha256(
        json.dumps(asdict(state), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PoweredCaptureRequest:
    time_s: float
    coast_started_s: float
    first_request_time_s: float
    elapsed_s: float
    status: str
    previous_status: str
    status_changed: bool
    stopped_s: float | None
    count: int
    throttle: float
    selected_center_index: int | None
    desired_reference: object
    reference_rate_actual_body_rad_s: tuple[float, ...]
    reference_acceleration_actual_body_rad_s2: tuple[float, ...]
    actual_q_body_to_eci: tuple[float, ...]
    actual_body_rate_rad_s: tuple[float, ...]
    actual_propellant_kg: float
    actual_state_sha256: str
    attitude_error_deg: float
    body_rate_error_rad_s: float

    @property
    def active(self):
        return self.status == "active"

    def record(self):
        return {
            "schema": "missionos.booster_powered_reference_capture_request.v1",
            "policy_id": POLICY_ID,
            "time_s": self.time_s,
            "coast_started_s": self.coast_started_s,
            "started_s": self.coast_started_s,
            "first_request_time_s": self.first_request_time_s,
            "elapsed_s": self.elapsed_s,
            "status": self.status,
            "previous_status": self.previous_status,
            "status_changed": self.status_changed,
            "stopped_s": self.stopped_s,
            "active": self.active,
            "count": self.count,
            "throttle": self.throttle,
            "selected_center_index": self.selected_center_index,
            "maximum_duration_s": MAXIMUM_DURATION_S,
            "minimum_propellant_kg": MINIMUM_PROPELLANT_KG,
            "maximum_attitude_error_deg": MAXIMUM_ATTITUDE_ERROR_DEG,
            "maximum_body_rate_error_rad_s": MAXIMUM_RATE_ERROR_RAD_S,
            "guard_clock_tolerance_s": GUARD_CLOCK_TOLERANCE_S,
            "desired_reference": self.desired_reference.record(),
            "reference_rate_actual_body_rad_s": list(self.reference_rate_actual_body_rad_s),
            "reference_acceleration_actual_body_rad_s2": list(
                self.reference_acceleration_actual_body_rad_s2
            ),
            "actual_q_body_to_eci": list(self.actual_q_body_to_eci),
            "actual_body_rate_rad_s": list(self.actual_body_rate_rad_s),
            "actual_propellant_kg": self.actual_propellant_kg,
            "actual_state_sha256": self.actual_state_sha256,
            "attitude_error_deg": self.attitude_error_deg,
            "body_rate_error_rad_s": self.body_rate_error_rad_s,
            "guard_order": "reserve; time strictly beyond limit; measured achievement; time at limit; no available center",
            "request_is_execution": False,
            "actual_state_assigned": False,
            "physics_limits_changed": False,
            "qualification_refreshed": False,
            "model_calls": 0,
        }


class PoweredReferenceCapture:
    """One pulse, anchored to actual boostback cutoff, with sticky termination."""

    def __init__(self, reference, coast_started_s):
        self.reference = _checked_reference(reference)
        self.coast_started_s = _number(coast_started_s, "coast_started_s")
        if not 0 <= self.coast_started_s <= 10000:
            raise ValueError("invalid_powered_reference_capture_origin")
        self.status = "pending"
        self.first_request_time_s = None
        self.last_time_s = None
        self.stopped_s = None

    def request(self, state, craft):
        if (
            type(state) is not dyn.State6DOF
            or type(craft) is not dyn.Vehicle6DOF
            or len(craft.engines) < 33
            or len(state.engine_states) != len(craft.engines)
            or [e.name for e in craft.engines[:3]] != [f"booster_main_{i}" for i in range(3)]
        ):
            raise ValueError("powered_reference_capture_requires_actual_booster_center_inventory")
        now = _number(state.time_s, "time_s")
        if now < self.coast_started_s or self.last_time_s is not None and now < self.last_time_s:
            raise ValueError("powered_reference_capture_clock_must_not_reverse")
        elapsed = now - self.coast_started_s
        desired = self.reference.at(now, actual_cutoff_time_s=self.coast_started_s)
        actual_q = dyn.normalize_quaternion(state.q_body_to_eci)
        inverse = (actual_q[0], *(-x for x in actual_q[1:]))
        error = dyn.quaternion_multiply(inverse, desired.q_body_to_eci)
        attitude_error = math.degrees(2 * math.acos(min(1.0, abs(float(error[0])))))
        rate = tuple(
            float(x)
            for x in dyn.rotate(
                inverse, dyn.rotate(desired.q_body_to_eci, desired.omega_reference_body_rad_s)
            )
        )
        acceleration = tuple(
            float(x)
            for x in dyn.rotate(
                inverse, dyn.rotate(desired.q_body_to_eci, desired.alpha_reference_body_rad_s2)
            )
        )
        rate_error = math.hypot(*(x - y for x, y in zip(state.omega_body_rad_s, rate)))
        fuel = _number(state.propellant_kg, "propellant_kg")
        center = next((i for i in range(3) if state.engine_states[i].available), None)
        previous = self.status
        status = previous
        if previous not in STOPPED:
            if fuel <= MINIMUM_PROPELLANT_KG:
                status = "incomplete_reserve"
            elif elapsed > MAXIMUM_DURATION_S + GUARD_CLOCK_TOLERANCE_S:
                status = "incomplete_timeout"
            elif (
                attitude_error <= MAXIMUM_ATTITUDE_ERROR_DEG
                and rate_error <= MAXIMUM_RATE_ERROR_RAD_S
            ):
                status = "completed"
            elif elapsed >= MAXIMUM_DURATION_S - GUARD_CLOCK_TOLERANCE_S:
                status = "incomplete_timeout"
            elif center is None:
                status = "incomplete_no_center"
            else:
                status = "active"
        count = center + 1 if status == "active" else 0
        throttle = float(craft.engines[center].min_throttle) if status == "active" else 0.0
        if status == "active" and not 0 < throttle <= 1:
            raise ValueError(
                "powered_reference_capture_requires_physical_positive_minimum_throttle"
            )
        first = now if self.first_request_time_s is None else self.first_request_time_s
        stopped = now if previous not in STOPPED and status in STOPPED else self.stopped_s
        request = PoweredCaptureRequest(
            now,
            self.coast_started_s,
            first,
            float(elapsed),
            status,
            previous,
            status != previous,
            stopped,
            count,
            throttle,
            center if status == "active" else None,
            desired,
            rate,
            acceleration,
            tuple(float(x) for x in actual_q),
            tuple(float(x) for x in state.omega_body_rad_s),
            fuel,
            _state_sha256(state),
            float(attitude_error),
            float(rate_error),
        )
        self.status, self.first_request_time_s, self.last_time_s, self.stopped_s = (
            status,
            first,
            now,
            stopped,
        )
        return request


def powered_capture_control(state, craft, profile, request, *, tail_tracking=False):
    """One existing measured-TVC request; engine spool is never prescribed.

    The existing tracking controller applies the body-frame transport cross
    term once. Reference acceleration passed here is the inertial derivative
    expressed in actual body axes, without pre-subtracting that cross term.
    """
    if (
        type(request) is not PoweredCaptureRequest
        or type(tail_tracking) is not bool
        or request.actual_state_sha256 != _state_sha256(state)
        or not request.active
        and not tail_tracking
    ):
        raise ValueError("powered_reference_capture_requires_fresh_active_or_shutdown_tail_request")
    command, diagnostic = control_with_measured_tvc(
        state,
        craft,
        request.desired_reference.q_body_to_eci,
        request.throttle if request.active else 0.0,
        request.count if request.active else 0,
        profile,
        use_flaps=False,
        reference_rate_body_rad_s=request.reference_rate_actual_body_rad_s,
        reference_acceleration_body_rad_s=request.reference_acceleration_actual_body_rad_s2,
        recenter_uncommanded_main=not tail_tracking,
    )
    command = replace(
        command,
        engines=tuple(
            target if actual.available else dyn.EngineCommand()
            for target, actual in zip(command.engines, state.engine_states)
        ),
    )
    diagnostic["powered_reference_capture"] = {
        "schema": "missionos.booster_powered_reference_capture_control.v1",
        "policy_id": POLICY_ID,
        "mode": "powered_reference_capture"
        if request.active
        else "powered_reference_shutdown_tail",
        "request": request.record(),
        "tail_tracking": tail_tracking,
        "recenter_uncommanded_main": not tail_tracking,
        "reference_transport_cross_term_applied_by_existing_controller": True,
        "finite_actuator_tracking_required": True,
        "actual_state_assigned": False,
        "physics_limits_changed": False,
        "qualification_refreshed": False,
        "model_calls": 0,
    }
    return command, diagnostic
