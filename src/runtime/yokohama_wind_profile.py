"""Authored single-vehicle harbour wind zones, not measured weather or CFD."""

from __future__ import annotations

import math
import random


PROFILES = {
    "harbor-nominal": {"offshore": 6.0, "harbor": 5.0, "coast": 4.0, "city": 3.0},
    "harbor-upper": {"offshore": 8.0, "harbor": 7.0, "coast": 6.0, "city": 4.0},
}


def gust_schedule(seed):
    """Freeze a bounded schedule before launch; wall-clock delays cannot redraw it."""
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("Gust seed must be an integer in [0, 2**32-1]")
    rng = random.Random(seed)
    events, start = [], rng.uniform(20, 35)
    while start + 10 <= 3600:
        duration, fraction = rng.uniform(6, 10), rng.random()
        events.append(
            dict(id=len(events), start_s=start, end_s=start + duration, fraction=fraction)
        )
        start += duration + rng.uniform(45, 75)
    return dict(
        seed=seed,
        horizon_sim_s=3600,
        events=events,
        peak_ranges_mps=dict(offshore=[7, 8], harbor=[6, 7], coast=[5, 6], city=[8, 12]),
        clock="simulation_seconds_since_first_post_takeoff_wind_observation",
        shape="rectangular_seed_pulse_filtered_by_Gazebo_WindEffects_tau_1s",
        direction="east_only_no_vertical_component",
        maximum_application_delay_sim_s=2,
    )


def make_profile(world, name, gust_seed=None):
    if gust_seed is not None and name != "harbor-nominal":
        raise ValueError("Frozen gust ranges require harbor-nominal steady wind")
    speeds = PROFILES[name]
    sea = world["sea_extension"]
    points = [p["world_xyz_m"] for p in world["points"]]
    entry = points[0]
    ship = sea["ship_hold_world_xyz_m"]
    distance = math.dist(entry[:2], ship[:2])
    profile = dict(
        schema="missionos.yokohama-wind-profile.v1",
        name=name,
        speeds_mps=dict(speeds),
        origin_xy_m=entry[:2],
        outward_unit_xy=[(ship[i] - entry[i]) / distance for i in (0, 1)],
        coast_boundary_m=sea["coast_to_city_entry_m"],
        offshore_boundary_m=sea["coast_to_city_entry_m"] + sea["offshore_distance_m"] / 2,
        city_bounds_xy_m=[
            [min(p[i] for p in points) - 15, max(p[i] for p in points) + 15] for i in (0, 1)
        ],
        hysteresis_m=5.0,
        direction_enu=[1, 0, 0],
        onset="after_observed_takeoff_hold",
        weather_source="user_supplied_scenario_assumption_not_observation",
        height_model="constant_with_height_not_surface_wind_extrapolation",
        spatial_model="global_wind_seed_follows_one_vehicle_zone_not_a_spatial_flow_field",
        gusts=False,
        building_wakes=False,
    )
    if gust_seed is not None:
        profile.update(
            schema="missionos.yokohama-wind-profile.v2",
            gusts=True,
            gust_schedule=gust_schedule(gust_seed),
        )
    return profile


def zone_at(spec, xyz, previous=None):
    if len(xyz) != 3 or not all(math.isfinite(v) for v in xyz):
        raise ValueError("Wind zone requires a finite observed position")
    if previous is not None and previous not in spec["speeds_mps"]:
        raise ValueError("Unknown previous wind zone")
    h = spec["hysteresis_m"]
    margin = h if previous == "city" else 0
    if all(a - margin <= xyz[i] <= b + margin for i, (a, b) in enumerate(spec["city_bounds_xy_m"])):
        return "city"
    s = sum((xyz[i] - spec["origin_xy_m"][i]) * spec["outward_unit_xy"][i] for i in (0, 1))
    offshore = spec["offshore_boundary_m"] + (-h if previous == "offshore" else h)
    coast = spec["coast_boundary_m"] + (-h if previous in {"offshore", "harbor"} else h)
    return "offshore" if s > offshore else "harbor" if s > coast else "coast"


