"""Steady rocket operating-point reference; no flight or Raptor calibration.

The caller supplies pressure-matched reference thrust and specific impulse for
ONE fixed chamber/throttle operating point, and a fixed nozzle exit area. At
that point chamber conditions and mass flow are held constant while ambient
pressure varies: F = F_ref + (p_ref - p_ambient) * A_exit. A different throttle
requires a separately supplied operating point; the pressure-area correction
is not multiplied by throttle. No throttle curve or nozzle separation model is
inferred. These equations alone do not establish a Starship performance map.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

STANDARD_GRAVITY_MPS2 = 9.80665


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite numeric")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite numeric")
    return result


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite positive")
    return value


@dataclass(frozen=True)
class FixedOperatingPoint:
    """Caller-established steady operating point with a supported pressure band.

    Reference pressure must be inside that band. Its reference thrust and Isp
    must describe this SAME operating point and pressure. The caller, not this
    equation helper, is responsible for establishing these inputs and bounds.
    """

    reference_thrust_n: float
    reference_isp_s: float
    reference_pressure_pa: float
    nozzle_exit_area_m2: float
    minimum_ambient_pressure_pa: float
    maximum_ambient_pressure_pa: float

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _number(getattr(self, name), name))
        for name in ("reference_thrust_n", "reference_isp_s", "nozzle_exit_area_m2"):
            _positive(getattr(self, name), name)
        low, high = self.minimum_ambient_pressure_pa, self.maximum_ambient_pressure_pa
        if not 0 <= low <= self.reference_pressure_pa <= high:
            raise ValueError("reference pressure must lie inside a nonnegative supported band")
        # These are finite floating-point computations, not just finite inputs.
        exhaust = _positive(STANDARD_GRAVITY_MPS2 * self.reference_isp_s, "reference exhaust velocity")
        flow = _positive(self.reference_thrust_n / exhaust, "mass flow")
        for pressure in (low, high):
            force = _force(self, pressure)
            _positive(force / (STANDARD_GRAVITY_MPS2 * flow), "effective Isp")


def _force(point, pressure):
    return _positive(
        point.reference_thrust_n
        + (point.reference_pressure_pa - pressure) * point.nozzle_exit_area_m2,
        "effective thrust",
    )


def performance_at_pressure(point: FixedOperatingPoint, ambient_pressure_pa: float) -> dict:
    """Evaluate the one registered operating point; reject unsupported pressure.

    Returns scalar force magnitude, positive propellant mass flow and effective
    Isp. This does not model ignition, shutdown, availability, actuators, thrust
    direction, fuel depletion, or fault derating, and it advances no state.
    """
    if type(point) is not FixedOperatingPoint:
        raise ValueError("fixed operating point required")
    pressure = _number(ambient_pressure_pa, "ambient pressure")
    if not point.minimum_ambient_pressure_pa <= pressure <= point.maximum_ambient_pressure_pa:
        raise ValueError("ambient pressure outside supported band")
    force = _force(point, pressure)
    flow = _positive(
        point.reference_thrust_n / (STANDARD_GRAVITY_MPS2 * point.reference_isp_s),
        "mass flow",
    )
    isp = _positive(force / (STANDARD_GRAVITY_MPS2 * flow), "effective Isp")
    return {"ambient_pressure_pa": pressure, "thrust_n": force,
            "mass_flow_kg_s": flow, "isp_s": isp}
