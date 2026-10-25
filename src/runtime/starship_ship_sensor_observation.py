"""Opt-in, synthetic observations for the accelerometer-versus-propulsion study.

Independent tracking uses sampled ECI velocity, not accelerometer output or the
plant's engine limits. Attitude, actuator telemetry and the existing fuel gauge
are shared nominal-model inputs. Gravity uses the simulator's J2 environment;
this is a second observation channel, not an independent physics model. The
noise, latency and uncertainty settings below are test assumptions, not measured
Starship or ground-station performance. No function here makes an AI decision.
"""
from copy import deepcopy
import math

import numpy as np

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_ship_capability_observation as obs
from .starship_mission_director import fuel_sensor

ACCELEROMETER = {
    "schema": "missionos.ship_sensor_accelerometer_gain.v1",
    "gain_application": "specific_force_signal_before_existing_bias_and_noise",
    "existing_sensor_model_sha256": obs.digest(obs.SENSOR),
    "basis": "synthetic engineering test, not identified sensor performance",
}
TRACKING = {
    "schema": "missionos.ship_sensor_tracking_model.v1",
    "velocity_noise_sigma_mps": .01,
    "position_noise_sigma_m": 10.,
    "request_latency_s": 30.,
    "sampling": "each powered control step; historical deorbit-burn samples only",
    "velocity_frame": "ECI",
    "gravity_model": "nominal WGS84 central gravity plus J2",
    "minimum_radius_m": env.EARTH_EQUATORIAL_RADIUS_M+120000.,
    "maximum_segment_gap_s": .15,
    "minimum_intervals": 10,
    "minimum_nominal_impulse_mps": 1.,
    "statistical_multiple": 3.,
    "gauge_error_bound_kg": 100.,
    "model_allowance": .01,
    "basis": "synthetic engineering test; no station geometry, outage or multipath model",
}
SOURCE_KEYS = {"time_s", "phase", "r_eci_m", "v_eci_mps", "q_body_to_eci",
    "engine_throttle_states", "engine_gimbal_states_rad", "engine_available", "propellant_kg"}
SAMPLE_KEYS = {"time_s", "phase", "position_eci_m", "velocity_eci_mps", "q_body_to_eci",
    "engine_throttle_states", "engine_gimbal_states_rad", "engine_available", "fuel_gauge_kg"}
DELIVERY_KEYS = {"schema", "requested_at_s", "received_at_s", "samples", "source_sha256",
    "measurement_model_sha256", "delivery_id"}


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid_tracking_"+name)
    return float(value)


def _vector(value, count, name):
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise ValueError("invalid_tracking_"+name)
    return [_number(x, name) for x in value]


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid_tracking_sha256")
    return value


def _validate_row(row, *, source=False, engines=None):
    if not isinstance(row, dict) or set(row) != (SOURCE_KEYS if source else SAMPLE_KEYS):
        raise ValueError("tracking_row_keys_mismatch")
    _number(row["time_s"], "time")
    if not isinstance(row["phase"], str):
        raise ValueError("invalid_tracking_phase")
    _vector(row["r_eci_m" if source else "position_eci_m"], 3, "position")
    _vector(row["v_eci_mps" if source else "velocity_eci_mps"], 3, "velocity")
    q = _vector(row["q_body_to_eci"], 4, "attitude")
    if abs(sum(x*x for x in q)-1.) > 1e-6:
        raise ValueError("invalid_tracking_unit_attitude")
    fuel = _number(row["propellant_kg" if source else "fuel_gauge_kg"], "fuel")
    if fuel < 0:
        raise ValueError("invalid_tracking_fuel")
    throttles, gimbals, available = (row[k] for k in
        ("engine_throttle_states", "engine_gimbal_states_rad", "engine_available"))
    if (not isinstance(throttles, (list, tuple)) or len(throttles) < 3
            or engines is not None and len(throttles) != engines
            or not isinstance(gimbals, (list, tuple)) or len(gimbals) != len(throttles)
            or not isinstance(available, (list, tuple)) or len(available) != len(throttles)):
        raise ValueError("invalid_tracking_engine_telemetry")
    if any(not 0 <= _number(t, "throttle") <= 1 for t in throttles):
        raise ValueError("invalid_tracking_throttle")
    for gimbal in gimbals:
        _vector(gimbal, 2, "gimbal")
    if any(type(x) is not bool for x in available):
        raise ValueError("invalid_tracking_availability")


def _ordered(rows, *, source=False, engines=None):
    if not isinstance(rows, (list, tuple)):
        raise ValueError("invalid_tracking_rows")
    previous = -math.inf
    for row in rows:
        _validate_row(row, source=source, engines=engines)
        if row["time_s"] <= previous:
            raise ValueError("tracking_samples_not_strictly_ordered")
        previous = row["time_s"]


