"""Generate an isolated, local-only Gazebo navigation benchmark scene."""

from pathlib import Path
import argparse
import json
import xml.etree.ElementTree as ET

import yaml

from obstacle_geometry import SHAPES, add_geometry


def prepare(out: Path, obstacle_shape="box", controller_profile="legacy") -> None:
    out.mkdir(parents=True, exist_ok=True)
    share = Path("/opt/turtlebot3_ws/install/turtlebot3_gazebo/share/turtlebot3_gazebo")
    sdf = ET.Element("sdf", version="1.9")
    world = ET.SubElement(sdf, "world", name="default")
    for lib, name in [
        ("physics", "Physics"),
        ("user-commands", "UserCommands"),
        ("scene-broadcaster", "SceneBroadcaster"),
        ("sensors", "Sensors"),
        ("imu", "Imu"),
        ("contact", "Contact"),
    ]:
        plugin = ET.SubElement(
            world, "plugin", filename=f"gz-sim-{lib}-system", name=f"gz::sim::systems::{name}"
        )
        if name == "Sensors":
            ET.SubElement(plugin, "render_engine").text = "ogre2"
    physics = ET.SubElement(world, "physics", name="default", type="ignored")
    ET.SubElement(physics, "max_step_size").text = "0.002"
    ET.SubElement(physics, "real_time_factor").text = "1.0"
    light = ET.SubElement(world, "light", name="sun", type="directional")
    ET.SubElement(light, "pose").text = "0 0 10 0 0 0"
    ET.SubElement(light, "diffuse").text = "0.9 0.9 0.9 1"
    ET.SubElement(light, "direction").text = "-0.3 0.2 -1"

    def box(name, x, y, z, sx, sy, sz, color):
        model = ET.SubElement(world, "model", name=name)
        ET.SubElement(model, "static").text = "true"
        ET.SubElement(model, "pose").text = f"{x} {y} {z} 0 0 0"
        link = ET.SubElement(model, "link", name="body")
        for kind in ("collision", "visual"):
            element = ET.SubElement(link, kind, name=kind)
            if name == "crossing_cart":
                add_geometry(element, obstacle_shape, sx, sz)
            else:
                geometry = ET.SubElement(element, "geometry")
                ET.SubElement(ET.SubElement(geometry, "box"), "size").text = f"{sx} {sy} {sz}"
            if kind == "visual":
                material = ET.SubElement(element, "material")
                ET.SubElement(material, "ambient").text = color
                ET.SubElement(material, "diffuse").text = color
        return link

    box("ground", 0, 0, -0.05, 8, 6, 0.1, "0.7 0.7 0.7 1")
    for name, x, y, sx, sy in [
        ("north", 0, 2.05, 6.2, 0.1),
        ("south", 0, -2.05, 6.2, 0.1),
        ("east", 3.05, 0, 0.1, 4),
        ("west", -3.05, 0, 0.1, 4),
    ]:
        box(name, x, y, 0.4, sx, sy, 0.8, "0.3 0.4 0.5 1")
    cart = box("crossing_cart", 0, 0, 0.3, 0.4, 0.4, 0.6, "0.9 0.2 0.1 1")
    sensor = ET.SubElement(cart, "sensor", name="contact", type="contact")
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "30"
    ET.SubElement(ET.SubElement(sensor, "contact"), "collision").text = "collision"
    robot = ET.parse(share / "models/turtlebot3_waffle_pi/model.sdf").getroot().find("model")
    robot.set("name", "waffle_pi")
    robot.find("pose").text = "-1 0 0.01 0 0 0"
    camera = robot.find(".//sensor[@type='camera']")
    camera.find("update_rate").text = "8"
    camera.find("camera/image/width").text = "320"
    camera.find("camera/image/height").text = "240"
    world.append(robot)
    ET.indent(sdf)
    ET.ElementTree(sdf).write(out / "arena.sdf", encoding="unicode")
    # Occupancy map contains walls, never the scenario's future moving obstacle.
    w, h = 124, 84
    pixels = bytes(
        0 if x < 2 or x >= w - 2 or y < 2 or y >= h - 2 else 254 for y in range(h) for x in range(w)
    )
    (out / "map.pgm").write_bytes(f"P5\n{w} {h}\n255\n".encode() + pixels)
    (out / "map.yaml").write_text(
        yaml.safe_dump(
            dict(
                image="map.pgm",
                resolution=0.05,
                origin=[-3.1, -2.1, 0.0],
                negate=0,
                occupied_thresh=0.65,
                free_thresh=0.196,
            )
        )
    )
    params = yaml.safe_load(
        Path("/opt/ros/humble/share/turtlebot3_navigation2/param/humble/waffle_pi.yaml").read_text()
    )
    amcl = params["amcl"]["ros__parameters"]
    amcl.update(set_initial_pose=True, initial_pose={"x": -1.0, "y": 0.0, "z": 0.0, "yaw": 0.0})
    controller = params["controller_server"]["ros__parameters"]["FollowPath"]
    controller["max_vel_x"] = 0.20
    controller["max_speed_xy"] = 0.20
    if controller_profile != "legacy":
        raise ValueError("unknown controller profile")
    (out / "nav2.yaml").write_text(yaml.safe_dump(params))
    (out / "protocol.json").write_text(
        json.dumps(
            {
                "schema_version": "tb3_prediction_protocol.v1",
                "phase": "development_baseline",
                "robot": "waffle_pi",
                "obstacle_shape": obstacle_shape,
                "camera": {"width": 320, "height": 240, "fps": 8},
                "start_xy_m": [-1, 0],
                "goal_xy_m": [1, 0],
                "goal_tolerance_m": 0.30,
                "max_speed_mps": 0.20,
                "episode_sim_budget_s": 65,
                "episode_wall_budget_s": 150,
                "observation_history_s": 2,
                "scenarios": {
                    "clearing": {"cart_y0_m": -1.1, "cart_vy_mps": 0.18},
                    "lingering": {"cart_y0_m": 0.0, "cart_vy_mps": 0.015},
                },
                "policies": ["nav2", "constant_velocity"],
                "candidate_actions": {
                    "direct": {"wait_s": 0, "waypoints": [[1, 0]]},
                    "wait": {"wait_s": 4, "waypoints": [[1, 0]]},
                    "detour": {"wait_s": 0, "waypoints": [[-0.55, -0.8], [0.65, -0.8], [1, 0]]},
                },
                "predictor_input": "observed simulator cart poses (privileged); no scenario ID or future truth",
                "prediction_horizon_s": 20,
                "learned_wam_invoked": False,
                "vla_invoked": False,
                "contact_measure": "Gazebo crossing_cart contact sensor; other contacts not covered",
                "adoption_gate": "No adoption from development runs; freeze held-out paired protocol first",
                "physical_execution_invoked": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--obstacle-shape", choices=SHAPES, default="box")
    parser.add_argument(
        "--controller-profile",
        choices=["legacy"],
        default="legacy",
    )
    args = parser.parse_args()
    prepare(args.output, args.obstacle_shape, args.controller_profile)
