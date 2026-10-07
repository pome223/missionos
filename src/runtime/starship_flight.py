"""Public-inspired Starship mission using integrated 3-D point-mass dynamics.

This is an engineering surrogate, not SpaceX GNC or an identified vehicle model.
Thrust direction and aerodynamic attitude are prescribed by the documented
controller. No position or velocity is overwritten to meet a desired trajectory.
All scenario faults and unreported vehicle inputs are explicit assumptions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from typing import Any

from src.runtime.starship_physics import (
    State3D,
    Vehicle,
    Control,
    step,
    observe,
    surface_state,
    local_frame,
    air_relative_velocity,
    orbital_elements,
    unit,
    add,
    scale,
    dot,
    cross,
    norm,
    EARTH_MU_M3_S2,
    STANDARD_GRAVITY_MPS2,
)


@dataclass(frozen=True)
class FlightProfile:
    """Frozen inputs. Values lacking public flight-specific evidence are assumed."""

    profile_id: str = "starship-v3-public-inspired-surrogate-v1"
    launch_latitude_deg: float = 25.997
    launch_longitude_deg: float = -97.155
    launch_azimuth_deg: float = 90.0
    booster_dry_mass_kg: float = 250_000.0
    booster_propellant_kg: float = 3_650_000.0
    booster_separation_reserve_kg: float = 260_000.0
    ship_dry_mass_kg: float = 120_000.0
    ship_propellant_kg: float = 1_600_000.0
    satellite_count: int = 26
    satellite_mass_kg: float = 1_700.0
    sea_level_thrust_n: float = 250.0 * 9806.65
    vacuum_thrust_n: float = 275.0 * 9806.65
    booster_isp_s: float = 330.0
    ship_ascent_isp_s: float = 365.0
    ship_sea_level_isp_s: float = 340.0
    frontal_area_m2: float = math.pi * 4.5**2
    ascent_cd: float = 0.35
    entry_area_m2: float = 400.0
    entry_cd: float = 1.3
    entry_cl: float = 0.22
    booster_entry_area_m2: float = 120.0
    booster_entry_cd: float = 0.8
    ship_ascent_reserve_kg: float = 75_000.0
    ship_return_reserve_kg: float = 28_000.0
    suborbital_target_altitude_m: float = 275_000.0
    ascent_altitude_response_s: float = 80.0
    suborbital_perigee_target_m: float = -50_000.0
    minimum_deploy_perigee_m: float = 220_000.0
    orbit_insertion_perigee_m: float = 250_000.0
    deorbit_perigee_m: float = 20_000.0
    orbit_gate_delay_s: float = 60.0
    deployment_interval_s: float = 15.0
    deployment_delay_s: float = 45.0
    deployment_impulse_mps: float = 0.45
    early_return_orbits: float = 1.65
    nominal_return_orbits: float = 5.2
    booster_engine_failure_time_s: float = 60.0
    rvac_failure_after_staging_s: float = 35.0
    landing_relight_altitude_m: float = 1100.0
    terminal_descent_time_constant_s: float = 6.0
    terminal_velocity_response_s: float = 1.5
    terminal_contact_target_mps: float = 2.0
    terminal_flip_duration_s: float = 3.0
    entry_bank_deg: float = 75.0
    max_mission_duration_s: float = 36_000.0
    max_ascent_duration_s: float = 750.0

    def validate(self) -> None:
        for key, value in asdict(self).items():
            if key == "profile_id":
                if not isinstance(value, str) or not value:
                    raise ValueError("profile_id must be nonempty")
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{key} must be finite numeric")
        if not isinstance(self.satellite_count, int) or not 1 <= self.satellite_count <= 100:
            raise ValueError("satellite_count must be 1..100")
        if (
            not -90 <= self.launch_latitude_deg <= 90
            or not -180 <= self.launch_longitude_deg <= 180
        ):
            raise ValueError("invalid launch coordinates")
        for key in (
            "booster_dry_mass_kg",
            "booster_propellant_kg",
            "ship_dry_mass_kg",
            "ship_propellant_kg",
            "satellite_mass_kg",
            "sea_level_thrust_n",
            "vacuum_thrust_n",
            "booster_isp_s",
            "ship_ascent_isp_s",
            "ship_sea_level_isp_s",
            "frontal_area_m2",
            "entry_area_m2",
            "entry_cd",
            "max_mission_duration_s",
            "max_ascent_duration_s",
            "deployment_interval_s",
            "terminal_flip_duration_s",
            "terminal_descent_time_constant_s",
            "ascent_altitude_response_s",
            "terminal_velocity_response_s",
            "terminal_contact_target_mps",
        ):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if not 0 < self.booster_separation_reserve_kg < self.booster_propellant_kg:
            raise ValueError("booster reserve outside tank bounds")
        if (
            not 0
            < self.ship_return_reserve_kg
            < self.ship_ascent_reserve_kg
            < self.ship_propellant_kg
        ):
            raise ValueError("ship reserves must be ordered within tank bounds")
        if (
            not self.deorbit_perigee_m
            < self.minimum_deploy_perigee_m
            <= self.orbit_insertion_perigee_m
        ):
            raise ValueError("orbit thresholds must be ordered")
        if not 0 <= self.entry_bank_deg <= 180 or not 0 <= self.ascent_cd or not 0 <= self.entry_cl:
            raise ValueError("invalid aerodynamic profile")


SCENARIOS = (
    "flight14_inspired",
    "counterfactual_nominal",
    "orbit_no_go",
    "dispenser_jam",
    "landing_engine_failure",
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _perigee(orbit: dict[str, Any]) -> float:
    return float(orbit["perigee_altitude_m"])


def _state_record(state: State3D) -> dict[str, Any]:
    return {
        "time_s": state.time_s,
        "r": list(state.r),
        "v": list(state.v),
        "r_m": list(state.r),
        "v_mps": list(state.v),
        "propellant_kg": state.propellant_kg,
    }


def _pitch_direction(
    state: State3D, pitch_deg: float, azimuth_deg: float
) -> tuple[float, float, float]:
    up, east, north = local_frame(state)
    azimuth = math.radians(azimuth_deg)
    heading = add(scale(east, math.sin(azimuth)), scale(north, math.cos(azimuth)))
    return unit(
        add(
            scale(up, math.sin(math.radians(pitch_deg))),
            scale(heading, math.cos(math.radians(pitch_deg))),
        )
    )


def _flight_tangent(state: State3D) -> tuple[float, float, float]:
    up = unit(state.r)
    horizontal = add(state.v, scale(up, -dot(state.v, up)))
    return unit(horizontal)


def _sample(
    state: State3D,
    vehicle: Vehicle,
    phase: str,
    control: Control,
    engine_count: int,
    alpha_deg: float,
) -> dict[str, Any]:
    obs = observe(state, vehicle)
    density = float(obs["density_kg_m3"])
    air_speed = float(obs["air_speed_mps"])
    sample = dict(obs)
    sample.update(
        {
            "x_m": state.r[0],
            "y_m": state.r[1],
            "z_m": state.r[2],
            "vx_mps": state.v[0],
            "vy_mps": state.v[1],
            "vz_mps": state.v[2],
            "lat_deg": obs["latitude_deg"],
            "lon_deg": obs["longitude_deg"],
            "speed_mps": obs["ground_speed_mps"],
            "inertial_speed_mps": norm(state.v),
            "propellant_mass_kg": state.propellant_kg,
            "phase": phase,
            "engine_count": engine_count if control.throttle > 0 and state.propellant_kg > 0 else 0,
            "throttle": control.throttle,
            "applied_thrust_n": vehicle.max_thrust_n * control.throttle
            if state.propellant_kg > 0
            else 0.0,
            "thrust_direction_eci": list(control.thrust_direction_eci),
            "propellant_reservoir": "booster_main"
            if phase == "stack_ascent" or phase.startswith("booster_")
            else "ship_main_and_reserve",
            "alpha_deg": alpha_deg,
            "bank_deg": math.degrees(control.bank_rad),
            "q_pa": obs["dynamic_pressure_pa"],
            "heat_rate_w_m2": 1.7415e-4 * math.sqrt(max(0.0, density) / 2.0) * air_speed**3,
        }
    )
    return sample


def _append_sample(trace: list[dict[str, Any]], sample: dict[str, Any]) -> None:
    if trace and sample["time_s"] == trace[-1]["time_s"]:
        trace[-1] = sample
    else:
        trace.append(sample)


def simulate_flight(
    profile: FlightProfile | None = None,
    scenario: str = "flight14_inspired",
    dt_s: float = 1.0,
    sample_interval_s: float = 5.0,
) -> dict[str, Any]:
    """Execute deterministic finite-force dynamics, returning unverified raw evidence.

    The caller must obtain simulation authorization. This function never connects
    to spacecraft, a Gateway, paid services or external mission control.
    """
    p = profile or FlightProfile()
    p.validate()
    if scenario not in SCENARIOS:
        raise ValueError(f"unsupported scenario: {scenario}")
    if isinstance(dt_s, bool) or not math.isfinite(dt_s) or not 0.05 <= dt_s <= 2.0:
        raise ValueError("dt_s must be 0.05..2.0 seconds")
    if (
        isinstance(sample_interval_s, bool)
        or not math.isfinite(sample_interval_s)
        or not dt_s <= sample_interval_s <= 60
    ):
        raise ValueError("sample_interval_s must be dt_s..60 seconds")
    events: list[dict[str, Any]] = []
    ship_trace: list[dict[str, Any]] = []
    booster_trace: list[dict[str, Any]] = []
    records: dict[str, list[dict[str, Any]]] = {"ship": [], "booster": []}
    satellites: dict[str, list[dict[str, Any]]] = {}
    satellite_release_states: dict[str, State3D] = {}
    satellite_release_receipts: list[dict[str, Any]] = []
    payload_kg = p.satellite_count * p.satellite_mass_kg
    ship_initial_mass = p.ship_dry_mass_kg + p.ship_propellant_kg + payload_kg
    state = surface_state(
        p.launch_latitude_deg,
        p.launch_longitude_deg,
        altitude_m=1.0,
        propellant_kg=p.booster_propellant_kg,
    )
    phase = "stack_ascent"
    stage_time: float | None = None
    booster_initial: State3D | None = None
    seco_time: float | None = None
    orbit_time: float | None = None
    insertion_started = False
    insertion_allowed = False
    rvac_failed = False
    booster_engine_failed = False
    next_deploy_time: float | None = None
    deployed = 0
    jammed = False
    return_at: float | None = None
    relight_time: float | None = None
    atmosphere_entry = False
    surface_contact = False
    gate_decision: dict[str, Any] | None = None
    transitions: list[dict[str, Any]] = []
    heat_load_j_m2 = 0.0
    previous_heat_rate = 0.0
    last_sample_time = -math.inf
    early_return = scenario != "counterfactual_nominal"

    def event(name: str, s: State3D, **fields: Any) -> None:
        events.append(
            {"event": name, "time_s": s.time_s, "body": "ship", "state": _state_record(s), **fields}
        )

    def integrate(
        s: State3D,
        vehicle: Vehicle,
        control: Control,
        h: float,
        current_phase: str,
        engines: int,
        alpha: float,
        body: str = "ship",
    ) -> State3D:
        after = step(s, vehicle, control, h)
        # Locate finite-burn orbital cutoff within the accepted RK4 step. This
        # changes only integration duration; no state is projected into orbit.
        threshold = {
            "ship_ascent": p.suborbital_perigee_target_m,
            "orbit_insertion": p.orbit_insertion_perigee_m,
            "deorbit_burn": p.deorbit_perigee_m,
        }.get(current_phase)
        if threshold is not None:
            first_perigee = _perigee(orbital_elements(s))
            last_perigee = _perigee(orbital_elements(after))
            rising = current_phase != "deorbit_burn"
            crossed = (
                first_perigee < threshold <= last_perigee
                if rising
                else last_perigee <= threshold < first_perigee
            )
            if crossed:
                low, high = 0.0, h
                for _ in range(32):
                    middle = (low + high) / 2
                    candidate = step(s, vehicle, control, middle)
                    reached = (
                        _perigee(orbital_elements(candidate)) >= threshold
                        if rising
                        else _perigee(orbital_elements(candidate)) <= threshold
                    )
                    if reached:
                        high = middle
                    else:
                        low = middle
                h = high
                after = step(s, vehicle, control, h)
        records[body].append(
            {
                "phase": current_phase,
                "engine_count": engines,
                "alpha_deg": alpha,
                "before": _state_record(s),
                "after": _state_record(after),
                "dt_s": h,
                "j2": True,
                "atmosphere": True,
                "dt_requested_s": h,
                "dt_actual_s": after.time_s - s.time_s,
                "vehicle": asdict(vehicle),
                "control": asdict(control),
            }
        )
        return after

    event("launch", state, classification="simulation_event", engine_count=33)
    while state.time_s < p.max_mission_duration_s:
        # Every policy decision uses this freshly integrated state.
        if phase == "stack_ascent":
            vehicle = Vehicle(
                p.booster_dry_mass_kg + ship_initial_mass,
                p.frontal_area_m2,
                p.ascent_cd,
                0.0,
                p.booster_isp_s,
                (32 if booster_engine_failed else 33) * p.sea_level_thrust_n,
            )
        else:
            vehicle = Vehicle(
                p.ship_dry_mass_kg + payload_kg,
                p.frontal_area_m2,
                p.ascent_cd,
                0.0,
                p.ship_ascent_isp_s,
                3 * p.sea_level_thrust_n + (2 if rvac_failed else 3) * p.vacuum_thrust_n,
            )
        obs = observe(state, vehicle)
        altitude = obs["altitude_m"]
        vr = obs["radial_velocity_mps"]
        orbit = orbital_elements(state)
        control = Control()
        engines = 0
        alpha = 0.0
        h = dt_s
        if phase == "stack_ascent":
            if (
                early_return
                and not booster_engine_failed
                and state.time_s >= p.booster_engine_failure_time_s
            ):
                booster_engine_failed = True
                event(
                    "booster_ascent_engine_out",
                    state,
                    affected_component="booster",
                    engine_count_after=32,
                    classification="scenario injection; assumed timing, not recovered flight telemetry",
                )
                continue
            # Assumed pitch program, never a position/velocity tracking force.
            t = state.time_s
            if t < 10:
                pitch = 90.0
            elif t < 40:
                pitch = 90.0 - (t - 10) * 0.25
            elif t < 100:
                pitch = 82.5 - (t - 40) * 0.45
            else:
                pitch = max(30.0, 55.5 - (t - 100) * 0.45)
            throttle = 0.7 if obs["dynamic_pressure_pa"] > 35_000 else 1.0
            control = Control(throttle, _pitch_direction(state, pitch, p.launch_azimuth_deg))
            engines = 32 if booster_engine_failed else 33
            reserve_dt = (
                (state.propellant_kg - p.booster_separation_reserve_kg)
                * p.booster_isp_s
                * STANDARD_GRAVITY_MPS2
                / (vehicle.max_thrust_n * throttle)
            )
            if reserve_dt <= 1e-7:
                booster_initial = replace(state, propellant_kg=state.propellant_kg)
                before = _state_record(state)
                state = replace(state, propellant_kg=p.ship_propellant_kg)
                transitions.append(
                    {
                        "body_id": "ship",
                        "reason": "stage_separation",
                        "time_s": state.time_s,
                        "before": before,
                        "after": _state_record(state),
                    }
                )
                stage_time = state.time_s
                event(
                    "hot_stage_separation",
                    state,
                    classification="assumed instantaneous separation; plume and overlap not modeled",
                    stack_before=before,
                    booster_state=_state_record(booster_initial),
                    ship_dry_mass_kg=p.ship_dry_mass_kg + payload_kg,
                    booster_dry_mass_kg=p.booster_dry_mass_kg,
                )
                phase = "ship_ascent"
                continue
            h = min(h, reserve_dt)
            if early_return and not booster_engine_failed:
                h = min(h, p.booster_engine_failure_time_s - state.time_s)
        elif phase == "ship_ascent":
            assert stage_time is not None
            if (
                early_return
                and not rvac_failed
                and state.time_s >= stage_time + p.rvac_failure_after_staging_s
            ):
                rvac_failed = True
                event(
                    "rvac_engine_out",
                    state,
                    engine_type="RVac",
                    engine_count_after=5,
                    classification="scenario injection; assumed timing",
                )
                continue
            up = unit(state.r)
            radial_v = dot(state.v, up)
            tangential_v = norm(add(state.v, scale(up, -radial_v)))
            desired_vr = _clamp(
                (p.suborbital_target_altitude_m - altitude) / p.ascent_altitude_response_s,
                -50.0,
                800.0,
            )
            required_radial_acc = (
                EARTH_MU_M3_S2 / norm(state.r) ** 2
                - tangential_v**2 / norm(state.r)
                + (desired_vr - radial_v) / 35.0
            )
            available_acc = vehicle.max_thrust_n / (vehicle.dry_mass_kg + state.propellant_kg)
            radial_fraction = _clamp(required_radial_acc / available_acc, -0.25, 0.95)
            direction = unit(
                add(
                    scale(up, radial_fraction),
                    scale(_flight_tangent(state), math.sqrt(1 - radial_fraction**2)),
                )
            )
            control = Control(1.0, direction)
            engines = 5 if rvac_failed else 6
            if (
                _perigee(orbit) >= p.suborbital_perigee_target_m - 1e-5
                or state.propellant_kg <= p.ship_ascent_reserve_kg + 1e-6
                or state.time_s >= p.max_ascent_duration_s
            ):
                seco_time = state.time_s
                phase = "suborbital_coast"
                event(
                    "ship_engine_cutoff",
                    state,
                    orbit=orbit,
                    reason="suborbital_perigee_target"
                    if _perigee(orbit) >= p.suborbital_perigee_target_m
                    else "ascent_budget_exhausted",
                )
                continue
            reserve_dt = (
                (state.propellant_kg - p.ship_ascent_reserve_kg)
                * vehicle.isp_s
                * STANDARD_GRAVITY_MPS2
                / vehicle.max_thrust_n
            )
            h = min(h, reserve_dt)
            if early_return and not rvac_failed:
                h = min(h, stage_time + p.rvac_failure_after_staging_s - state.time_s)
        elif phase == "suborbital_coast":
            assert seco_time is not None
            if gate_decision is None and state.time_s >= seco_time + p.orbit_gate_delay_s:
                healthy = scenario != "orbit_no_go"
                # Health is a declared synthetic fault observation. Dynamics and
                # reserve checks are measured from the just-integrated state.
                insertion_allowed = bool(
                    healthy
                    and altitude > 100_000
                    and vr > -100
                    and state.propellant_kg > p.ship_return_reserve_kg + 15_000
                )
                gate_decision = {
                    "observation_time_s": state.time_s,
                    "sea_level_engine_health_ok": True,
                    "orbit_insertion_navigation_health_ok": healthy,
                    "health_evidence_type": "synthetic scenario observation",
                    "altitude_m": altitude,
                    "radial_velocity_mps": vr,
                    "propellant_kg": state.propellant_kg,
                    "decision": "orbit_go" if insertion_allowed else "orbit_no_go",
                    "rules": {
                        "navigation_health_ok": healthy,
                        "altitude_above_100km": altitude > 100_000,
                        "not_rapidly_descending": vr > -100,
                        "return_reserve_present": state.propellant_kg
                        > p.ship_return_reserve_kg + 15_000,
                    },
                }
                event("orbit_health_gate", state, **gate_decision)
            if insertion_allowed and vr < 65:
                phase = "orbit_insertion"
                insertion_started = True
                event("orbit_insertion_ignition", state, engine_count=1, engine_type="sea_level")
                continue
            if altitude < 120_000 and vr < 0:
                phase = "entry"
                event("entry_interface", state, reason="suborbital_continuation")
                atmosphere_entry = True
                continue
            h = min(5 * dt_s, 5.0)
            if gate_decision is None:
                h = min(h, max(1e-6, seco_time + p.orbit_gate_delay_s - state.time_s))
        elif phase == "orbit_insertion":
            vehicle = replace(
                vehicle, isp_s=p.ship_sea_level_isp_s, max_thrust_n=p.sea_level_thrust_n
            )
            if _perigee(orbit) >= p.orbit_insertion_perigee_m - 1e-5:
                orbit_time = state.time_s
                phase = "orbital_coast"
                next_deploy_time = state.time_s + p.deployment_delay_s
                period = 2 * math.pi * math.sqrt((norm(state.r)) ** 3 / EARTH_MU_M3_S2)
                return_at = (
                    state.time_s
                    + (p.early_return_orbits if early_return else p.nominal_return_orbits) * period
                )
                event("orbit_insertion_cutoff", state, orbit=orbit, return_not_before_s=return_at)
                continue
            if state.propellant_kg <= p.ship_return_reserve_kg or altitude < 120_000:
                phase = "entry" if altitude < 120_000 else "suborbital_abort_coast"
                event(
                    "orbit_insertion_aborted",
                    state,
                    reason="reserve_or_altitude_limit",
                    orbit=orbit,
                )
                continue
            control = Control(1.0, _flight_tangent(state))
            engines = 1
            h = min(
                dt_s,
                (state.propellant_kg - p.ship_return_reserve_kg)
                * vehicle.isp_s
                * STANDARD_GRAVITY_MPS2
                / vehicle.max_thrust_n,
            )
        elif phase == "orbital_coast":
            assert next_deploy_time is not None and return_at is not None
            if deployed < p.satellite_count and not jammed and state.time_s >= next_deploy_time:
                clear_orbit = _perigee(orbit) >= p.minimum_deploy_perigee_m
                if scenario == "dispenser_jam" and deployed == 7:
                    jammed = True
                    event(
                        "deployment_inhibited",
                        state,
                        reason="synthetic_dispenser_jam",
                        released_count=deployed,
                        orbit=orbit,
                    )
                elif not clear_orbit:
                    jammed = True
                    event(
                        "deployment_inhibited",
                        state,
                        reason="measured_perigee_below_contract",
                        released_count=deployed,
                        orbit=orbit,
                    )
                else:
                    sat_id = f"sim-starlink-v3-{deployed + 1:02d}"
                    before_mass = vehicle.dry_mass_kg + state.propellant_kg
                    # Equal/opposite impulse conserves momentum. No artificial
                    # position separation; separation develops by integration.
                    release_axis = unit(cross(unit(state.r), _flight_tangent(state)))
                    dv = scale(release_axis, p.deployment_impulse_mps)
                    sat_state = replace(state, v=add(state.v, dv), propellant_kg=0.0)
                    payload_kg -= p.satellite_mass_kg
                    ship_mass_after = p.ship_dry_mass_kg + payload_kg + state.propellant_kg
                    ship_before = _state_record(state)
                    state = replace(
                        state, v=add(state.v, scale(dv, -p.satellite_mass_kg / ship_mass_after))
                    )
                    transitions.append(
                        {
                            "body_id": "ship",
                            "reason": "satellite_release",
                            "time_s": state.time_s,
                            "before": ship_before,
                            "after": _state_record(state),
                            "satellite_id": sat_id,
                        }
                    )
                    satellite_release_states[sat_id] = sat_state
                    deployed += 1
                    receipt = {
                        "satellite_id": sat_id,
                        "time_s": state.time_s,
                        "ship_before": ship_before,
                        "ship_after": _state_record(state),
                        "satellite_state": _state_record(sat_state),
                        "satellite_mass_kg": p.satellite_mass_kg,
                        "ship_mass_before_kg": before_mass,
                        "ship_mass_after_kg": ship_mass_after,
                        "release_impulse_mps": list(dv),
                        "release_observed_in_simulator": True,
                        "communications_verified": False,
                        "service_verified": False,
                    }
                    satellite_release_receipts.append(receipt)
                    event(
                        "satellite_released",
                        state,
                        **{key: value for key, value in receipt.items() if key != "time_s"},
                    )
                    vehicle = replace(vehicle, dry_mass_kg=p.ship_dry_mass_kg + payload_kg)
                next_deploy_time += p.deployment_interval_s
            if state.time_s >= return_at:
                phase = "deorbit_burn"
                event(
                    "early_return_selected" if early_return else "planned_return_selected",
                    state,
                    reason="synthetic persistent Rvac fault; available return epoch"
                    if early_return
                    else "assumed orbit-duration budget",
                    observed_fault=rvac_failed,
                    released_count=deployed,
                    residual_propellant_kg=state.propellant_kg,
                    decision_evidence="fresh integrated state plus declared synthetic health observations",
                    geographic_targeting_verified=False,
                )
                continue
            if altitude < 120_000 and vr < 0:
                phase = "entry"
                event("entry_interface", state, reason="unexpected_orbital_decay")
                atmosphere_entry = True
                continue
            h = min(10 * dt_s, 10.0, return_at - state.time_s)
            if deployed < p.satellite_count and not jammed:
                h = min(h, max(1e-6, next_deploy_time - state.time_s))
        elif phase == "deorbit_burn":
            vehicle = replace(
                vehicle, isp_s=p.ship_sea_level_isp_s, max_thrust_n=p.sea_level_thrust_n
            )
            if (
                _perigee(orbit) <= p.deorbit_perigee_m + 1e-5
                or state.propellant_kg <= p.ship_return_reserve_kg
            ):
                phase = "deorbit_coast"
                event(
                    "deorbit_cutoff",
                    state,
                    orbit=orbit,
                    deorbit_target_reached=_perigee(orbit) <= p.deorbit_perigee_m,
                )
                continue
            control = Control(1.0, scale(_flight_tangent(state), -1.0))
            engines = 1
            h = min(
                dt_s,
                (state.propellant_kg - p.ship_return_reserve_kg)
                * vehicle.isp_s
                * STANDARD_GRAVITY_MPS2
                / vehicle.max_thrust_n,
            )
        elif phase in ("deorbit_coast", "suborbital_abort_coast"):
            if altitude < 120_000 and vr < 0:
                previous_phase = phase
                phase = "entry"
                event("entry_interface", state, reason=previous_phase)
                atmosphere_entry = True
                continue
            h = min(5 * dt_s, 5.0)
        elif phase == "entry":
            alpha = 70.0
            vehicle = replace(vehicle, area_m2=p.entry_area_m2, cd=p.entry_cd, cl=p.entry_cl)
            # Bank sign changes cross-range direction without moving the state.
            bank = math.radians(p.entry_bank_deg) * (
                1 if math.sin((state.time_s - (orbit_time or 0)) / 90.0) >= 0 else -1
            )
            control = Control(bank_rad=bank)
            if altitude <= 15_000 and obs["air_speed_mps"] < 500:
                phase = "belly_flop"
                event(
                    "belly_flop_started",
                    state,
                    classification="prescribed aerodynamic attitude, not integrated rotation",
                )
                continue
            h = min(dt_s, 0.5)
        elif phase == "belly_flop":
            alpha = 90.0
            vehicle = replace(vehicle, area_m2=p.entry_area_m2, cd=p.entry_cd, cl=0.0)
            if altitude <= p.landing_relight_altitude_m and vr < 0:
                relight_time = state.time_s
                phase = "terminal_flip_burn"
                event(
                    "landing_relight_attempt",
                    state,
                    engines_requested=3,
                    successful=scenario != "landing_engine_failure",
                    classification="synthetic relight outcome",
                )
                continue
            h = min(dt_s, 0.5)
        elif phase == "terminal_flip_burn":
            assert relight_time is not None
            elapsed = state.time_s - relight_time
            fraction = _clamp(elapsed / p.terminal_flip_duration_s, 0.0, 1.0)
            alpha = 90.0 * (1.0 - fraction)
            vehicle = replace(
                vehicle,
                area_m2=p.frontal_area_m2 + (p.entry_area_m2 - p.frontal_area_m2) * (1 - fraction),
                cd=p.entry_cd,
                cl=0.0,
                isp_s=p.ship_sea_level_isp_s,
                max_thrust_n=3 * p.sea_level_thrust_n,
            )
            if scenario != "landing_engine_failure" and state.propellant_kg > 0:
                up, _, _ = local_frame(state)
                ground_v = air_relative_velocity(state)
                radial_v = dot(ground_v, up)
                horizontal = add(ground_v, scale(up, -radial_v))
                desired_radial_v = -min(
                    55.0,
                    max(
                        p.terminal_contact_target_mps, altitude / p.terminal_descent_time_constant_s
                    ),
                )
                desired_acc = add(
                    scale(
                        up,
                        STANDARD_GRAVITY_MPS2
                        + (desired_radial_v - radial_v) / p.terminal_velocity_response_s,
                    ),
                    scale(horizontal, -1 / 2.0),
                )
                force = (vehicle.dry_mass_kg + state.propellant_kg) * norm(desired_acc)
                # Thrust axis rotates smoothly from the belly-flop horizontal
                # direction into the terminal command; rotation itself prescribed.
                direction = unit(
                    add(
                        scale(_flight_tangent(state), 1 - fraction),
                        scale(unit(desired_acc), fraction),
                    )
                )
                control = Control(
                    _clamp(force / vehicle.max_thrust_n, 0.0, 1.0) * fraction, direction
                )
                engines = 3
            h = min(dt_s, 0.1)
        else:
            raise AssertionError(f"unknown phase {phase}")
        if h <= 1e-8:
            raise RuntimeError(f"nonprogressing integration step in {phase}")
        h = min(h, p.max_mission_duration_s - state.time_s)
        before_time = state.time_s
        if not ship_trace:
            _append_sample(ship_trace, _sample(state, vehicle, phase, control, engines, alpha))
        state = integrate(state, vehicle, control, h, phase, engines, alpha)
        sample = _sample(state, vehicle, phase, control, engines, alpha)
        elapsed = state.time_s - before_time
        heat_load_j_m2 += (previous_heat_rate + sample["heat_rate_w_m2"]) * 0.5 * elapsed
        previous_heat_rate = sample["heat_rate_w_m2"]
        sample["heat_load_j_m2"] = heat_load_j_m2
        contact = sample["altitude_m"] <= 1e-4 and state.time_s > 2
        if (
            state.time_s - last_sample_time >= sample_interval_s - 1e-9
            or contact
            or state.time_s >= p.max_mission_duration_s
        ):
            _append_sample(ship_trace, sample)
            last_sample_time = state.time_s
        if contact:
            surface_contact = True
            event(
                "surface_contact",
                state,
                phase=phase,
                ground_speed_mps=sample["speed_mps"],
                radial_velocity_mps=sample["radial_velocity_mps"],
                alpha_deg=alpha,
                classification="point-mass surface crossing; survival/catch/recovery not modeled",
            )
            break
    # Preserve a final state even when the simulation duration expires.
    _append_sample(ship_trace, _sample(state, vehicle, phase, control, engines, alpha))
    ship_trace[-1]["heat_load_j_m2"] = heat_load_j_m2
    if booster_initial is not None:
        booster_trace = _simulate_booster(
            booster_initial, p, dt_s, sample_interval_s, records["booster"], events
        )
    for sat_id, initial in satellite_release_states.items():
        satellite_vehicle = Vehicle(p.satellite_mass_kg, 20.0, 2.2, 0.0, 1.0, 0.0)
        sat = initial
        trace = [_sample(sat, satellite_vehicle, "passive_satellite_coast", Control(), 0, 0.0)]
        while sat.time_s < state.time_s - 1e-7:
            sat = step(
                sat, satellite_vehicle, Control(), min(10 * dt_s, 10.0, state.time_s - sat.time_s)
            )
            trace.append(
                _sample(sat, satellite_vehicle, "passive_satellite_coast", Control(), 0, 0.0)
            )
            if trace[-1]["altitude_m"] <= 1e-4:
                break
        satellites[sat_id] = trace
    final = ship_trace[-1]
    outcomes = {
        "launch_executed_in_simulator": True,
        "stage_separation_observed": booster_initial is not None,
        "orbit_insertion_attempted": insertion_started,
        "orbit_insertion_completed": orbit_time is not None,
        "satellites_released": deployed,
        "payload_release_count_complete": deployed == p.satellite_count,
        "satellite_communications_verified": False,
        "satellite_service_verified": False,
        "ship_surface_contact": surface_contact,
        "ship_soft_contact_candidate": surface_contact
        and final["speed_mps"] <= 5.0
        and final["alpha_deg"] <= 10,
        "ship_contact_ground_speed_mps": final["speed_mps"] if surface_contact else None,
        "ship_return_zone_verified": False,
        "ship_survival_verified": False,
        "booster_surface_contact": bool(booster_trace and booster_trace[-1]["altitude_m"] <= 1e-4),
        "booster_catch_verified": False,
        "mission_completed": False,
        "real_flight_validated": False,
        "tps_survival_validated": False,
        "duration_limit_reached": not surface_contact,
        "entry_interface_observed": atmosphere_entry,
    }
    events.sort(key=lambda item: (item["time_s"], item["body"]))
    return {
        "schema": "starship_flight_simulation.v1",
        "profile": asdict(p),
        "scenario": scenario,
        "integration": {
            "method": "RK4",
            "base_dt_s": dt_s,
            "sample_interval_s": sample_interval_s,
            "coast_max_dt_s": min(10 * dt_s, 10.0),
            "entry_max_dt_s": min(dt_s, 0.5),
            "terminal_max_dt_s": min(dt_s, 0.1),
            "j2": True,
            "earth_rotation": True,
            "attitude_model": "prescribed; no angular equations of motion",
            "epoch_convention": "ECI and ECEF coincide at simulation t=0; not an astronomical epoch",
        },
        "traces": {"ship": ship_trace, "booster": booster_trace, "satellites": satellites},
        "step_records": records,
        "state_transitions": transitions,
        "events": events,
        "release_receipts": satellite_release_receipts,
        "orbit_gate": gate_decision,
        "outcomes": outcomes,
        "limitations": [
            "Assumed vehicle masses, Isp, aerodynamic coefficients, guidance and fault timings; not identified from real telemetry.",
            "Raptor rated thrust and engine counts are public inputs, not actual throttle histories.",
            "Instantaneous stage separation omits hot-staging overlap, plume interaction and separation hardware.",
            "3D translation with prescribed alpha and bank; not 6DOF, not SpaceX control software.",
            "No target landing zone, navigation errors, atmospheric winds or ocean dynamics.",
            "Sutton-Graves heating proxy is not a tile-temperature or survival model.",
            "Synthetic health observations and finite-force deterministic guidance; no LLM runtime invocation.",
            "Satellite point masses, finite impulse and passive orbital propagation only; no electrical, RF, laser, orbit-raising or customer service model.",
            "No actual spacecraft connection; all satellite identifiers are synthetic.",
        ],
    }


def _simulate_booster(
    initial: State3D,
    p: FlightProfile,
    dt_s: float,
    sample_interval_s: float,
    records: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Independent finite-propellant booster disposal trajectory, not a catch."""
    state = initial
    phase = "booster_flip_coast"
    trace: list[dict[str, Any]] = []
    last_sample = -math.inf
    while state.time_s < initial.time_s + 1800:
        vehicle = Vehicle(
            p.booster_dry_mass_kg,
            p.booster_entry_area_m2,
            p.booster_entry_cd,
            0.0,
            p.booster_isp_s,
            13 * p.sea_level_thrust_n,
        )
        obs = observe(state, vehicle)
        control = Control()
        engines = 0
        h = dt_s
        if phase == "booster_flip_coast" and state.time_s >= initial.time_s + 4:
            phase = "booster_boostback"
            events.append(
                {
                    "event": "booster_boostback_ignition",
                    "time_s": state.time_s,
                    "body": "booster",
                    "state": _state_record(state),
                    "engine_count": 13,
                    "classification": "assumed burn for disposable booster study, not flight14 engine history",
                }
            )
        if phase == "booster_boostback":
            up = unit(state.r)
            horizontal_v = add(
                air_relative_velocity(state), scale(up, -dot(air_relative_velocity(state), up))
            )
            control = Control(1.0, unit(add(scale(unit(horizontal_v), -1.0), scale(up, 0.2))))
            engines = 13
            if state.propellant_kg <= 35_000 or norm(horizontal_v) < 50:
                phase = "booster_aerodynamic_descent"
                control = Control()
                engines = 0
                events.append(
                    {
                        "event": "booster_boostback_cutoff",
                        "time_s": state.time_s,
                        "body": "booster",
                        "state": _state_record(state),
                    }
                )
            else:
                h = min(
                    h,
                    (state.propellant_kg - 35_000)
                    * p.booster_isp_s
                    * STANDARD_GRAVITY_MPS2
                    / vehicle.max_thrust_n,
                )
        if phase == "booster_aerodynamic_descent":
            if obs["altitude_m"] < 1000 and obs["radial_velocity_mps"] < 0:
                phase = "booster_landing_burn"
                events.append(
                    {
                        "event": "booster_landing_ignition",
                        "time_s": state.time_s,
                        "body": "booster",
                        "state": _state_record(state),
                        "engine_count": 3,
                    }
                )
        if phase == "booster_landing_burn":
            vehicle = replace(vehicle, max_thrust_n=3 * p.sea_level_thrust_n)
            control = (
                Control(1.0, scale(unit(air_relative_velocity(state)), -1.0))
                if state.propellant_kg > 0
                else Control()
            )
            engines = 3
            h = min(h, 0.1)
        if not trace:
            trace.append(_sample(state, vehicle, phase, control, engines, 0.0))
        after = step(state, vehicle, control, h)
        records.append(
            {
                "phase": phase,
                "engine_count": engines,
                "alpha_deg": 0.0,
                "before": _state_record(state),
                "after": _state_record(after),
                "dt_s": h,
                "j2": True,
                "atmosphere": True,
                "dt_requested_s": h,
                "dt_actual_s": after.time_s - state.time_s,
                "vehicle": asdict(vehicle),
                "control": asdict(control),
            }
        )
        state = after
        sample = _sample(state, vehicle, phase, control, engines, 0.0)
        contact = sample["altitude_m"] <= 1e-4
        if state.time_s - last_sample >= sample_interval_s - 1e-9 or contact:
            _append_sample(trace, sample)
            last_sample = state.time_s
        if contact:
            events.append(
                {
                    "event": "booster_surface_contact",
                    "time_s": state.time_s,
                    "body": "booster",
                    "state": _state_record(state),
                    "ground_speed_mps": sample["speed_mps"],
                    "classification": "point-mass contact; recovery not modeled",
                }
            )
            break
    _append_sample(trace, _sample(state, vehicle, phase, control, engines, 0.0))
    return trace
