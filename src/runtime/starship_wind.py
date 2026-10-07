"""Optional declared synthetic ENU wind, not meteorology or a vehicle fit.

The axes are the instantaneous WGS84 local East/North/Up at each vehicle.
Harmonic phase uses absolute simulation time, never wall time or phase resets.
No random generator, callback, external data or live observation is accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

SCHEMA = "missionos.starship_wind.v1"
FRAME = "instantaneous_wgs84_enu"
FIELDS = frozenset({"schema", "frame", "mean_enu_mps", "gust_amplitude_enu_mps",
                    "gust_period_s", "gust_phase_rad"})
_ZERO = (0., 0., 0.)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _coordinate(value):
    # The plant permits numpy.float64 intermediate values. The declaration
    # above remains strict JSON-shaped input; queries follow the plant's
    # finite-real rule rather than rejecting its numerical scalar subtype.
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _vector(value, *, limit):
    if (type(value) not in (list, tuple) or len(value) != 3
            or any(not _number(x) or abs(x) > limit for x in value)):
        raise ValueError("invalid_synthetic_wind_vector")
    return tuple(float(x) for x in value)


@dataclass(frozen=True)
class WindField:
    mean_enu_mps: tuple[float, float, float] = _ZERO
    gust_amplitude_enu_mps: tuple[float, float, float] = _ZERO
    gust_period_s: float = 20.
    gust_phase_rad: float = 0.
    schema: str = SCHEMA
    frame: str = FRAME

    def __post_init__(self):
        if type(self.schema) is not str or type(self.frame) is not str or self.schema != SCHEMA or self.frame != FRAME:
            raise ValueError("unsupported_synthetic_wind")
        mean = _vector(self.mean_enu_mps, limit=100.)
        amplitude = _vector(self.gust_amplitude_enu_mps, limit=100.)
        # This is a finite test envelope, not an operational wind limit.
        if math.hypot(*mean)+math.hypot(*amplitude) > 100.:
            raise ValueError("synthetic_wind_exceeds_test_envelope")
        if (not _number(self.gust_period_s) or not 4. <= self.gust_period_s <= 3600.
                or not _number(self.gust_phase_rad) or abs(self.gust_phase_rad) > 2*math.pi):
            raise ValueError("invalid_synthetic_wind_harmonic")
        object.__setattr__(self, "mean_enu_mps", mean)
        object.__setattr__(self, "gust_amplitude_enu_mps", amplitude)

    @property
    def active(self):
        return any(x != 0. for x in self.mean_enu_mps+self.gust_amplitude_enu_mps)

    @property
    def gust_active(self):
        return any(x != 0. for x in self.gust_amplitude_enu_mps)

    def enu(self, time_s):
        if not _coordinate(time_s) or abs(time_s) > 1e12:
            raise ValueError("invalid_synthetic_wind_time")
        if not self.active:
            return _ZERO
        if not self.gust_active:
            return self.mean_enu_mps
        harmonic = math.sin(2*math.pi*time_s/self.gust_period_s+self.gust_phase_rad)
        return tuple(m+a*harmonic for m, a in zip(self.mean_enu_mps, self.gust_amplitude_enu_mps))

    def eci(self, position_eci_m, time_s):
        enu = self.enu(time_s)
        if not self.active:
            return _ZERO
        if (type(position_eci_m) not in (list, tuple) or len(position_eci_m) != 3
                or any(not _coordinate(x) or abs(x) > 1e15 for x in position_eci_m)):
            raise ValueError("invalid_synthetic_wind_position")
        x, y, z = (float(x) for x in position_eci_m)
        if math.hypot(x, y, z) <= 1.:
            raise ValueError("invalid_synthetic_wind_position")
        # WGS84 is axisymmetric about inertial Z. The geodetic longitude
        # calculated from ECI directly yields local axes already rotated to ECI.
        radius, eccentricity = 6378137., 6.6943799901413165e-3
        p = math.hypot(x, y)
        latitude = math.atan2(z, p*(1-eccentricity))
        for _ in range(8):
            n = radius/math.sqrt(1-eccentricity*math.sin(latitude)**2)
            latitude = math.atan2(z+eccentricity*n*math.sin(latitude), p)
        longitude = math.atan2(y, x)
        sl, cl, so, co = math.sin(latitude), math.cos(latitude), math.sin(longitude), math.cos(longitude)
        axes = ((-so, co, 0.), (-sl*co, -sl*so, cl), (cl*co, cl*so, sl))
        return tuple(sum(enu[j]*axes[j][i] for j in range(3)) for i in range(3))


def wind_from_dict(value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != FIELDS:
        raise ValueError("invalid_synthetic_wind_schema")
    return WindField(**value)


def wind_from_profile(profile):
    if type(profile) is not dict:
        raise ValueError("invalid_synthetic_wind_profile")
    if "environment" not in profile:
        return None
    environment = profile["environment"]
    if type(environment) is not dict or set(environment) != {"wind"}:
        raise ValueError("unsupported_synthetic_environment")
    return wind_from_dict(environment["wind"])
