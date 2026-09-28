"""Authored, stationary offshore extension of the frozen city simulation.

The coastal gateway is a scenario boundary beyond the source crop, not a
surveyed shoreline. No water, wind, battery or ship-motion model is qualified.
"""

import json
import math
import xml.etree.ElementTree as ET

from src.runtime.yokohama_scene import sha256, to_source, to_world


def extend_world(root, bundle, world):
    from shapely.geometry import LineString, shape

    entry = world["points"][0]["world_xyz_m"]
    source_entry = world["points"][0]["xyz_m"]
    direction = [math.cos(math.pi / 12), math.sin(math.pi / 12)]
    end = to_world(
        [source_entry[0] + direction[0], source_entry[1] + direction[1], source_entry[2]],
        world["frame"],
    )
    length = math.dist(entry[:2], end[:2])
    direction = [(end[i] - entry[i]) / length for i in (0, 1)]
    coast = [entry[0] + 400 * direction[0], entry[1] + 400 * direction[1], entry[2]]
    ship = [entry[0] + 1400 * direction[0], entry[1] + 1400 * direction[1], entry[2]]
    approach = to_source([entry, ship], world["frame"])
    line = LineString(approach[:, :2])
    footprints = json.loads((bundle / "collision-footprints.geojson").read_text())["features"]
    clearance = min(
        line.distance(shape(f["geometry"]))
        for f in footprints
        if f["properties"]["zmax"] >= source_entry[2] - 1
        and f["properties"]["zmin"] <= source_entry[2] + 1
    )
    if clearance <= 3:
        raise ValueError("Offshore connector intersects the mapped building envelope")
    path = root / "models/worlds/default.sdf"
    tree = ET.parse(path)
    node = tree.getroot().find("world")
    deck = node.find("model[@name='launch_pad']")
    deck.find("pose").text = f"{ship[0]} {ship[1]} -0.5 0 0 0"
    for box in deck.findall("link/*/geometry/box/size"):
        box.text = "12 12 1"
    water = ET.SubElement(node, "model", name="authored_sea")
    ET.SubElement(water, "static").text = "true"
    ET.SubElement(water, "pose").text = f"{ship[0]} {ship[1]} -3 0 0 0"
    link = ET.SubElement(water, "link", name="link")
    visual = ET.SubElement(link, "visual", name="water_visual_only")
    geometry = ET.SubElement(visual, "geometry")
    ET.SubElement(ET.SubElement(geometry, "box"), "size").text = "3500 3500 .02"
    material = ET.SubElement(visual, "material")
    ET.SubElement(material, "diffuse").text = ".10 .30 .40 1"
    tree.write(path, encoding="utf-8", xml_declaration=True)
    world["world_sha256"] = sha256(path)
    world["sea_extension"] = dict(
        schema="missionos.yokohama-sea-extension.v1",
        offshore_distance_m=1000,
        coast_to_city_entry_m=400,
        coast_world_xyz_m=coast,
        ship_hold_world_xyz_m=ship,
        ship_deck_world_xyz_m=[*ship[:2], 0],
        launch_contact_topic="launch_pad",
        source_connector_clearance_m=clearance,
        sea_airspeed_mps=8,
        city_airspeed_mps=3,
        coastal_gateway_role="authored boundary beyond the source crop, not surveyed shoreline",
        stationary_ship=True,
        wind_mps=0,
        payload_release_present=False,
    )
    return world


def flight_stops(world):
    points = world["points"]
    order = [0, 1, 2, 3, 2, 1, 0]
    city = [
        dict(
            name=f"{i:02d}-" + points[p]["id"],
            target_world_xyz_m=points[p]["world_xyz_m"],
            airspeed_mps=3,
        )
        for i, p in enumerate(order)
    ]
    if world.get("payload_delivery"):
        city[4:4] = [
            dict(
                name="PAYLOAD-LOW",
                target_world_xyz_m=world["payload_delivery"]["hover_world_xyz_m"],
                airspeed_mps=3,
            ),
            dict(name="PAYLOAD-CLIMB", target_world_xyz_m=points[3]["world_xyz_m"], airspeed_mps=3),
        ]
    sea = world.get("sea_extension")
    if not sea:
        return city

    def stop(name, target):
        # These authored sea legs are straight and have no intermediate task.
        # Dense fly-through waypoints with a 0.5 m acceptance radius can be
        # missed in wind, leaving Navigator on an already overflown waypoint.
        # Keep the endpoint/hold tolerances; ask AP to brake at the endpoint.
        return dict(name=name, target_world_xyz_m=target, airspeed_mps=8, direct_endpoint=True)

    return [
        stop("SEA-TAKEOFF", sea["ship_hold_world_xyz_m"]),
        stop("SEA-INBOUND-COAST", sea["coast_world_xyz_m"]),
        *city,
        stop("SEA-OUTBOUND-COAST", sea["coast_world_xyz_m"]),
        stop("SEA-RETURN", sea["ship_hold_world_xyz_m"]),
    ]