def measure_accelerometer(rows, factor_index, seed, *, gain=1.):
    """Apply opt-in signal gain; gain=1 preserves every existing measured byte.

    Source rows are sensor-model truth, kept offline. The returned rows have the
    old measured schema and contain neither the gain nor a fault/plant label.
    Existing downstream bias, noise, gauge and estimator semantics are unchanged.
    """
    gain = _number(gain, "accelerometer_gain")
    if not .3 <= gain <= 1.2:
        raise ValueError("accelerometer_gain_outside_registered_model")
    scaled = deepcopy(rows)
    if gain != 1.:
        for row in scaled:
            row["true_specific_force_body_mps2"] = [
                gain*x for x in _vector(row["true_specific_force_body_mps2"], 3, "specific_force")]
    return obs.measure(scaled, factor_index, seed)


def tracking_source_row(state, phase):
    """Sample kinematics and actuator readouts, with no plant object accepted."""
    row = {"time_s": state.time_s, "phase": phase,
        "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
        "q_body_to_eci": list(state.q_body_to_eci),
        "engine_throttle_states": [e.throttle for e in state.engine_states],
        "engine_gimbal_states_rad": [[e.gimbal_x_rad, e.gimbal_y_rad] for e in state.engine_states],
        "engine_available": [e.available for e in state.engine_states],
        "propellant_kg": state.propellant_kg}
    _validate_row(row, source=True)
    return row


def measure_tracking(source_rows, seed):
    """A separate RNG stream measures kinematics; accelerometer rows are rejected."""
    if type(seed) is not int or seed < 0:
        raise ValueError("invalid_tracking_seed")
    _ordered(source_rows, source=True)
    rng = np.random.default_rng([20261015, 91, seed])
    samples = []
    for row in source_rows:
        position_noise = rng.normal(0., TRACKING["position_noise_sigma_m"], 3)
        velocity_noise = rng.normal(0., TRACKING["velocity_noise_sigma_mps"], 3)
        samples.append({"time_s": row["time_s"], "phase": row["phase"],
            "position_eci_m": [float(x+n) for x, n in zip(row["r_eci_m"], position_noise)],
            "velocity_eci_mps": [float(x+n) for x, n in zip(row["v_eci_mps"], velocity_noise)],
            "q_body_to_eci": list(row["q_body_to_eci"]),
            "engine_throttle_states": list(row["engine_throttle_states"]),
            "engine_gimbal_states_rad": deepcopy(row["engine_gimbal_states_rad"]),
            "engine_available": list(row["engine_available"]),
            "fuel_gauge_kg": fuel_sensor(row["propellant_kg"], row["time_s"], "ship")})
    return samples


def request_tracking(samples, requested_at_s, source_sha256):
    """Acquire already sampled burn data only after a real request plus latency.

    A source hash is a provenance link, not a truth assertion. Neither a pending
    delivery nor its rows should be exposed to the AI before received_at_s.
    """
    _ordered(samples)
    requested_at_s = _number(requested_at_s, "requested_at")
    delivery = {"schema": "missionos.ship_sensor_tracking_delivery.v1",
        "requested_at_s": requested_at_s,
        "received_at_s": requested_at_s+TRACKING["request_latency_s"],
        "samples": deepcopy([r for r in samples
            if r["phase"] == "deorbit_burn" and r["time_s"] <= requested_at_s]),
        "source_sha256": _sha(source_sha256),
        "measurement_model_sha256": obs.digest(TRACKING)}
    delivery["delivery_id"] = obs.digest(delivery)
    return delivery


def validate_delivery(delivery, observed_at_s):
    """Enforce request binding, data chronology, complete schema and arrival."""
    if not isinstance(delivery, dict) or set(delivery) != DELIVERY_KEYS:
        raise ValueError("tracking_delivery_keys_mismatch")
    if (delivery["schema"] != "missionos.ship_sensor_tracking_delivery.v1"
            or delivery["measurement_model_sha256"] != obs.digest(TRACKING)
            or delivery["delivery_id"] != obs.digest({k: v for k, v in delivery.items() if k != "delivery_id"})):
        raise ValueError("tracking_delivery_binding_mismatch")
    requested = _number(delivery["requested_at_s"], "requested_at")
    received = _number(delivery["received_at_s"], "received_at")
    observed = _number(observed_at_s, "observed_at")
    _sha(delivery["source_sha256"])
    if received != requested+TRACKING["request_latency_s"]:
        raise ValueError("tracking_latency_mismatch")
    if observed < received:
        raise ValueError("tracking_not_received")
    _ordered(delivery["samples"])
    if any(r["time_s"] > requested or r["phase"] != "deorbit_burn" for r in delivery["samples"]):
        raise ValueError("tracking_future_or_wrong_phase_sample")
    return delivery


