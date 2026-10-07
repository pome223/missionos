"""Coupled translational/rotational surrogate, not an identified Starship model.

Body +Z is the longitudinal/thrust axis. Scalar-first Hamilton quaternions map
body vectors actively into ECI. r and v refer to the instantaneous retained CoM.
The rigid dry body and homogeneous cylindrical propellant share a body frame.
Fuel has a fixed centroid and constant occupied shape with declining density;
this is NOT a draining free surface, slosh, feed-line or nozzle-flow model.

Variable mass uses the explicitly reduced *instantaneous-property* convention:
  m v_dot = F; I omega_dot = torque - omega x (I omega).
Mass, CoM, and the FULL inertia tensor are recomputed at every RK stage. Their
reported derivatives are not silently inserted as uncompensated -I_dot*omega
loads. Depletion-rate forces, nozzle jet damping and internal transport momentum
are omitted. This is the convention documented by Basilisk's comparison below,
not a momentum-closed variable-mass spacecraft equation. Fixed-mass force-free
runs conserve angular momentum and rotational energy to integration accuracy.

References (equations/conventions, not calibration of this vehicle):
https://avslab.github.io/basilisk/Documentation/simulation/dynamics/spacecraft/spacecraft.html
https://avslab.github.io/basilisk/develop/examples/dynamicsComparison/scenarioCompareVariableMass.html
https://avslab.github.io/basilisk/Documentation/simulation/dynamics/GravityGradientEffector/GravityGradientEffector.html
https://www.grc.nasa.gov/www/k-12/airplane/specimp.html
Environment: starship_physics.py (WGS84/J2/COESA lower atmosphere).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from . import starship_physics as env
from .starship_wind import WindField, wind_from_dict

Vector = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
Matrix = tuple[Vector, Vector, Vector]
ZERO: Vector = (0.0, 0.0, 0.0)
IDENTITY: Quaternion = (1.0, 0.0, 0.0, 0.0)
VARIABLE_MASS_CONVENTION = "instantaneous_properties_without_depletion_rate_loads"


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric")


def _vector(value: Any, name: str, size: int = 3) -> tuple:
    if not isinstance(value, (tuple, list)) or len(value) != size:
        raise ValueError(f"{name} must have {size} components")
    for x in value:
        _finite(x, name)
    return tuple(value)


def _positive(value: float, name: str, *, zero: bool = False) -> None:
    _finite(value, name)
    if value < 0 or (not zero and value == 0):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'}")


def _sub(a: Vector, b: Vector) -> Vector:
    return env.add(a, env.scale(b, -1))


def _mv(a: Matrix, b: Vector) -> Vector:
    return tuple(env.dot(row, b) for row in a)


def _mmadd(a: Matrix, b: Matrix) -> Matrix:
    return tuple(tuple(a[i][j] + b[i][j] for j in range(3)) for i in range(3))


def _mscale(a: Matrix, scale: float) -> Matrix:
    return tuple(tuple(x * scale for x in row) for row in a)


def _parallel(v: Vector) -> Matrix:
    r2 = env.dot(v, v)
    return tuple(tuple((r2 if i == j else 0.0) - v[i] * v[j] for j in range(3)) for i in range(3))


def _matrix(value: Any, name: str) -> Matrix:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{name} must be 3x3")
    m = tuple(_vector(row, name) for row in value)
    scale = max(abs(x) for row in m for x in row)
    if any(abs(m[i][j] - m[j][i]) > 1e-12 * max(1.0, scale) for i in range(3) for j in range(3)):
        raise ValueError(f"{name} must be symmetric")
    # Positive definiteness by Cholesky; full off-diagonal inertia is retained.
    _cholesky(m)
    # A physical inertia has principal moments obeying the triangle inequality:
    # trace(I)/2 * 1 - I is the positive semidefinite second-moment matrix.
    second = tuple(tuple((sum(m[k][k] for k in range(3)) / 2 if i == j else 0) - m[i][j]
                         for j in range(3)) for i in range(3))
    eps = max(1.0, scale) * 1e-10
    if any(second[i][i] < -eps for i in range(3)):
        raise ValueError(f"{name} violates physical inertia inequalities")
    if any(second[i][i]*second[j][j] - second[i][j]**2 < -eps*max(1.0, scale)
           for i in range(3) for j in range(i + 1, 3)):
        raise ValueError(f"{name} violates physical inertia inequalities")
    det = (second[0][0]*(second[1][1]*second[2][2]-second[1][2]**2)
           - second[0][1]*(second[0][1]*second[2][2]-second[1][2]*second[0][2])
           + second[0][2]*(second[0][1]*second[1][2]-second[1][1]*second[0][2]))
    if det < -eps * max(1.0, scale)**2:
        raise ValueError(f"{name} violates physical inertia inequalities")
    return m


def _cholesky(a: Matrix) -> list[list[float]]:
    lower = [[0.0]*3 for _ in range(3)]
    for i in range(3):
        for j in range(i + 1):
            value = a[i][j] - sum(lower[i][k]*lower[j][k] for k in range(j))
            if i == j:
                if value <= 0:
                    raise ValueError("inertia must be positive definite")
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value/lower[j][j]
    return lower


def _solve(a: Matrix, b: Vector) -> Vector:
    lower = _cholesky(a)
    y = [0.0]*3
    for i in range(3):
        y[i] = (b[i] - sum(lower[i][k]*y[k] for k in range(i)))/lower[i][i]
    x = [0.0]*3
    for i in reversed(range(3)):
        x[i] = (y[i] - sum(lower[k][i]*x[k] for k in range(i+1, 3)))/lower[i][i]
    return tuple(x)


def quaternion_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    w, x, y, z = a
    s, i, j, k = b
    return (w*s-x*i-y*j-z*k, w*i+x*s+y*k-z*j,
            w*j-x*k+y*s+z*i, w*k+x*j-y*i+z*s)


def normalize_quaternion(q: Quaternion) -> Quaternion:
    q = _vector(q, "quaternion", 4)
    length = math.sqrt(sum(x*x for x in q))
    if length < 1e-12:
        raise ValueError("zero quaternion")
    return tuple(x/length for x in q)


def rotate(q: Quaternion, v: Vector) -> Vector:
    """Active body-to-ECI rotation; does not prescribe/integrate an attitude."""
    w, x, y, z = normalize_quaternion(q)
    u = (x, y, z)
    return env.add(v, env.scale(env.cross(u, env.add(env.cross(u, v), env.scale(v, w))), 2))


def inverse_rotate(q: Quaternion, v: Vector) -> Vector:
    return rotate((q[0], -q[1], -q[2], -q[3]), v)


def quaternion_conjugate(q: Quaternion) -> Quaternion:
    return q[0], -q[1], -q[2], -q[3]


def quaternion_from_two_vectors(source: Vector, target: Vector) -> Quaternion:
    """Shortest active rotation mapping source onto target (roll unspecified)."""
    a, b = env.unit(_vector(source, "source")), env.unit(_vector(target, "target"))
    cosine = max(-1.0, min(1.0, env.dot(a, b)))
    if cosine < -1 + 1e-12:
        basis = (1.0, 0.0, 0.0) if abs(a[0]) < 0.8 else (0.0, 1.0, 0.0)
        return axis_angle(env.cross(a, basis), math.pi)
    return normalize_quaternion((1.0+cosine, *env.cross(a, b)))


def axis_angle(axis: Vector, angle_rad: float) -> Quaternion:
    _finite(angle_rad, "angle_rad")
    direction = env.unit(_vector(axis, "axis"))
    return (math.cos(angle_rad/2), *env.scale(direction, math.sin(angle_rad/2)))


@dataclass(frozen=True)
class Engine:
    name: str
    position_body_m: Vector
    max_thrust_n: float
    isp_s: float
    min_throttle: float = 0.4
    max_gimbal_rad: float = math.radians(10)
    throttle_time_constant_s: float = 0.35
    throttle_rate_per_s: float = 2.0
    gimbal_time_constant_s: float = 0.15
    gimbal_rate_rad_s: float = math.radians(15)
    direction_body: Vector = (0.0, 0.0, 1.0)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("engine name required")
        object.__setattr__(self, "position_body_m", _vector(self.position_body_m, "engine position"))
        object.__setattr__(self, "direction_body", env.unit(_vector(self.direction_body, "engine direction")))
        for name in ("max_thrust_n", "isp_s", "throttle_time_constant_s", "throttle_rate_per_s",
                     "gimbal_time_constant_s", "gimbal_rate_rad_s"):
            _positive(getattr(self, name), name)
        _finite(self.min_throttle, "min_throttle")
        if not 0 <= self.min_throttle <= 1:
            raise ValueError("min_throttle outside [0,1]")
        _positive(self.max_gimbal_rad, "max_gimbal_rad", zero=True)
        if self.max_gimbal_rad >= math.pi/2:
            raise ValueError("gimbal limit must be less than pi/2")


@dataclass(frozen=True)
class EngineState:
    throttle: float = 0.0
    gimbal_x_rad: float = 0.0
    gimbal_y_rad: float = 0.0
    available: bool = True

    def __post_init__(self) -> None:
        for name in ("throttle", "gimbal_x_rad", "gimbal_y_rad"):
            _finite(getattr(self, name), name)
        if not 0 <= self.throttle <= 1 or type(self.available) is not bool:
            raise ValueError("invalid engine state")


@dataclass(frozen=True)
class EngineCommand:
    enabled: bool = False
    throttle: float = 0.0
    gimbal_x_rad: float = 0.0
    gimbal_y_rad: float = 0.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for name in ("throttle", "gimbal_x_rad", "gimbal_y_rad"):
            _finite(getattr(self, name), name)
        if not 0 <= self.throttle <= 1:
            raise ValueError("throttle outside [0,1]")


@dataclass(frozen=True)
class AeroPanel:
    name: str
    position_body_m: Vector
    normal_body: Vector
    area_m2: float
    normal_coefficient: float = 1.3
    tangential_coefficient: float = 0.02
    hinge_axis_body: Vector = (1.0, 0.0, 0.0)
    max_deflection_rad: float = math.radians(35)
    deflection_time_constant_s: float = 0.4
    deflection_rate_rad_s: float = math.radians(20)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("panel name required")
        for name in ("position_body_m", "normal_body", "hinge_axis_body"):
            object.__setattr__(self, name, _vector(getattr(self, name), name))
        for name in ("normal_body", "hinge_axis_body"):
            object.__setattr__(self, name, env.unit(getattr(self, name)))
        for name in ("area_m2", "deflection_time_constant_s", "deflection_rate_rad_s"):
            _positive(getattr(self, name), name)
        for name in ("normal_coefficient", "tangential_coefficient", "max_deflection_rad"):
            _positive(getattr(self, name), name, zero=True)
        if self.max_deflection_rad >= math.pi/2:
            raise ValueError("panel deflection must be less than pi/2")


@dataclass(frozen=True)
class Vehicle6DOF:
    dry_mass_kg: float
    dry_com_body_m: Vector
    dry_inertia_kg_m2: Matrix
    propellant_capacity_kg: float
    tank_center_body_m: Vector
    tank_radius_m: float
    tank_length_m: float
    engines: tuple[Engine, ...] = ()
    aero_panels: tuple[AeroPanel, ...] = ()
    wind: WindField | None = None

    def __post_init__(self) -> None:
        for name in ("dry_mass_kg", "tank_radius_m", "tank_length_m"):
            _positive(getattr(self, name), name)
        _positive(self.propellant_capacity_kg, "propellant_capacity_kg", zero=True)
        for name in ("dry_com_body_m", "tank_center_body_m"):
            object.__setattr__(self, name, _vector(getattr(self, name), name))
        object.__setattr__(self, "dry_inertia_kg_m2", _matrix(self.dry_inertia_kg_m2, "dry inertia"))
        for name, kind in (("engines", Engine), ("aero_panels", AeroPanel)):
            value = tuple(getattr(self, name))
            if any(not isinstance(v, kind) for v in value) or len({v.name for v in value}) != len(value):
                raise ValueError(f"invalid or duplicate {name}")
            object.__setattr__(self, name, value)
        if self.wind is not None and type(self.wind) is not WindField:
            raise ValueError("wind must be a declared immutable WindField")


@dataclass(frozen=True)
class State6DOF:
    time_s: float
    r_eci_m: Vector
    v_eci_mps: Vector
    q_body_to_eci: Quaternion
    omega_body_rad_s: Vector
    propellant_kg: float
    engine_states: tuple[EngineState, ...] = ()
    flap_angles_rad: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        _finite(self.time_s, "time_s")
        _positive(self.propellant_kg, "propellant_kg", zero=True)
        for name in ("r_eci_m", "v_eci_mps", "omega_body_rad_s"):
            object.__setattr__(self, name, _vector(getattr(self, name), name))
        if env.norm(self.r_eci_m) < 1:
            raise ValueError("position too close to Earth center")
        q = _vector(self.q_body_to_eci, "q_body_to_eci", 4)
        if abs(sum(x*x for x in q)-1) > 1e-8:
            raise ValueError("q_body_to_eci must be unit length")
        object.__setattr__(self, "q_body_to_eci", q)
        if any(not isinstance(x, EngineState) for x in self.engine_states):
            raise ValueError("invalid engine states")
        object.__setattr__(self, "engine_states", tuple(self.engine_states))
        for x in self.flap_angles_rad:
            _finite(x, "flap angle")
        object.__setattr__(self, "flap_angles_rad", tuple(self.flap_angles_rad))


@dataclass(frozen=True)
class Command6DOF:
    engines: tuple[EngineCommand, ...] = ()
    flap_angles_rad: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(x, EngineCommand) for x in self.engines):
            raise ValueError("invalid engine commands")
        object.__setattr__(self, "engines", tuple(self.engines))
        for x in self.flap_angles_rad:
            _finite(x, "flap command")
        object.__setattr__(self, "flap_angles_rad", tuple(self.flap_angles_rad))


@dataclass(frozen=True)
class MassProperties:
    mass_kg: float
    com_body_m: Vector
    inertia_kg_m2: Matrix
    dcom_dpropellant_m_per_kg: Vector
    dinertia_dpropellant_m2: Matrix


def mass_properties(vehicle: Vehicle6DOF, propellant_kg: float) -> MassProperties:
    _positive(propellant_kg, "propellant_kg", zero=True)
    if propellant_kg > vehicle.propellant_capacity_kg + 1e-8:
        raise ValueError("fuel exceeds capacity")
    dry, fuel = vehicle.dry_mass_kg, propellant_kg
    total = dry + fuel
    offset = _sub(vehicle.tank_center_body_m, vehicle.dry_com_body_m)
    com = env.add(vehicle.dry_com_body_m, env.scale(offset, fuel/total))
    r2, l2 = vehicle.tank_radius_m**2, vehicle.tank_length_m**2
    per_mass: Matrix = (((3*r2+l2)/12, 0.0, 0.0),
                        (0.0, (3*r2+l2)/12, 0.0), (0.0, 0.0, r2/2))
    # Reduced-mass parallel-axis contribution includes full products of inertia.
    inertia = _mmadd(vehicle.dry_inertia_kg_m2,
                    _mmadd(_mscale(per_mass, fuel), _mscale(_parallel(offset), dry*fuel/total)))
    d_inertia = _mmadd(per_mass, _mscale(_parallel(offset), (dry/total)**2))
    return MassProperties(total, com, inertia, env.scale(offset, dry/total**2), d_inertia)


def _validate_pair(state: State6DOF, vehicle: Vehicle6DOF, command: Command6DOF) -> None:
    if len(state.engine_states) != len(vehicle.engines) or len(command.engines) != len(vehicle.engines):
        raise ValueError("engine state/command count mismatch")
    if len(state.flap_angles_rad) != len(vehicle.aero_panels) or len(command.flap_angles_rad) != len(vehicle.aero_panels):
        raise ValueError("panel state/command count mismatch")
    mass_properties(vehicle, state.propellant_kg)
    for spec, actual, target in zip(vehicle.engines, state.engine_states, command.engines):
        if any(abs(x) > spec.max_gimbal_rad + 1e-12 for x in
               (actual.gimbal_x_rad, actual.gimbal_y_rad, target.gimbal_x_rad, target.gimbal_y_rad)):
            raise ValueError("gimbal beyond limit")
        if target.enabled and target.throttle < spec.min_throttle:
            raise ValueError("enabled throttle below engine minimum")
        if not target.enabled and target.throttle != 0:
            raise ValueError("disabled command must request zero throttle")
    for spec, actual, target in zip(vehicle.aero_panels, state.flap_angles_rad, command.flap_angles_rad):
        if abs(actual) > spec.max_deflection_rad + 1e-12 or abs(target) > spec.max_deflection_rad + 1e-12:
            raise ValueError("flap beyond limit")


def _direction(x: float, y: float, base: Vector) -> Vector:
    # Active Ry(y) Rx(x) about BODY axes. Fixed-axis RCS uses zero gimbal.
    cx, sx, cy, sy = math.cos(x), math.sin(x), math.cos(y), math.sin(y)
    vx, vy, vz = base[0], cx*base[1]-sx*base[2], sx*base[1]+cx*base[2]
    return cy*vx+sy*vz, vy, -sy*vx+cy*vz


def _rate(actual: float, target: float, tau: float, max_rate: float) -> float:
    # Explicit numerical actuator deadband (dimensionless throttle / radians).
    # It permits coarse coast steps after settling without integrating a tiny
    # stiff residual. No physical angle or attitude is reset to a target.
    if abs(target-actual) <= 1e-10:
        return 0.0
    return max(-max_rate, min(max_rate, (target-actual)/tau))


def _pack(state: State6DOF) -> list[float]:
    values = [*state.r_eci_m, *state.v_eci_mps, *state.q_body_to_eci, *state.omega_body_rad_s, state.propellant_kg]
    for engine in state.engine_states:
        values.extend((engine.throttle, engine.gimbal_x_rad, engine.gimbal_y_rad))
    values.extend(state.flap_angles_rad)
    return values


def _loads(y: list[float], vehicle: Vehicle6DOF, available: tuple[bool, ...], atmosphere: bool,
           *, burning_to_event: bool = False, detailed: bool = True, time_s: float = 0.) -> dict:
    r, v, q, omega = tuple(y[:3]), tuple(y[3:6]), normalize_quaternion(tuple(y[6:10])), tuple(y[10:13])
    fuel = max(0.0, y[13])
    props = mass_properties(vehicle, fuel)
    thrust, torque, mdot = ZERO, ZERO, 0.0
    engine_loads = []
    for i, spec in enumerate(vehicle.engines):
        throttle, gx, gy = y[14+3*i:17+3*i]
        magnitude = spec.max_thrust_n * max(0.0, min(1.0, throttle)) if (fuel > 0 or burning_to_event) and available[i] else 0.0
        if magnitude == 0 and not detailed:
            continue
        force = env.scale(_direction(gx, gy, spec.direction_body), magnitude)
        moment = env.cross(_sub(spec.position_body_m, props.com_body_m), force)
        mass_rate = magnitude/(spec.isp_s * env.STANDARD_GRAVITY_MPS2)
        thrust, torque, mdot = env.add(thrust, force), env.add(torque, moment), mdot+mass_rate
        if detailed:
            engine_loads.append({"name": spec.name, "force_body_n": force, "torque_body_nm": moment,
                                 "thrust_n": magnitude, "mass_flow_kg_s": mass_rate})
    aero_force, aero_torque, panel_loads = ZERO, ZERO, []
    altitude = env.ecef_to_geodetic(r)[2]
    air = env.standard_atmosphere(altitude)
    atmosphere_velocity = env.cross((0.0, 0.0, env.EARTH_ROTATION_RAD_S), r)
    ground_relative = _sub(v, atmosphere_velocity)
    active_wind = vehicle.wind is not None and vehicle.wind.active
    wind_enu, wind_eci = ZERO, ZERO
    if active_wind and atmosphere:
        wind_enu = vehicle.wind.enu(time_s)
        wind_eci = vehicle.wind.eci(r, time_s)
        relative = inverse_rotate(q, _sub(ground_relative, wind_eci))
    else:
        # Preserve the old arithmetic exactly for absent/all-zero wind.
        relative = inverse_rotate(q, ground_relative)
    for i, panel in enumerate(vehicle.aero_panels):
        if not atmosphere and not detailed:
            continue
        lever = _sub(panel.position_body_m, props.com_body_m)
        local_velocity = env.add(relative, env.cross(omega, lever))
        angle = y[14+3*len(vehicle.engines)+i]
        normal = rotate(axis_angle(panel.hinge_axis_body, angle), panel.normal_body)
        normal_speed = env.dot(local_velocity, normal)
        tangent = _sub(local_velocity, env.scale(normal, normal_speed))
        scale = 0.5*air["density_kg_m3"]*panel.area_m2 if atmosphere else 0.0
        # A bidirectional drag plate, explicitly not a calibrated hypersonic table.
        force = env.add(env.scale(normal, -scale*panel.normal_coefficient*normal_speed*abs(normal_speed)),
                        env.scale(tangent, -scale*panel.tangential_coefficient*env.norm(tangent)))
        moment = env.cross(lever, force)
        aero_force, aero_torque = env.add(aero_force, force), env.add(aero_torque, moment)
        if detailed:
            panel_loads.append({"name": panel.name, "force_body_n": force, "torque_body_nm": moment,
                                "normal_body": normal, "local_air_velocity_body_mps": local_velocity})
    result = {"mass_properties": props, "thrust_force_body_n": thrust,
            "thrust_torque_body_nm": torque, "aero_force_body_n": aero_force,
            "aero_torque_body_nm": aero_torque, "mass_flow_kg_s": mdot,
            "engine_loads": engine_loads, "panel_loads": panel_loads,
            "air_velocity_body_mps": relative, "atmosphere": air, "altitude_m": altitude}
    if active_wind:
        result.update(wind_applied=atmosphere, wind_enu_mps=list(wind_enu), wind_eci_mps=list(wind_eci),
                      ground_speed_mps=env.norm(ground_relative), air_density_kg_m3=air["density_kg_m3"])
    return result


def _rhs(y: list[float], vehicle: Vehicle6DOF, command: Command6DOF,
         available: tuple[bool, ...], gravity: bool, j2: bool, atmosphere: bool,
         external_force_body_n: Vector = ZERO, external_torque_body_nm: Vector = ZERO,
         *, burning_to_event: bool = False, gravity_gradient: bool = True, time_s: float = 0.) -> list[float]:
    loads = _loads(y, vehicle, available, atmosphere, burning_to_event=burning_to_event, detailed=False, time_s=time_s)
    props = loads["mass_properties"]
    q, omega = normalize_quaternion(tuple(y[6:10])), tuple(y[10:13])
    force = env.add(env.add(loads["thrust_force_body_n"], loads["aero_force_body_n"]), external_force_body_n)
    accel = env.scale(rotate(q, force), 1/props.mass_kg)
    if gravity:
        accel = env.add(accel, env.gravity_acceleration(tuple(y[:3]), j2=j2))
    torque = env.add(env.add(loads["thrust_torque_body_nm"], loads["aero_torque_body_nm"]), external_torque_body_nm)
    if gravity and gravity_gradient:
        torque = env.add(torque, gravity_gradient_torque(tuple(y[:3]), q, props.inertia_kg_m2))
    omega_dot = _solve(props.inertia_kg_m2, _sub(torque, env.cross(omega, _mv(props.inertia_kg_m2, omega))))
    q_dot = tuple(0.5*x for x in quaternion_multiply(q, (0.0, *omega)))
    out = [*y[3:6], *accel, *q_dot, *omega_dot, -loads["mass_flow_kg_s"]]
    for i, (spec, target) in enumerate(zip(vehicle.engines, command.engines)):
        actual = y[14+3*i:17+3*i]
        target_throttle = target.throttle if target.enabled and available[i] and (y[13] > 0 or burning_to_event) else 0.0
        out.extend((_rate(actual[0], target_throttle, spec.throttle_time_constant_s, spec.throttle_rate_per_s),
                    _rate(actual[1], target.gimbal_x_rad, spec.gimbal_time_constant_s, spec.gimbal_rate_rad_s),
                    _rate(actual[2], target.gimbal_y_rad, spec.gimbal_time_constant_s, spec.gimbal_rate_rad_s)))
    for i, (spec, target) in enumerate(zip(vehicle.aero_panels, command.flap_angles_rad)):
        out.append(_rate(y[14+3*len(vehicle.engines)+i], target,
                         spec.deflection_time_constant_s, spec.deflection_rate_rad_s))
    return out


def gravity_gradient_torque(r_eci_m: Vector, q_body_to_eci: Quaternion, inertia_kg_m2: Matrix) -> Vector:
    """First-order CENTRAL-field torque, about the instantaneous retained CoM.

    n is the radial unit vector in body coordinates. J2 tidal derivatives and
    higher multipoles of the vehicle/field are not included in this torque.
    """
    radius = env.norm(r_eci_m)
    if radius < 1:
        raise ValueError("gravity gradient undefined near Earth center")
    n_body = inverse_rotate(q_body_to_eci, env.scale(r_eci_m, 1/radius))
    return env.scale(env.cross(n_body, _mv(inertia_kg_m2, n_body)), 3*env.EARTH_MU_M3_S2/radius**3)


def derivatives(state: State6DOF, vehicle: Vehicle6DOF, command: Command6DOF, *,
                gravity: bool = True, j2: bool = True, atmosphere: bool = True,
                external_force_body_n: Vector = ZERO, external_torque_body_nm: Vector = ZERO,
                gravity_gradient: bool = True) -> list[float]:
    """Derivative in packed order r,v,q,omega,fuel,each throttle/gx/gy,flaps."""
    _validate_pair(state, vehicle, command)
    if any(type(flag) is not bool for flag in (gravity, j2, atmosphere, gravity_gradient)):
        raise ValueError("environment switches must be boolean")
    return _rhs(_pack(state), vehicle, command, tuple(e.available for e in state.engine_states), gravity, j2, atmosphere,
                _vector(external_force_body_n, "external force"), _vector(external_torque_body_nm, "external torque"),
                gravity_gradient=gravity_gradient, time_s=state.time_s)


def _rk4(y: list[float], vehicle: Vehicle6DOF, command: Command6DOF,
         available: tuple[bool, ...], dt: float, gravity: bool, j2: bool, atmosphere: bool,
         external_force_body_n: Vector, external_torque_body_nm: Vector, gravity_gradient: bool,
         time_s: float = 0.) -> list[float]:
    def rhs(v: list[float], clock: float) -> list[float]:
        return _rhs(v, vehicle, command, available, gravity, j2, atmosphere,
                    external_force_body_n, external_torque_body_nm, burning_to_event=y[13] > 0,
                    gravity_gradient=gravity_gradient, time_s=clock)
    a = rhs(y, time_s)
    b = rhs([x+dt*k/2 for x, k in zip(y, a)], time_s+dt/2)
    c = rhs([x+dt*k/2 for x, k in zip(y, b)], time_s+dt/2)
    d = rhs([x+dt*k for x, k in zip(y, c)], time_s+dt)
    out = [x+dt*(ka+2*kb+2*kc+kd)/6 for x, ka, kb, kc, kd in zip(y, a, b, c, d)]
    out[6:10] = normalize_quaternion(tuple(out[6:10]))
    return out


def step(state: State6DOF, vehicle: Vehicle6DOF, command: Command6DOF, dt_s: float, *,
         gravity: bool = True, j2: bool = True, atmosphere: bool = True,
         external_force_body_n: Vector = ZERO, external_torque_body_nm: Vector = ZERO,
         gravity_gradient: bool = True) -> State6DOF:
    """RK4 with actuator/stability substeps and fuel depletion event splitting.

    No launch-pad constraint, surface reset, landing clamp, attitude tracking or
    guidance is hidden here. A caller must stop when its termination condition
    is crossed; a below-surface state is a failed trajectory, never a landing.
    """
    _positive(dt_s, "dt_s")
    if dt_s > 10:
        raise ValueError("dt_s must be at most 10 seconds")
    for flag in (gravity, j2, atmosphere, gravity_gradient):
        if type(flag) is not bool:
            raise ValueError("environment switches must be boolean")
    _validate_pair(state, vehicle, command)
    external_force_body_n = _vector(external_force_body_n, "external force")
    external_torque_body_nm = _vector(external_torque_body_nm, "external torque")
    y, remaining = _pack(state), dt_s
    available = tuple(e.available for e in state.engine_states)
    iterations = 0
    elapsed = 0.
    while remaining > 1e-13:
        iterations += 1
        if iterations > 200_000:
            raise ValueError("integration budget exhausted")
        constants = [4.0]
        for i, (spec, target) in enumerate(zip(vehicle.engines, command.engines)):
            actual = y[14+3*i:17+3*i]
            throttle_target = target.throttle if target.enabled and available[i] and y[13] > 0 else 0.0
            if abs(actual[0]-throttle_target) > 1e-10:
                constants.append(spec.throttle_time_constant_s)
            if abs(actual[1]-target.gimbal_x_rad) > 1e-10 or abs(actual[2]-target.gimbal_y_rad) > 1e-10:
                constants.append(spec.gimbal_time_constant_s)
        for i, (spec, target) in enumerate(zip(vehicle.aero_panels, command.flap_angles_rad)):
            if abs(y[14+3*len(vehicle.engines)+i]-target) > 1e-10:
                constants.append(spec.deflection_time_constant_s)
        maximum = min(constants)/4
        if atmosphere and vehicle.wind is not None and vehicle.wind.gust_active:
            maximum = min(maximum, vehicle.wind.gust_period_s/32)
        angular_dt = 0.1/max(env.norm(tuple(y[10:13])), 1e-12)
        dt = min(remaining, maximum, angular_dt)
        trial = _rk4(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                     external_force_body_n, external_torque_body_nm, gravity_gradient, state.time_s+elapsed)
        if trial[13] < 0:
            # Find first empty-tank endpoint using the same continuous RHS.
            low, high = 0.0, dt
            for _ in range(50):
                mid = (low+high)/2
                candidate = _rk4(y, vehicle, command, available, mid, gravity, j2, atmosphere,
                                 external_force_body_n, external_torque_body_nm, gravity_gradient, state.time_s+elapsed)
                if candidate[13] >= 0:
                    low = mid
                else:
                    high = mid
            dt = max(low, 1e-14)
            trial = _rk4(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                         external_force_body_n, external_torque_body_nm, gravity_gradient, state.time_s+elapsed)
            trial[13] = 0.0
        elif trial[13] < 1e-12:
            trial[13] = 0.0
        y, remaining = trial, max(0.0, remaining-dt)
        elapsed += dt
    engines = tuple(EngineState(max(0.0, min(1.0, y[14+3*i])), y[15+3*i], y[16+3*i], available[i])
                    for i in range(len(vehicle.engines)))
    return State6DOF(state.time_s+dt_s, tuple(y[:3]), tuple(y[3:6]), tuple(y[6:10]), tuple(y[10:13]),
                     y[13], engines, tuple(y[14+3*len(vehicle.engines):]))


def observe(state: State6DOF, vehicle: Vehicle6DOF, command: Command6DOF | None = None, *,
            atmosphere: bool = True, external_force_body_n: Vector = ZERO,
            external_torque_body_nm: Vector = ZERO, gravity: bool = True,
            gravity_gradient: bool = True) -> dict:
    """Actual integrated attitude/rates and force decomposition; no success flag."""
    if command is None:
        command = Command6DOF(tuple(EngineCommand() for _ in vehicle.engines), tuple(state.flap_angles_rad))
    _validate_pair(state, vehicle, command)
    if any(type(flag) is not bool for flag in (gravity, atmosphere, gravity_gradient)):
        raise ValueError("environment switches must be boolean")
    external_force_body_n = _vector(external_force_body_n, "external force")
    external_torque_body_nm = _vector(external_torque_body_nm, "external torque")
    loads = _loads(_pack(state), vehicle, tuple(e.available for e in state.engine_states), atmosphere, time_s=state.time_s)
    props = loads.pop("mass_properties")
    angular_momentum_body = _mv(props.inertia_kg_m2, state.omega_body_rad_s)
    relative = loads["air_velocity_body_mps"]
    lat, lon, altitude = env.ecef_to_geodetic(env.eci_to_ecef(state.r_eci_m, state.time_s))
    speed = env.norm(relative)
    return {"time_s": state.time_s, "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
            "q_body_to_eci": list(state.q_body_to_eci), "omega_body_rad_s": list(state.omega_body_rad_s),
            "body_z_eci": list(rotate(state.q_body_to_eci, (0.0, 0.0, 1.0))),
            "propellant_kg": state.propellant_kg, "mass_kg": props.mass_kg,
            "com_body_m": list(props.com_body_m), "inertia_kg_m2": props.inertia_kg_m2,
            "dcom_dpropellant_m_per_kg": props.dcom_dpropellant_m_per_kg,
            "dinertia_dpropellant_m2": props.dinertia_dpropellant_m2,
            "inertia_rate_kg_m2_s": _mscale(props.dinertia_dpropellant_m2, -loads["mass_flow_kg_s"]),
            "com_rate_body_mps": env.scale(props.dcom_dpropellant_m_per_kg, -loads["mass_flow_kg_s"]),
            "angular_momentum_eci_kg_m2_s": list(rotate(state.q_body_to_eci, angular_momentum_body)),
            "rotational_energy_j": 0.5*env.dot(state.omega_body_rad_s, angular_momentum_body),
            "latitude_deg": math.degrees(lat), "longitude_deg": math.degrees(lon), "altitude_m": altitude,
            "air_speed_mps": speed, "mach": speed/loads["atmosphere"]["speed_of_sound_mps"],
            "dynamic_pressure_pa": 0.5*loads["atmosphere"]["density_kg_m3"]*speed**2 if atmosphere else 0.0,
            "angle_of_attack_rad": math.atan2(math.hypot(relative[0], relative[1]), relative[2]) if speed > 0 else None,
            "external_force_body_n": list(external_force_body_n),
            "external_torque_body_nm": list(external_torque_body_nm),
            "gravity_gradient_torque_body_nm": list(gravity_gradient_torque(
                state.r_eci_m, state.q_body_to_eci, props.inertia_kg_m2) if gravity and gravity_gradient else ZERO),
            "central_gravity_gradient_modeled": gravity and gravity_gradient,
            "j2_gravity_gradient_modeled": False,
            "variable_mass_convention": VARIABLE_MASS_CONVENTION,
            "depletion_rate_loads_modeled": False, "six_dof_integrated": True,
            "attitude_prescribed": False, "starship_vehicle_validated": False, **loads}


def state_from_dict(value: dict) -> State6DOF:
    return State6DOF(**{**value, "engine_states": tuple(EngineState(**x) for x in value.get("engine_states", ()))})


def vehicle_from_dict(value: dict) -> Vehicle6DOF:
    return Vehicle6DOF(**{**value, "engines": tuple(Engine(**x) for x in value.get("engines", ())),
                         "aero_panels": tuple(AeroPanel(**x) for x in value.get("aero_panels", ())),
                         "wind": wind_from_dict(value.get("wind"))})


def command_from_dict(value: dict) -> Command6DOF:
    return Command6DOF(**{**value, "engines": tuple(EngineCommand(**x) for x in value.get("engines", ()))})
