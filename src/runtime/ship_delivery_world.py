"""A bounded stationary ship / coastal delivery Gazebo world (metres, ENU)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from src.runtime.ship_delivery import ShipDeliveryScenario


def _box(name, xyz, size, color, *, collision=True, contact_topic=None):
    shape = f"<geometry><box><size>{' '.join(map(str, size))}</size></box></geometry>"
    contact = ""
    if contact_topic:
        contact = f"""<sensor name="contact" type="contact"><always_on>true</always_on>
          <update_rate>20</update_rate><topic>{contact_topic}</topic>
          <contact><collision>collision</collision></contact></sensor>"""
    return f'''<model name="{name}"><static>true</static>
      <pose>{" ".join(map(str, xyz))} 0 0 0</pose><link name="link">
      {f'<collision name="collision">{shape}</collision>' if collision else ""}
      <visual name="visual">{shape}<material><ambient>{color}</ambient>
      <diffuse>{color}</diffuse></material></visual>{contact}</link></model>'''


def prepare_ship_world(model_root: Path, scenario: ShipDeliveryScenario) -> dict:
    """Modify private copies of stock x500 models; never mutate the Docker image."""
    from scripts.smoke_missionos_auto_mission_full_runtime_probe import (
        _payload_model_sdf_patch,
        _payload_world_sdf_patch,
        _wind_effects_world_sdf_patch,
    )

    s = scenario
    world_path = model_root / "worlds/default.sdf"
    tree = ET.parse(world_path)
    world = tree.getroot().find("world")
    world.remove(world.find("model[@name='ground_plane']"))
    # Route uses north (Gazebo y); the sea has no supporting collision surface.
    goal = s.offshore_distance_m + s.urban_distance_m
    models = [
        _box("seabed", (0, 0, -22), (4000, 16000, 2), "0.2 0.2 0.2 1"),
        _box(
            "sea_surface",
            (0, s.offshore_distance_m / 2, -1),
            (1000, s.offshore_distance_m + 200, 0.05),
            "0.03 0.2 0.5 1",
            collision=False,
        ),
        _box(
            "stationary_ship",
            (0, 0, -1),
            (24, 36, 2),
            "0.3 0.3 0.35 1",
            contact_topic="/ship/deck_contacts",
        ),
        _box(
            "coastal_land", (0, s.offshore_distance_m + 2000, -2), (1000, 4000, 4), "0.4 0.45 0.3 1"
        ),
        _box(
            "delivery_pad",
            (0, goal, 0.025),
            (16, 16, 0.05),
            "0.8 0.7 0.1 1",
            contact_topic="/ship/payload_contacts",
        ),
    ]
    for row, north in enumerate((s.offshore_distance_m + 45, goal - 35, goal + 45)):
        for side in (-1, 1):
            models.append(
                _box(
                    f"urban_building_{row}_{side + 1}",
                    (side * 45, north, 12),
                    (35, 30, 24),
                    "0.6 0.6 0.65 1",
                )
            )
    for xml in models:
        world.append(ET.fromstring(xml))
    world.append(ET.fromstring(_payload_world_sdf_patch()))
    plugins = ET.fromstring("<root>" + _wind_effects_world_sdf_patch() + "</root>")
    plugins.find("wind/linear_velocity").text = "0 0 0"
    for plugin in plugins.findall("plugin"):
        if plugin.get("name") == "gz::sim::systems::WindEffects":
            plugin.find("force_approximation_scaling_factor").text = "0.05"
    for element in plugins:
        world.append(element)
    world.append(
        ET.fromstring("""<plugin filename="gz-sim-contact-system"
        name="gz::sim::systems::Contact"/>""")
    )
    # The geographic origin is explicitly bound to the same synthetic home.
    world.find("spherical_coordinates/latitude_deg").text = "35.3195"
    world.find("spherical_coordinates/longitude_deg").text = "138.7435"
    tree.write(world_path, encoding="utf-8", xml_declaration=True)
    for filename in ("x500/model.sdf", "x500_base/model.sdf"):
        path = model_root / filename
        model_tree = ET.parse(path)
        model = model_tree.getroot().find("model")
        if filename.startswith("x500/"):
            model.append(ET.fromstring(_payload_model_sdf_patch()))
        else:
            for link in model.findall("link"):
                wind = link.find("enable_wind")
                if wind is None:
                    wind = ET.SubElement(link, "enable_wind")
                wind.text = "true"
        model_tree.write(path, encoding="utf-8", xml_declaration=True)
    return {
        "world_name": "default",
        "vehicle_entity": "x500_0",
        "payload_entity": "delivery_payload",
        "ship_entity": "stationary_ship",
        "frame": "Gazebo ENU metres",
        "ship_position": [0, 0, 0],
        "coast_north_m": s.offshore_distance_m,
        "delivery_position": [0, goal, 0.05],
        "wind_vector_mps": [0, s.wind_mps, 0],
        "wind_force_scaling": 0.05,
        "wind_starts_above_m": 20,
        "payload_mass_kg": 0.05,
        "deck_half_size_m": [12, 18],
        "building_count": 6,
        "moving_ship": False,
        "water_dynamics": False,
    }