def _expected(row, nominal):
    mass = dyn.mass_properties(nominal, row["fuel_gauge_kg"]).mass_kg
    main, other = np.zeros(3), np.zeros(3)
    for i, engine in enumerate(nominal.engines):
        gx, gy = row["engine_gimbal_states_rad"][i]
        if abs(gx) > engine.max_gimbal_rad+1e-9 or abs(gy) > engine.max_gimbal_rad+1e-9:
            raise ValueError("tracking_gimbal_outside_nominal_limits")
        direction = dyn.rotate(row["q_body_to_eci"], dyn._direction(gx, gy, engine.direction_body))
        force = np.asarray(direction)*row["engine_throttle_states"][i]*engine.max_thrust_n/mass
        if i in obs.MAIN:
            main += force
        else:
            other += force
    return main, other, mass


def estimate_tracking(delivery, nominal, observed_at_s):
    """Velocity-difference estimate with nominal modeled-gravity correction.

    The nominal vehicle MUST be built from the frozen nominal profile, never the
    physical plant. Whole connected burn segments supply observed delta-v; each
    segment contributes independent endpoint velocity noise. No acceleration
    measurement, true capability, actual thrust or true mass is consumed.
    """
    validate_delivery(delivery, observed_at_s)
    rows = delivery["samples"]
    _ordered(rows, engines=len(nominal.engines))
    expected = [_expected(r, nominal) for r in rows]
    main, other, measured, gravity = (np.zeros(3) for _ in range(4))
    intervals, segments, masses, previous_accepted = 0, 0, [], False
    for i, (a, b) in enumerate(zip(rows, rows[1:])):
        dt = b["time_s"]-a["time_s"]
        usable = (dt <= TRACKING["maximum_segment_gap_s"]+1e-9
            and all(all(r["engine_available"][j] for j in obs.MAIN)
                    and sum(r["engine_throttle_states"][j] for j in obs.MAIN)
                        >= obs.ESTIMATOR["minimum_main_throttle_sum"]
                    and env.norm(r["position_eci_m"]) >= TRACKING["minimum_radius_m"] for r in (a, b)))
        if not usable:
            previous_accepted = False
            continue
        if not previous_accepted:
            segments += 1
        previous_accepted = True
        intervals += 1
        main += .5*dt*(expected[i][0]+expected[i+1][0])
        other += .5*dt*(expected[i][1]+expected[i+1][1])
        measured += np.asarray(b["velocity_eci_mps"])-a["velocity_eci_mps"]
        gravity += .5*dt*(np.asarray(env.gravity_acceleration(a["position_eci_m"]))
                         +env.gravity_acceleration(b["position_eci_m"]))
        masses.extend((expected[i][2], expected[i+1][2]))
    result = {"schema": "missionos.ship_sensor_tracking_estimate.v1",
        "observed_at_s": observed_at_s, "received_at_s": delivery["received_at_s"],
        "data_through_s": max((r["time_s"] for r in rows), default=None),
        "accepted_samples": intervals, "segments": segments,
        "factor_estimate": None, "band": None, "half_width_terms": None,
        "nominal_main_impulse_mps": float(np.linalg.norm(main)),
        "source_sha256": delivery["source_sha256"], "delivery_id": delivery["delivery_id"],
        "measurement_model_sha256": delivery["measurement_model_sha256"],
        "provenance": "requested historical independent ECI velocity; nominal J2 gravity, actuator readouts and gauge mass",
        "status": "insufficient_data"}
    impulse = float(np.linalg.norm(main))
    if intervals >= TRACKING["minimum_intervals"] and impulse >= TRACKING["minimum_nominal_impulse_mps"]:
        factor = float(np.dot(main, measured-gravity-other)/(impulse*impulse))
        terms = {"endpoint_velocity_noise": TRACKING["statistical_multiple"]
                *TRACKING["velocity_noise_sigma_mps"]*math.sqrt(2*segments)/impulse,
            "mass_gauge": abs(factor)*TRACKING["gauge_error_bound_kg"]/min(masses),
            "model_allowance": TRACKING["model_allowance"]}
        half = sum(terms.values())
        result.update(status="estimated", factor_estimate=factor,
                      band=[factor-half, factor+half], half_width_terms=terms)
    result["estimate_id"] = obs.digest(result)
    return result
