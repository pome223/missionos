"""Opt-in uniform Gazebo WindEffects; a synthetic stress model, not CFD."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from src.runtime.yokohama_scene import sha256, sphere


def add_wind(root, world, east_mps, *, after_takeoff=False):
    if not math.isfinite(east_mps) or not 0 < east_mps <= 5:
        raise ValueError("Wind must be finite, eastward and in (0, 5] m/s")
    path = root / "models/worlds/default.sdf"
    tree = ET.parse(path)
    node = tree.getroot().find("world")
    if node.find("wind") is not None:
        node.remove(node.find("wind"))
    ET.SubElement(
        ET.SubElement(node, "wind"), "linear_velocity"
    ).text = f"{0 if after_takeoff else east_mps} 0 0"
    plugin = ET.SubElement(
        node, "plugin", filename="gz-sim-wind-effects-system", name="gz::sim::systems::WindEffects"
    )
    ET.SubElement(plugin, "force_approximation_scaling_factor").text = "1"
    # An isolated, unpowered witness distinguishes a configured wind vector from
    # a physics plugin that actually loads and applies force. The control has
    # identical mass/geometry but wind disabled. Both remain outside the route.
    for name, enabled, y in [("wind_witness", True, 3000), ("wind_control", False, 3010)]:
        sphere(node, name, [0, y, 100], False)
        link = node.find(f"model[@name='{name}']/link")
        ET.SubElement(link, "enable_wind").text = str(enabled).lower()
    cargo = node.find("model[@name='delivery_payload']/link")
    if cargo is not None:
        ET.SubElement(cargo, "enable_wind").text = "true"
    tree.write(path, encoding="utf-8", xml_declaration=True)
    model_path = root / "models/x500_base/model.sdf"
    model = ET.parse(model_path)
    base = model.getroot().find("model/link[@name='base_link']")
    if base is None:
        raise ValueError("Expected x500 base_link missing")
    if base.find("enable_wind") is None:
        ET.SubElement(base, "enable_wind").text = "true"
    else:
        base.find("enable_wind").text = "true"
    model.write(model_path, encoding="utf-8", xml_declaration=True)
    world["wind"] = dict(
        schema="missionos.yokohama-wind.v1",
        after_takeoff=after_takeoff,
        velocity_enu_mps=[east_mps, 0, 0],
        force_scale_per_s=1,
        force_law="mass * 1/s * (wind_velocity - link_velocity)",
        time_constant_s=1,
        gusts=False,
        building_wakes=False,
        calibrated_aerodynamics=False,
        affected_links=["x500_0/base_link"]
        + (["delivery_payload/" + cargo.attrib["name"]] if cargo is not None else []),
        witness_start_xyz_m=[0, 3000, 100],
        control_start_xyz_m=[0, 3010, 100],
    )
    world["world_sha256"] = sha256(path)
    world["model_sha256"] = {
        str(p.relative_to(root)): sha256(p) for p in sorted((root / "models").glob("*/model.*"))
    }
    if world.get("sea_extension"):
        world["sea_extension"]["wind_mps"] = east_mps
    return world


def verify_wind(root, config, rows):
    """Recheck SDF flags and independently observed motion of an unpowered pair."""
    spec = config["world"]["wind"]
    world = ET.parse(root / "models/worlds/default.sdf").getroot().find("world")
    base = (
        ET.parse(root / "models/x500_base/model.sdf")
        .getroot()
        .find("model/link[@name='base_link']")
    )
    plugin = world.find("plugin[@name='gz::sim::systems::WindEffects']")
    checks = {
        "plugin_and_vector": plugin is not None
        and float(plugin.findtext("force_approximation_scaling_factor"))
        == spec["force_scale_per_s"]
        == 1
        and [float(x) for x in world.findtext("wind/linear_velocity").split()]
        == ([0, 0, 0] if spec.get("after_takeoff") else spec["velocity_enu_mps"]),
        "vehicle_wind_enabled": base.findtext("enable_wind") == "true",
    }
    if config["world"].get("payload_delivery"):
        checks["cargo_wind_enabled"] = (
            world.findtext("model[@name='delivery_payload']/link/enable_wind") == "true"
        )
    import json

    start, end = 0.0, 0.0
    if spec.get("after_takeoff"):
        path = root / "wind-activation.json"
        activation = json.loads(path.read_text()) if path.exists() else {}
        checks["airborne_wind_activation"] = (
            activation.get("confirmed") is True
            and activation.get("phase") == "SEA-TAKEOFF"
            and activation.get("requested_enu_mps") == spec["velocity_enu_mps"]
            and activation.get("run_id") == config.get("run_id")
            and 0 <= activation.get("end_sim_s", -1) - activation.get("start_sim_s", 0) <= 5
        )
        start = activation.get("start_sim_s", 0)
        end = activation.get("end_sim_s", 0)
    usable = [r for r in rows if end + 8 <= r["sim_s"] <= end + 18]
    samples = []
    for r in usable:
        pair = r.get("wind_probe", {})
        a, b = pair.get("wind_witness"), pair.get("wind_control")
        if not a or not b:
            continue
        if not all(
            math.isfinite(v) for p in (a, b) for v in [*p["xyz"], p["age_s"], p["sensor_sim_s"]]
        ):
            checks["finite_wind_observations"] = False
            continue
        # For a first-order wind rise and the stock unit force scale, x(t)
        # approaches v*(t-2+(t+2)*exp(-t)). Euler physics leaves small error.
        t = a["sensor_sim_s"] - start
        expected_x = spec["velocity_enu_mps"][0] * (t - 2 + (t + 2) * math.exp(-t))
        samples.append(
            dict(
                sim_s=a["sensor_sim_s"],
                elapsed_since_activation_start_s=t,
                witness_x_m=a["xyz"][0],
                expected_x_m=expected_x,
                error_m=max(
                    0, abs(a["xyz"][0] - expected_x) - spec["velocity_enu_mps"][0] * (end - start)
                ),
                control_displacement_m=math.dist(b["xyz"], spec["control_start_xyz_m"]),
                fresh=all(
                    0 <= p["age_s"] <= 2 and abs(p["sensor_sim_s"] - r["sim_s"]) <= 0.5
                    for p in (a, b)
                ),
            )
        )
    checks["force_observed_against_unpowered_control"] = bool(len(samples) >= 3) and all(
        s["fresh"] and s["error_m"] < 0.15 and s["control_displacement_m"] < 0.001 for s in samples
    )
    return dict(
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        samples=samples,
        limitation="Uniform built-in force approximation; no calibrated wind envelope",
    )