def requested_wind(spec, pose, sim_s, previous=None):
    if (
        not pose
        or not all(math.isfinite(v) for v in [sim_s, pose["age_s"], pose["sensor_sim_s"]])
        or not 0 <= pose["age_s"] <= 2
        or abs(pose["sensor_sim_s"] - sim_s) > 0.5
    ):
        raise ValueError("Wind zone requires a fresh independent Gazebo pose")
    zone = zone_at(spec, pose["xyz"], previous)
    return zone, [spec["speeds_mps"][zone], 0, 0]


def wind_state(spec, pose, sim_s, previous=None, epoch_sim_s=None):
    zone, velocity = requested_wind(spec, pose, sim_s, previous)
    state = dict(zone=zone, requested_enu_mps=velocity, gust_id=None, steady_speed_mps=velocity[0])
    if spec.get("gusts"):
        if epoch_sim_s is None or not math.isfinite(epoch_sim_s):
            raise ValueError("Gust schedule requires a fixed simulation epoch")
        elapsed = sim_s - epoch_sim_s
        schedule = spec["gust_schedule"]
        if not 0 <= elapsed <= schedule["horizon_sim_s"]:
            raise ValueError("Outside frozen gust schedule")
        state["gust_epoch_sim_s"] = epoch_sim_s
        for event in schedule["events"]:
            if event["start_s"] <= elapsed < event["end_s"]:
                low, high = schedule["peak_ranges_mps"][zone]
                state["requested_enu_mps"] = [low + (high - low) * event["fraction"], 0, 0]
                state["gust_id"] = event["id"]
                break
    return state


def check_gust_deadlines(spec, epoch, previous_check, now):
    """A blocked worker must fail instead of silently skipping a scheduled pulse."""
    if not spec.get("gusts"):
        return
    if not all(math.isfinite(t) for t in (epoch, previous_check, now)) or now < previous_check:
        raise ValueError("Invalid gust application clock")
    schedule = spec["gust_schedule"]
    for event in schedule["events"]:
        for key in ("start_s", "end_s"):
            edge = epoch + event[key]
            if (
                previous_check < edge <= now
                and now - edge > schedule["maximum_application_delay_sim_s"]
            ):
                raise RuntimeError("Scheduled gust transition missed its application deadline")


