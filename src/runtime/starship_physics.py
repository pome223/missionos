"""Three-dimensional point-mass dynamics; NOT a validated Starship vehicle model.

The inertial frame is aligned with ECEF at mission time zero. Uniform Earth
rotation is modeled, but no astronomical epoch, precession, nutation, terrain,
geoid, winds, body attitude dynamics, engine transients or TPS is modeled.
Cd/Cl, reference area, Isp and thrust are caller-supplied assumptions. A bank
command prescribes lift direction; it does not simulate achievable flap motion.

Reference constants/equations:
* NGA WGS84: https://earth-info.nga.mil/?action=wgs84&dir=wgs84
* NASA NESC J2: https://ntrs.nasa.gov/citations/20160006944 (slide 33)
* COESA 1976: https://ntrs.nasa.gov/citations/19770009539 (lower atmosphere).
  The seven geopotential layers implement molecular-scale temperature and
  hydrostatic pressure/density through 86 km geometric height. Above 86 km an
  explicitly NON-COESA exponential surrogate is used, not a thermosphere model.
* Lift/drag: https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/drag-equation/
* Isp: https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/specific-impulse/

All interfaces use SI, except explicitly named degree-valued convenience APIs.
Propagation is RK4 with held control over the step, analytically split at fuel
depletion, and a root solve at ellipsoid contact. Contact preserves impact
velocity; it never creates a soft landing or advances a landed vehicle.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


Vector = tuple[float, float, float]
EARTH_MU_M3_S2 = 3.986004418e14
EARTH_EQUATORIAL_RADIUS_M = 6_378_137.0
EARTH_FLATTENING = 1.0 / 298.257223563
EARTH_POLAR_RADIUS_M = EARTH_EQUATORIAL_RADIUS_M * (1.0 - EARTH_FLATTENING)
EARTH_ECCENTRICITY_SQUARED = EARTH_FLATTENING * (2.0 - EARTH_FLATTENING)
EARTH_ROTATION_RAD_S = 7.292115e-5
EARTH_J2 = 1.08262982e-3  # NASA NESC WGS84-derived reference value.
STANDARD_GRAVITY_MPS2 = 9.80665
ATMOSPHERE_GEOPOTENTIAL_RADIUS_M = 6_356_766.0
AIR_SPECIFIC_GAS_CONSTANT = 8314.32 / 28.9644
UPPER_ATMOSPHERE_SURROGATE_SCALE_HEIGHT_M = 15_000.0


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _vector(value: Vector, name: str) -> None:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{name} must have three components")
    for component in value:
        _finite(component, name)


def add(a: Vector, b: Vector) -> Vector:
    return a[0] + b[0], a[1] + b[1], a[2] + b[2]


def scale(a: Vector, factor: float) -> Vector:
    return a[0] * factor, a[1] * factor, a[2] * factor


def dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vector, b: Vector) -> Vector:
    return a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]


def norm(a: Vector) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vector) -> Vector:
    length = norm(a)
    if length < 1e-15:
        raise ValueError("cannot normalize zero vector")
    return scale(a, 1.0 / length)


@dataclass(frozen=True)
class State3D:
    time_s: float
    r: Vector
    v: Vector
    propellant_kg: float

    def __post_init__(self) -> None:
        _finite(self.time_s, "time_s")
        _finite(self.propellant_kg, "propellant_kg")
        _vector(self.r, "r")
        _vector(self.v, "v")
        if self.propellant_kg < 0 or norm(self.r) < 1.0:
            raise ValueError("propellant must be nonnegative and radius at least 1 m")
        object.__setattr__(self, "r", tuple(self.r))
        object.__setattr__(self, "v", tuple(self.v))


@dataclass(frozen=True)
class Vehicle:
    dry_mass_kg: float
    area_m2: float
    cd: float
    cl: float
    isp_s: float
    max_thrust_n: float

    def __post_init__(self) -> None:
        for name in ("dry_mass_kg", "area_m2", "cd", "cl", "isp_s", "max_thrust_n"):
            _finite(getattr(self, name), name)
        if self.dry_mass_kg <= 0 or self.isp_s <= 0:
            raise ValueError("dry mass and Isp must be positive")
        if min(self.area_m2, self.cd, self.max_thrust_n) < 0:
            raise ValueError("area, Cd and maximum thrust must be nonnegative")


@dataclass(frozen=True)
class Control:
    throttle: float = 0.0
    thrust_direction_eci: Vector = (0.0, 0.0, 0.0)
    bank_rad: float = 0.0

    def __post_init__(self) -> None:
        _finite(self.throttle, "throttle")
        _finite(self.bank_rad, "bank_rad")
        _vector(self.thrust_direction_eci, "thrust_direction_eci")
        if not 0 <= self.throttle <= 1:
            raise ValueError("throttle must be between 0 and 1")
        if self.throttle > 0 and norm(self.thrust_direction_eci) < 1e-15:
            raise ValueError("powered control requires a nonzero thrust direction")
        object.__setattr__(self, "thrust_direction_eci", tuple(self.thrust_direction_eci))


def _rotate_z(vector: Vector, angle: float) -> Vector:
    c, s = math.cos(angle), math.sin(angle)
    return c*vector[0]-s*vector[1], s*vector[0]+c*vector[1], vector[2]


def ecef_to_eci(vector: Vector, time_s: float) -> Vector:
    """Rotate vector components only; velocity transforms also need omega x r."""
    return _rotate_z(vector, EARTH_ROTATION_RAD_S * time_s)


def eci_to_ecef(vector: Vector, time_s: float) -> Vector:
    return _rotate_z(vector, -EARTH_ROTATION_RAD_S * time_s)


def geodetic_to_ecef(lat_rad: float, lon_rad: float, altitude_m: float) -> Vector:
    for name, value in (("latitude", lat_rad), ("longitude", lon_rad), ("altitude", altitude_m)):
        _finite(value, name)
    if not -math.pi/2 <= lat_rad <= math.pi/2:
        raise ValueError("latitude must be within +/- pi/2")
    s, c = math.sin(lat_rad), math.cos(lat_rad)
    n = EARTH_EQUATORIAL_RADIUS_M / math.sqrt(1.0 - EARTH_ECCENTRICITY_SQUARED*s*s)
    return ((n+altitude_m)*c*math.cos(lon_rad), (n+altitude_m)*c*math.sin(lon_rad),
            (n*(1.0-EARTH_ECCENTRICITY_SQUARED)+altitude_m)*s)


def ecef_to_geodetic(r: Vector) -> tuple[float, float, float]:
    """Return geodetic latitude, longitude (radians), ellipsoid height (m)."""
    _vector(r, "r")
    p = math.hypot(r[0], r[1])
    if norm(r) < 1.0:
        raise ValueError("geodetic coordinates undefined at Earth center")
    if p < 1e-8:
        return math.copysign(math.pi/2, r[2]), 0.0, abs(r[2])-EARTH_POLAR_RADIUS_M
    lat = math.atan2(r[2], p*(1.0-EARTH_ECCENTRICITY_SQUARED))
    for _ in range(12):
        s = math.sin(lat)
        n = EARTH_EQUATORIAL_RADIUS_M / math.sqrt(1.0-EARTH_ECCENTRICITY_SQUARED*s*s)
        next_lat = math.atan2(r[2]+EARTH_ECCENTRICITY_SQUARED*n*s, p)
        if abs(next_lat-lat) < 1e-14:
            lat = next_lat
            break
        lat = next_lat
    s, c = math.sin(lat), math.cos(lat)
    # Projection on ellipsoid normal avoids loss of precision near the poles.
    h = p*c+r[2]*s-EARTH_EQUATORIAL_RADIUS_M*math.sqrt(1.0-EARTH_ECCENTRICITY_SQUARED*s*s)
    return lat, math.atan2(r[1], r[0]), h


def air_relative_velocity(state: State3D) -> Vector:
    """Still-air velocity, expressed in inertial axes; atmosphere co-rotates."""
    return add(state.v, (EARTH_ROTATION_RAD_S*state.r[1], -EARTH_ROTATION_RAD_S*state.r[0], 0.0))


def local_frame(state: State3D) -> tuple[Vector, Vector, Vector]:
    """Return ellipsoid-normal up, east, north, all in inertial coordinates."""
    lat, lon, _ = ecef_to_geodetic(eci_to_ecef(state.r, state.time_s))
    up = (math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat))
    east = (-math.sin(lon), math.cos(lon), 0.0)
    north = cross(up, east)
    return tuple(ecef_to_eci(v, state.time_s) for v in (up, east, north))


def surface_state(lat_deg: float, lon_deg: float, altitude_m: float = 0.0,
                  propellant_kg: float = 0.0, time_s: float = 0.0) -> State3D:
    r = ecef_to_eci(geodetic_to_ecef(math.radians(lat_deg), math.radians(lon_deg), altitude_m), time_s)
    v = (-EARTH_ROTATION_RAD_S*r[1], EARTH_ROTATION_RAD_S*r[0], 0.0)
    return State3D(time_s, r, v, propellant_kg)


def gravity_acceleration(r: Vector, *, j2: bool = True) -> Vector:
    radius = norm(r)
    if radius < 1:
        raise ValueError("gravity undefined near Earth center")
    central = scale(r, -EARTH_MU_M3_S2/radius**3)
    if not j2:
        return central
    z2 = (r[2]/radius)**2
    factor = 1.5*EARTH_J2*EARTH_MU_M3_S2*EARTH_EQUATORIAL_RADIUS_M**2/radius**5
    return add(central, (factor*r[0]*(5*z2-1), factor*r[1]*(5*z2-1), factor*r[2]*(5*z2-3)))


def specific_energy(state: State3D, *, j2: bool = True) -> float:
    """Kinetic plus central/J2 potential, in inertial frame, J/kg."""
    radius = norm(state.r)
    potential = -EARTH_MU_M3_S2/radius
    if j2:
        potential += EARTH_MU_M3_S2/radius*EARTH_J2*(EARTH_EQUATORIAL_RADIUS_M/radius)**2 * (3*(state.r[2]/radius)**2-1)/2
    return dot(state.v, state.v)/2+potential


_LAYER_HEIGHTS = (0.0, 11_000.0, 20_000.0, 32_000.0, 47_000.0, 51_000.0, 71_000.0, 84_852.04584490575)
_LAPSE_RATES = (-0.0065, 0.0, 0.001, 0.0028, 0.0, -0.0028, -0.002)


def _layer_pressure(temperature: float, pressure: float, lapse: float, dh: float) -> tuple[float, float]:
    next_t = temperature+lapse*dh
    if lapse == 0:
        next_p = pressure*math.exp(-STANDARD_GRAVITY_MPS2*dh/(AIR_SPECIFIC_GAS_CONSTANT*temperature))
    else:
        next_p = pressure*(temperature/next_t)**(STANDARD_GRAVITY_MPS2/(AIR_SPECIFIC_GAS_CONSTANT*lapse))
    return next_t, next_p


def standard_atmosphere(altitude_m: float) -> dict:
    """Dry standard lower atmosphere; height uses ellipsoid as sea-level proxy.

    temperature_k is molecular-scale temperature, not corrected kinetic
    temperature above 80 km. Density remains derived from the scale temperature.
    Above 86 km a continuous 15 km scale-height surrogate is explicitly flagged.
    Density becomes zero at 1000 km. This is not a validated thermosphere.
    """
    _finite(altitude_m, "altitude_m")
    z = max(0.0, min(86_000.0, altitude_m))
    h = ATMOSPHERE_GEOPOTENTIAL_RADIUS_M*z/(ATMOSPHERE_GEOPOTENTIAL_RADIUS_M+z)
    temperature, pressure = 288.15, 101325.0
    for base, top, lapse in zip(_LAYER_HEIGHTS, _LAYER_HEIGHTS[1:], _LAPSE_RATES):
        if h <= base:
            break
        temperature, pressure = _layer_pressure(temperature, pressure, lapse, min(h, top)-base)
        if h <= top:
            break
    regime = "coesa1976_lower_molecular_scale"
    if altitude_m > 86_000:
        regime = "unvalidated_exponential_above_86km"
        pressure *= math.exp(-(altitude_m-86_000)/UPPER_ATMOSPHERE_SURROGATE_SCALE_HEIGHT_M)
        if altitude_m >= 1_000_000:
            pressure = 0.0
    if altitude_m < 0:
        regime = "sea_level_clamp_below_ellipsoid"
    return {"density_kg_m3": pressure/(AIR_SPECIFIC_GAS_CONSTANT*temperature),
            "pressure_pa": pressure, "temperature_k": temperature,
            "speed_of_sound_mps": math.sqrt(1.4*AIR_SPECIFIC_GAS_CONSTANT*temperature),
            "regime": regime}


def aerodynamic_force(state: State3D, vehicle: Vehicle, control: Control) -> Vector:
    velocity = air_relative_velocity(state)
    speed = norm(velocity)
    if speed < 1e-9 or vehicle.area_m2 == 0:
        return 0.0, 0.0, 0.0
    _, _, altitude = ecef_to_geodetic(state.r)  # Axisymmetric ellipsoid; rotation preserves h.
    density = standard_atmosphere(altitude)["density_kg_m3"]
    q_area = 0.5*density*speed*speed*vehicle.area_m2
    flow = scale(velocity, 1/speed)
    drag = scale(flow, -q_area*vehicle.cd)
    if vehicle.cl == 0:
        return drag
    radial = unit(state.r)
    lift_up = add(radial, scale(flow, -dot(radial, flow)))
    if norm(lift_up) < 1e-10:  # Vertical motion: deterministic transverse direction.
        candidate = (0.0, 0.0, 1.0) if abs(flow[2]) < 0.9 else (1.0, 0.0, 0.0)
        lift_up = add(candidate, scale(flow, -dot(candidate, flow)))
    lift_up = unit(lift_up)
    lift = add(scale(lift_up, math.cos(control.bank_rad)), scale(cross(flow, lift_up), math.sin(control.bank_rad)))
    return add(drag, scale(lift, q_area*vehicle.cl))


def _rk4_segment(state: State3D, vehicle: Vehicle, control: Control, dt: float,
                 thrust: float, mdot: float, *, j2: bool, atmosphere: bool, gravity: bool) -> State3D:
    thrust_force = scale(unit(control.thrust_direction_eci), thrust) if thrust else (0.0, 0.0, 0.0)

    def derivative(offset: float, r: Vector, v: Vector) -> tuple[Vector, Vector]:
        prop = max(0.0, state.propellant_kg-mdot*offset)
        interim = State3D(state.time_s+offset, r, v, prop)
        force = thrust_force
        if atmosphere:
            force = add(force, aerodynamic_force(interim, vehicle, control))
        acceleration = scale(force, 1/(vehicle.dry_mass_kg+prop))
        if gravity:
            acceleration = add(acceleration, gravity_acceleration(r, j2=j2))
        return v, acceleration

    a, b = derivative(0.0, state.r, state.v)
    c, d = derivative(dt/2, add(state.r, scale(a, dt/2)), add(state.v, scale(b, dt/2)))
    e, f = derivative(dt/2, add(state.r, scale(c, dt/2)), add(state.v, scale(d, dt/2)))
    g, h = derivative(dt, add(state.r, scale(e, dt)), add(state.v, scale(f, dt)))
    r = add(state.r, scale(add(add(a, scale(c, 2)), add(scale(e, 2), g)), dt/6))
    v = add(state.v, scale(add(add(b, scale(d, 2)), add(scale(f, 2), h)), dt/6))
    return State3D(state.time_s+dt, r, v, max(0.0, state.propellant_kg-mdot*dt))


def _segment_with_contact(state: State3D, vehicle: Vehicle, control: Control,
                          dt: float, thrust: float, mdot: float, *, j2: bool,
                          atmosphere: bool, gravity: bool, stop_at_ground: bool) -> tuple[State3D, bool]:
    kwargs = {"j2": j2, "atmosphere": atmosphere, "gravity": gravity}
    result = _rk4_segment(state, vehicle, control, dt, thrust, mdot, **kwargs)
    if not stop_at_ground or ecef_to_geodetic(result.r)[2] > 0:
        return result, False
    lo, hi = 0.0, dt
    # Contact is defined by a root of WGS84 height; no velocity or energy reset.
    for _ in range(45):
        mid = (lo+hi)/2
        candidate = _rk4_segment(state, vehicle, control, mid, thrust, mdot, **kwargs)
        if ecef_to_geodetic(candidate.r)[2] > 0:
            lo = mid
        else:
            hi = mid
    contact = _rk4_segment(state, vehicle, control, (lo+hi)/2, thrust, mdot, **kwargs)
    lat, lon, _ = ecef_to_geodetic(contact.r)
    return State3D(contact.time_s, geodetic_to_ecef(lat, lon, 0.0), contact.v, contact.propellant_kg), True


def step(state: State3D, vehicle: Vehicle, control: Control, dt_s: float, *,
         j2: bool = True, atmosphere: bool = True, gravity: bool = True,
         stop_at_ground: bool = True) -> State3D:
    """Advance held control, truncating on impact and splitting at empty fuel.

    Callers must resolve guidance/engine/staging discontinuities at their exact
    times and select a converged integration step. This is fixed-step RK4, not
    an adaptive stiff integrator. Ground contact models the ellipsoid only.
    """
    _finite(dt_s, "dt_s")
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")
    if stop_at_ground:
        altitude = ecef_to_geodetic(state.r)[2]
        if altitude < -1e-4:
            raise ValueError("initial state is below ground")
        if altitude <= 1e-7 and dot(air_relative_velocity(state), local_frame(state)[0]) < -1e-9:
            return state  # Preserve a completed impact; no additional time/fuel credit.
    thrust = vehicle.max_thrust_n*control.throttle if state.propellant_kg > 0 else 0.0
    mdot = thrust/(vehicle.isp_s*STANDARD_GRAVITY_MPS2)
    powered_duration = min(dt_s, state.propellant_kg/mdot) if mdot else dt_s
    kwargs = {"j2": j2, "atmosphere": atmosphere, "gravity": gravity, "stop_at_ground": stop_at_ground}
    result, contact = _segment_with_contact(state, vehicle, control, powered_duration, thrust, mdot, **kwargs)
    if not contact and powered_duration < dt_s:
        result, _ = _segment_with_contact(result, vehicle, control, dt_s-powered_duration, 0.0, 0.0, **kwargs)
    return result


def orbital_elements(state: State3D) -> dict:
    """Osculating two-body elements; altitudes use WGS84 equatorial radius.

    Osculating elements are diagnostics under J2, not constants of that motion.
    A positive perigee above the equatorial radius conservatively clears the
    ellipsoid but does not establish long-term survival or satellite service.
    """
    radius = norm(state.r)
    angular = cross(state.r, state.v)
    h = norm(angular)
    energy = specific_energy(state, j2=False)
    evector = add(scale(cross(state.v, angular), 1/EARTH_MU_M3_S2), scale(state.r, -1/radius))
    eccentricity = norm(evector)
    bound = energy < 0 and eccentricity < 1 and h > 1e-6
    semimajor = -EARTH_MU_M3_S2/(2*energy) if bound else None
    perigee = h*h/(EARTH_MU_M3_S2*(1+eccentricity))
    inclination = math.acos(max(-1.0, min(1.0, angular[2]/h))) if h > 1e-6 else None
    return {"status": "bound" if bound else "unbound_or_degenerate",
            "eccentricity": eccentricity, "semimajor_axis_m": semimajor,
            "inclination_deg": math.degrees(inclination) if inclination is not None else None,
            "perigee_altitude_m": perigee-EARTH_EQUATORIAL_RADIUS_M,
            "apogee_altitude_m": semimajor*(1+eccentricity)-EARTH_EQUATORIAL_RADIUS_M if bound else None,
            "period_s": 2*math.pi*math.sqrt(semimajor**3/EARTH_MU_M3_S2) if bound else None,
            "specific_energy_j_kg": energy, "specific_angular_momentum_m2_s": h,
            "altitude_reference": "WGS84_equatorial_radius"}


def observe(state: State3D, vehicle: Vehicle) -> dict:
    lat, lon, altitude = ecef_to_geodetic(eci_to_ecef(state.r, state.time_s))
    atmosphere = standard_atmosphere(altitude)
    relative = air_relative_velocity(state)
    speed = norm(relative)
    up, _, _ = local_frame(state)
    return {"time_s": state.time_s, "r_m": list(state.r), "v_mps": list(state.v),
            "mass_kg": vehicle.dry_mass_kg+state.propellant_kg, "propellant_kg": state.propellant_kg,
            "latitude_deg": math.degrees(lat), "longitude_deg": math.degrees(lon),
            "altitude_m": altitude, "ground_speed_mps": speed, "air_speed_mps": speed,
            "inertial_speed_mps": norm(state.v), "radial_velocity_mps": dot(state.v, unit(state.r)),
            "vertical_speed_mps": dot(relative, up), "density_kg_m3": atmosphere["density_kg_m3"],
            "dynamic_pressure_pa": 0.5*atmosphere["density_kg_m3"]*speed*speed,
            "mach": speed/atmosphere["speed_of_sound_mps"], "atmosphere_regime": atmosphere["regime"],
            "specific_energy_with_j2_j_kg": specific_energy(state), "orbit": orbital_elements(state)}
