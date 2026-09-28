"""Authored single-vehicle harbour wind zones, not measured weather or CFD."""

from __future__ import annotations

import math


PROFILES = {
    "harbor-nominal": {"offshore": 6.0, "harbor": 5.0, "coast": 4.0, "city": 3.0},
    "harbor-upper": {"offshore": 8.0, "harbor": 7.0, "coast": 6.0, "city": 4.0},
}


def make_profile(world, name):
    speeds = PROFILES[name]
    sea = world["sea_extension"]
    points = [p["world_xyz_m"] for p in world["points"]]
    entry = points[0]
    ship = sea["ship_hold_world_xyz_m"]
    distance = math.dist(entry[:2], ship[:2])
    return dict(
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


def verify_profile(config, rows, receipts):
    """Bind zone changes to poses and verify each applied seed via witness motion."""
    spec = config["world"]["wind"]["profile"]
    checks = {"canonical_profile": spec == make_profile(config["world"], spec["name"])}
    valid = bool(receipts)
    previous, previous_end = None, -1.0
    first_phase = (
        "transport-marker-only"
        if config.get("wind_validation_scope") == "transport-marker-only"
        else "SEA-TAKEOFF"
    )
    for i, r in enumerate(receipts):
        try:
            zone, velocity = requested_wind(spec, r["vehicle"], r["observation_sim_s"], previous)
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
                and r["start_sim_s"] > previous_end
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
            if r["end_sim_s"] + 8 <= row["sim_s"]
            and (i == len(receipts) - 1 or row["sim_s"] < receipts[i + 1]["start_sim_s"])
        ]
        if active:
            counts[active[-1]] += 1
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
        coverage_note="Passed means observed transitions and forces agree; unvisited zones are untested",
    )