def verify_profile(config, rows, receipts):
    """Bind zone changes to poses and verify each applied seed via witness motion."""
    spec = config["world"]["wind"]["profile"]
    seed = spec.get("gust_schedule", {}).get("seed")
    try:
        canonical = spec == make_profile(config["world"], spec["name"], seed)
    except (ValueError, KeyError, TypeError):
        canonical = False
    checks = {"canonical_profile": canonical}
    valid = bool(receipts)
    previous, previous_end = None, -1.0
    first_phase = (
        "transport-marker-only"
        if config.get("wind_validation_scope") == "transport-marker-only"
        else "SEA-TAKEOFF"
    )
    for i, r in enumerate(receipts):
        try:
            epoch = receipts[0]["observation_sim_s"]
            state = wind_state(spec, r["vehicle"], r["observation_sim_s"], previous, epoch)
            zone, velocity = state["zone"], state["requested_enu_mps"]
            if spec.get("gusts"):
                valid &= all(r.get(k) == v for k, v in state.items())
            valid &= (
                r["run_id"] == config["run_id"]
                and r["world_sha256"] == config["world"]["world_sha256"]
                and r["sequence"] == i
                and r["previous_zone"] == previous
                and r["zone"] == zone
                and r["requested_enu_mps"] == velocity
                and r["confirmed"] is True
                and (i != 0 or (zone == "offshore" and r["phase"] == first_phase))
                and 0 <= r["start_sim_s"] - r["observation_sim_s"] <= 2
                and 0 <= r["end_sim_s"] - r["start_sim_s"] <= 5
                and r["start_sim_s"] >= previous_end
            )
            nearest = min(
                rows,
                key=lambda p: (
                    abs(p["sim_s"] - r["observation_sim_s"]),
                    math.dist(p["vehicle"]["xyz"], r["vehicle"]["xyz"]),
                ),
            )
            valid &= (
                abs(nearest["sim_s"] - r["observation_sim_s"]) <= 2
                and nearest["vehicle"]["id"] == r["vehicle"]["id"]
                and math.dist(nearest["vehicle"]["xyz"], r["vehicle"]["xyz"]) <= 20
            )
            previous, previous_end = zone, r["end_sim_s"]
        except (KeyError, TypeError, ValueError):
            valid = False
    checks["position_bound_confirmed_transitions"] = bool(valid)
    expected_gusts = []
    if spec.get("gusts"):
        schedule_valid = bool(valid and rows)
        if schedule_valid:
            epoch = receipts[0]["observation_sim_s"]
            last = max(r["sim_s"] for r in rows)
            delay = spec["gust_schedule"]["maximum_application_delay_sim_s"]
            for event in spec["gust_schedule"]["events"]:
                start, end = (epoch + event[k] for k in ("start_s", "end_s"))
                if start + delay > last:
                    continue
                expected_gusts.append(event["id"])
                schedule_valid &= any(
                    r.get("gust_id") == event["id"] and start <= r["end_sim_s"] <= start + delay
                    for r in receipts
                )
                if end + delay <= last:
                    schedule_valid &= any(
                        r.get("gust_id") is None and end <= r["end_sim_s"] <= end + delay
                        for r in receipts
                    )
        checks["scheduled_gusts_observed"] = bool(schedule_valid)

    def response(t):
        return 0 if t <= 0 else t - 2 + (t + 2) * math.exp(-t)

    force_valid = valid
    errors = []
    counts = [0] * len(receipts)
    for row in rows:
        if not valid or row["sim_s"] < receipts[0]["end_sim_s"] + 8:
            continue
        pair = row.get("wind_probe", {})
        a, b = pair.get("wind_witness"), pair.get("wind_control")
        if not a or not b:
            force_valid = False
            continue
        finite = all(
            math.isfinite(v) for p in (a, b) for v in [*p["xyz"], p["age_s"], p["sensor_sim_s"]]
        )
        fresh = finite and all(
            0 <= p["age_s"] <= 2 and abs(p["sensor_sim_s"] - row["sim_s"]) <= 0.5 for p in (a, b)
        )
        if not fresh:
            force_valid = False
            continue
        lo = hi = old = 0.0
        for r in receipts:
            speed = r["requested_enu_mps"][0]
            delta = speed - old
            values = [
                delta * response(a["sensor_sim_s"] - r[k]) for k in ("start_sim_s", "end_sim_s")
            ]
            lo += min(values)
            hi += max(values)
            old = speed
        error = max(0, lo - a["xyz"][0], a["xyz"][0] - hi)
        control_error = math.dist(b["xyz"], config["world"]["wind"]["control_start_xyz_m"])
        errors.append(error)
        force_valid &= error < 0.25 and control_error < 0.001
        active = [
            i
            for i, r in enumerate(receipts)
            if r["end_sim_s"] + (0.5 if spec.get("gusts") else 8) <= row["sim_s"]
            and (
                # A pulse can cross a zone boundary immediately after onset.
                # Verify the joint transient, rather than demand impossible
                # steady-state samples between nearly simultaneous steps.
                (row["sim_s"] <= r["end_sim_s"] + 10)
                if spec.get("gusts")
                else (i == len(receipts) - 1 or row["sim_s"] < receipts[i + 1]["start_sim_s"])
            )
        ]
        for i in active:
            counts[i] += 1
    checks["piecewise_force_observed"] = bool(
        force_valid and errors and all(n >= 3 for n in counts)
    )
    return dict(
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        observed_zones=list(dict.fromkeys(r.get("zone") for r in receipts)),
        all_zones_exercised=set(r.get("zone") for r in receipts) == set(spec["speeds_mps"]),
        force_samples_per_transition=counts,
        maximum_witness_error_m=max(errors) if errors else None,
        expected_gust_ids=expected_gusts,
        observed_gust_ids=list(
            dict.fromkeys(r["gust_id"] for r in receipts if r.get("gust_id") is not None)
        ),
        coverage_note="Passed means observed transitions and forces agree; unvisited zones are untested",
    )
