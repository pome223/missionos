"""Source-bound Yokohama scene conversion for opt-in, model-free Gazebo tests.

Source projected/vertical coordinates are distinct from Gazebo ENU and PX4 NED.
The local vertical origin is an authored launch pad, not a real surveyed altitude.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ground_height(scene, xy):
    terrain = next(o for o in scene["objects"] if o["kind"] == "terrain")
    vertices = np.asarray(terrain["vertices"]).reshape(-1, 3)
    triangles = vertices[np.asarray(terrain["triangles"]).reshape(-1, 3)]
    for a, b, c in triangles:
        matrix = np.column_stack((b[:2] - a[:2], c[:2] - a[:2]))
        if abs(np.linalg.det(matrix)) < 1e-10:
            continue
        weights = np.linalg.solve(matrix, np.asarray(xy) - a[:2])
        if min(weights) >= -1e-8 and sum(weights) <= 1 + 1e-8:
            return float(a[2] + weights[0] * (b[2] - a[2]) + weights[1] * (c[2] - a[2]))
    raise ValueError("No source terrain below requested point")


def to_world(points, frame):
    p = np.asarray(points, dtype=float)
    if p.shape[-1] != 3 or not np.isfinite(p).all():
        raise ValueError("Invalid source position")
    q = p - np.asarray(frame["source_origin_xyz_m"])
    return q @ np.asarray(frame["source_to_world_matrix"]).T


def to_source(points, frame):
    return (
        np.asarray(points) @ np.linalg.inv(np.asarray(frame["source_to_world_matrix"])).T
        + frame["source_origin_xyz_m"]
    )


def build_frame(scene, route):
    from pyproj import Geod, Transformer

    start = route["waypoints"][0]["xyz_m"]
    source_origin = [*start[:2], ground_height(scene, start[:2]) + 0.12]
    inverse = Transformer.from_crs(6677, 6668, always_xy=True)
    geod = Geod(ellps="GRS80")
    projected = np.asarray(scene["origin_projected_xy"]) + start[:2]
    lon, lat = inverse.transform(*projected)
    columns = []
    for dx, dy in [(10.0, 0.0), (0.0, 10.0)]:
        ll = inverse.transform(projected[0] + dx, projected[1] + dy)
        bearing, _, distance = geod.inv(lon, lat, *ll)
        angle = math.radians(bearing)
        columns.append([math.sin(angle) * distance / 10, math.cos(angle) * distance / 10, 0])
    matrix = np.column_stack([*columns, [0, 0, 1]])
    return {
        "source_origin_xyz_m": source_origin,
        "source_to_world_matrix": matrix.tolist(),
        "home_lon_lat": [lon, lat],
        "world_vertical_origin": "authored launch-pad top; source ground +0.12 m",
        "world_geodetic_altitude_m": 0,
        "geodetic_altitude_role": "synthetic PX4/Gazebo origin, not source orthometric or real ellipsoid height",
        "horizontal_conversion": "local Jacobian of EPSG:6677 inverse projection and GRS80 geodesic at D1; metres, true east/north",
        "px4_local_ned": "[world north - observed EKF origin north, world east - observed EKF origin east, -(world up - observed EKF origin up)]",
    }


def write_obj(path, objects, frame):
    offset = 1
    with Path(path).open("w") as out:
        out.write(
            "# Yokohama City / PLATEAU CC BY 4.0; MissionOS local simulation transform. ENU metres.\n"
        )
        for o in objects:
            v = to_world(np.asarray(o["vertices"]).reshape(-1, 3), frame)
            faces = np.asarray(o["triangles"]).reshape(-1, 3)
            out.write("o " + o["id"] + "\n")
            for p in v:
                out.write("v " + " ".join(f"{x:.6f}" for x in p) + "\n")
            for f in faces:
                out.write("f " + " ".join(str(int(x) + offset) for x in f) + "\n")
            offset += len(v)


def transform_obj(source, target, frame):
    with Path(target).open("w") as out:
        for line in Path(source).read_text().splitlines():
            if line.startswith("v "):
                p = to_world([float(x) for x in line.split()[1:]], frame)
                out.write("v " + " ".join(f"{x:.6f}" for x in p) + "\n")
            else:
                out.write(line + "\n")


def add_face_normals(path):
    """DART requires normals; retain winding, omit only zero-area faces."""
    lines = Path(path).read_text().splitlines()
    vertices = np.array([[float(x) for x in s.split()[1:]] for s in lines if s.startswith("v ")])
    output = []
    index = 0
    omitted = 0
    for line in lines:
        if line.startswith("f "):
            face = [int(x.split("/")[0]) for x in line.split()[1:]]
            if len(face) != 3:
                raise ValueError("Only triangular OBJ meshes are supported")
            a, b, c = vertices[np.array(face) - 1]
            normal = np.cross(b - a, c - a)
            length = np.linalg.norm(normal)
            if length < 1e-12:
                omitted += 1
                continue
            index += 1
            output.append("vn " + " ".join(f"{x:.9f}" for x in normal / length))
            line = "f " + " ".join(f"{v}//{index}" for v in face)
        output.append(line)
    Path(path).write_text("\n".join(output) + "\n")
    return {"triangles_with_normals": index, "zero_area_faces_omitted": omitted}


def mesh(kind, filename, color=None):
    element = ET.Element(kind, name=filename.removesuffix(".obj"))
    geometry = ET.SubElement(element, "geometry")
    m = ET.SubElement(geometry, "mesh")
    ET.SubElement(m, "uri").text = "file:///mission/assets/" + filename
    if color:
        material = ET.SubElement(element, "material")
        for key in ["ambient", "diffuse"]:
            ET.SubElement(material, key).text = color
    return element


def contact_sensor(link, name, collisions):
    sensor = ET.SubElement(link, "sensor", name=name, type="contact")
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "20"
    ET.SubElement(sensor, "topic").text = "/yokohama/contacts/" + name
    contact = ET.SubElement(sensor, "contact")
    for collision in collisions:
        ET.SubElement(contact, "collision").text = collision


def camera(link, name, xyz, rpy, topic, *, rgbd=False):
    sensor = ET.SubElement(link, "sensor", name=name, type="rgbd_camera" if rgbd else "camera")
    ET.SubElement(sensor, "pose").text = " ".join(map(str, [*xyz, *rpy]))
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "2"
    ET.SubElement(sensor, "topic").text = topic
    c = ET.SubElement(sensor, "camera")
    ET.SubElement(c, "horizontal_fov").text = str(math.pi / 3)
    image = ET.SubElement(c, "image")
    for key, value in [("width", "640"), ("height", "360"), ("format", "R8G8B8")]:
        ET.SubElement(image, key).text = value
    clip = ET.SubElement(c, "clip")
    ET.SubElement(clip, "near").text = ".1"
    ET.SubElement(clip, "far").text = "800"
    if rgbd:
        clip = ET.SubElement(ET.SubElement(c, "depth_camera"), "clip")
        ET.SubElement(clip, "near").text = ".1"
        ET.SubElement(clip, "far").text = "800"


def pad(world, name, xyz):
    model = ET.SubElement(world, "model", name=name)
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = " ".join(map(str, [xyz[0], xyz[1], xyz[2] - 0.1, 0, 0, 0]))
    link = ET.SubElement(model, "link", name="link")
    for kind in ["visual", "collision"]:
        e = ET.SubElement(link, kind, name=kind)
        g = ET.SubElement(e, "geometry")
        ET.SubElement(ET.SubElement(g, "box"), "size").text = "4 4 .2"
        if kind == "visual":
            m = ET.SubElement(e, "material")
            ET.SubElement(m, "diffuse").text = ".12 .48 .36 1"
    contact_sensor(link, name, ["collision"])


def sphere(world, name, xyz, gravity):
    model = ET.SubElement(world, "model", name=name)
    ET.SubElement(model, "pose").text = " ".join(map(str, [*xyz, 0, 0, 0]))
    link = ET.SubElement(model, "link", name="link")
    ET.SubElement(link, "gravity").text = str(gravity).lower()
    inertial = ET.SubElement(link, "inertial")
    ET.SubElement(inertial, "mass").text = "1"
    inertia = ET.SubElement(inertial, "inertia")
    for key, value in [
        ("ixx", ".025"),
        ("iyy", ".025"),
        ("izz", ".025"),
        ("ixy", "0"),
        ("ixz", "0"),
        ("iyz", "0"),
    ]:
        ET.SubElement(inertia, key).text = value
    for kind in ["collision", "visual"]:
        e = ET.SubElement(link, kind, name=kind)
        ET.SubElement(ET.SubElement(ET.SubElement(e, "geometry"), "sphere"), "radius").text = ".25"
    contact_sensor(link, name, ["collision"])


def build_world(root, bundle, phase):
    root, bundle = Path(root), Path(bundle)
    expected = json.loads((bundle / "files.sha256.json").read_text())
    for name in [
        "scene.json",
        "route.json",
        "collision-prisms.obj",
        "collision-footprints.geojson",
    ]:
        if sha256(bundle / name) != expected[name]:
            raise ValueError("Source asset hash mismatch: " + name)
    scene = json.loads((bundle / "scene.json").read_text())
    route = json.loads((bundle / "route.json").read_text())
    frame = build_frame(scene, route)
    assets = root / "assets"
    assets.mkdir()
    for kind in ["building", "bridge", "terrain", "road"]:
        write_obj(
            assets / (kind + ".obj"), [o for o in scene["objects"] if o["kind"] == kind], frame
        )
    transform_obj(bundle / "collision-prisms.obj", assets / "collision-prisms.obj", frame)
    mesh_conversion = {path.name: add_face_normals(path) for path in assets.glob("*.obj")}
    tree = ET.parse(root / "models/worlds/default.sdf")
    world = tree.getroot().find("world")
    world.remove(world.find("model[@name='ground_plane']"))
    for plugin in world.findall("plugin"):
        world.remove(plugin)
    for slug, classname in [
        ("physics", "Physics"),
        ("user-commands", "UserCommands"),
        ("scene-broadcaster", "SceneBroadcaster"),
        ("contact", "Contact"),
        ("imu", "Imu"),
        ("magnetometer", "Magnetometer"),
        ("air-pressure", "AirPressure"),
        ("navsat", "NavSat"),
        ("sensors", "Sensors"),
    ]:
        p = ET.SubElement(
            world,
            "plugin",
            filename="gz-sim-" + slug + "-system",
            name="gz::sim::systems::" + classname,
        )
        if classname == "Sensors":
            ET.SubElement(p, "render_engine").text = "ogre2"
    world.find("spherical_coordinates/latitude_deg").text = str(frame["home_lon_lat"][1])
    world.find("spherical_coordinates/longitude_deg").text = str(frame["home_lon_lat"][0])
    world.find("spherical_coordinates/elevation").text = "0"
    city = ET.SubElement(world, "model", name="yokohama_city")
    ET.SubElement(city, "static").text = "true"
    link = ET.SubElement(city, "link", name="link")
    for kind, color in [
        ("building", ".58 .65 .63 1"),
        ("bridge", ".48 .55 .52 1"),
        ("terrain", ".50 .59 .46 1"),
        ("road", ".77 .78 .74 1"),
    ]:
        link.append(mesh("visual", kind + ".obj", color))
    for name in ["terrain.obj", "collision-prisms.obj"]:
        link.append(mesh("collision", name))
    contact_sensor(link, "city", ["terrain", "collision-prisms"])
    pad(world, "launch_pad", [0, 0, 0])
    pad(world, "delivery_pad", to_world(route["delivery_pad"]["center_xyz_m"], frame))
    points = [dict(p, world_xyz_m=to_world(p["xyz_m"], frame).tolist()) for p in route["waypoints"]]
    static_cam = ET.SubElement(world, "model", name="scene_camera")
    ET.SubElement(static_cam, "static").text = "true"
    camlink = ET.SubElement(static_cam, "link", name="link")
    p = np.asarray(points[1]["world_xyz_m"])
    direction = np.asarray(points[2]["world_xyz_m"]) - p
    camera(
        camlink,
        "rgbd",
        p,
        [0, 0, math.atan2(direction[1], direction[0])],
        "/yokohama/scene",
        rgbd=True,
    )
    probes = {}
    if phase == "contacts":
        from shapely.geometry import Point, shape

        footprints = json.loads((bundle / "collision-footprints.geojson").read_text())["features"]
        target = next(
            f
            for f in footprints
            if f["properties"]["id"] == "bldg_19332410-cd96-430a-aed7-02dd727fc597"
        )
        from shapely.ops import nearest_points

        boundary = nearest_points(
            Point(route["waypoints"][1]["xyz_m"][:2]), shape(target["geometry"])
        )[1]
        q = np.array(boundary.coords[0])
        v = np.array(route["waypoints"][1]["xyz_m"][:2]) - q
        v /= np.linalg.norm(v)
        source_wall = [*(q + v * 0.15), 18.0]
        ground_xy = np.array(route["waypoints"][0]["xyz_m"][:2]) + [-6, 0]
        ground_z = ground_height(scene, ground_xy)
        probes = {
            "wall_probe": {
                "initial_world_xyz_m": to_world(source_wall, frame).tolist(),
                "gravity": False,
                "expected_collision": "collision-prisms",
                "source_building": target["properties"]["id"],
            },
            "ground_probe": {
                "initial_world_xyz_m": to_world([*ground_xy, ground_z + 3], frame).tolist(),
                "gravity": True,
                "expected_collision": "terrain",
                "expected_rest_z_m": float(to_world([*ground_xy, ground_z + 0.25], frame)[2]),
            },
            "free_probe": {
                "initial_world_xyz_m": points[1]["world_xyz_m"],
                "gravity": False,
                "expected_collision": None,
            },
        }
        for name, settings in probes.items():
            sphere(world, name, settings["initial_world_xyz_m"], settings["gravity"])
    world_path = root / "models/worlds/default.sdf"
    tree.write(world_path, encoding="utf-8", xml_declaration=True)
    # Modify disposable copies only. No colored marker or native model process.
    xbase = root / "models/x500_base/model.sdf"
    bt = ET.parse(xbase)
    body = bt.getroot().find("model/link[@name='base_link']")
    camera(body, "urban_rgbd", [0.25, 0, 0.1], [0, 0, 0], "/yokohama/onboard", rgbd=True)
    camera(body, "urban_down", [0.25, 0, 0.1], [0, math.pi / 2, 0], "/yokohama/down")
    bt.write(xbase, encoding="utf-8", xml_declaration=True)
    xt = ET.parse(root / "models/x500/model.sdf")
    xm = xt.getroot().find("model")
    pp = ET.SubElement(
        xm,
        "plugin",
        filename="gz-sim-pose-publisher-system",
        name="gz::sim::systems::PosePublisher",
    )
    for key, value in [
        ("publish_model_pose", "true"),
        ("publish_link_pose", "false"),
        ("use_pose_vector_msg", "true"),
        ("update_frequency", "25"),
    ]:
        ET.SubElement(pp, key).text = value
    xt.write(root / "models/x500/model.sdf", encoding="utf-8", xml_declaration=True)
    return {
        "schema": "missionos.yokohama-world.v1",
        "phase": phase,
        "mesh_conversion": mesh_conversion,
        "frame": frame,
        "points": points,
        "probes": probes,
        "source_sha256": {
            n: expected[n]
            for n in [
                "scene.json",
                "route.json",
                "collision-prisms.obj",
                "collision-footprints.geojson",
            ]
        },
        "world_sha256": sha256(world_path),
        "model_sha256": {
            str(p.relative_to(root)): sha256(p) for p in sorted((root / "models").glob("*/model.*"))
        },
        "asset_sha256": {p.name: sha256(p) for p in sorted(assets.glob("*.obj"))},
        "source_buildings": 153,
        "source_bridges": 4,
        "source_geometry_triangles": 35916,
        "vla_invoked": False,
        "wam_invoked": False,
        "gpu_requested": False,
        "physical_execution": False,
    }
