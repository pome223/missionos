#!/usr/bin/env python3
"""Opt-in, network-isolated PX4 urban routes with explicit geometric or ANWM selection.

ANWM ranks route-prefix images; independent scene geometry constrains execution.
This experiment does not claim learned collision risk or autonomous replanning.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.px4_aerial_flight_session import (  # noqa: E402
    FlightSession,
    STREAM_RATES,
    assert_container,
    atomic_json,
    sign_command,
    wrap_angle,
)
from scripts.urban_navigation_contract import (  # noqa: E402
    ASSET_FILES,
    ASSET_REVISION,
    digest,
    route_check,
    scene_spec,
    segment_box_clearance,
)

NAME = "missionos-urban-wam-e2e"
LABEL = "urban-wam-px4-sitl"
OPT_IN = "RUN_PX4_URBAN_WAM_TRIAL"


def call(args, *, timeout=30, **kwargs):
    return subprocess.run(
        list(map(str, args)),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )


def require_opt_in():
    if os.environ.get(OPT_IN) != "1":
        raise ValueError("set " + OPT_IN + "=1 for isolated simulation")


def validate_trigger(envelope, config, key, now):
    if set(envelope) != {"command", "hmac_sha256"}:
        raise ValueError("invalid urban trigger envelope")
    command = envelope["command"]
    expected = sign_command(command, key)["hmac_sha256"]
    if not isinstance(envelope["hmac_sha256"], str) or not hmac.compare_digest(
        expected, envelope["hmac_sha256"]
    ):
        raise ValueError("urban trigger signature rejected")
    scope_keys = ["session_id", "scene_sha256", "approved_instruction_ref"]
    if config.get("selection_mode", "geometric") == "geometric":
        scope_keys.append("route_sha256")
    else:
        scene = scene_spec(config["family"])
        route = scene["routes"].get(command.get("route_id"))
        if (
            route is None
            or command.get("route_sha256") != digest(route)
            or not route_check(scene, route)["admissible"]
        ):
            raise ValueError("urban model route outside declared admissible scope")
        if (
            not isinstance(command.get("selection_sha256"), str)
            or len(command["selection_sha256"]) != 64
        ):
            raise ValueError("urban model selection binding missing")
    if any(command.get(k) != config[k] for k in scope_keys):
        raise ValueError("urban trigger scope mismatch")
    issued, expiry = command.get("issued_at_unix_s"), command.get("expires_at_unix_s")
    if (
        any(
            type(v) not in (int, float) or not math.isfinite(v)
            for v in (issued, expiry)
        )
        or not issued <= now < expiry <= issued + 5
    ):
        raise ValueError("urban trigger expired or invalid")
    return command


def verify_assets(root):
    for name, expected in ASSET_FILES.items():
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("missing or changed pinned Gazebo asset: " + name)


def fetch_assets(root):
    from urllib.request import urlopen

    root.mkdir(parents=True, exist_ok=True)
    for name, expected in ASSET_FILES.items():
        path = root / name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("refusing to overwrite a changed asset: " + name)
            continue
        url = f"https://raw.githubusercontent.com/osrf/gazebo_models/{ASSET_REVISION}/{name}"
        with urlopen(url, timeout=30) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("downloaded asset digest differs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    verify_assets(root)
    atomic_json(
        root / "urban-asset-provenance.json",
        {
            "repository": "https://github.com/osrf/gazebo_models",
            "revision": ASSET_REVISION,
            "license": "CC-BY-3.0",
            "copyright": "2012 Nathan Koenig",
            "model_author": "Cole Biesemeyer",
            "files": ASSET_FILES,
            "changes": "none; runtime SDF supplies instance poses and uniform scales",
        },
    )


def contact_sensor(link, name, collision):
    sensor = ET.SubElement(link, "sensor", name=name, type="contact")
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "50"
    ET.SubElement(ET.SubElement(sensor, "contact"), "collision").text = collision


def building_sdf(building):
    root = ET.Element("sdf", version="1.9")
    model = ET.SubElement(root, "model", name=building["name"])
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = " ".join(
        map(str, building["translation_enu_m"] + [0, 0, 0])
    )
    link = ET.SubElement(model, "link", name="link")
    for kind in ("collision", "visual"):
        element = ET.SubElement(link, kind, name="building_" + kind)
        mesh = ET.SubElement(ET.SubElement(element, "geometry"), "mesh")
        ET.SubElement(mesh, "uri").text = "/assets/apartment/meshes/apartment.dae"
        ET.SubElement(mesh, "scale").text = " ".join([str(building["scale"])] * 3)
    contact_sensor(
        link,
        "building_contact",
        "building_collision",
    )
    return ET.tostring(root, encoding="unicode")


def setup(
    root, assets, family, route_id, instruction_ref, image, selection_mode="geometric"
):
    require_opt_in()
    if selection_mode not in ("geometric", "anwm", "headroom"):
        raise ValueError("explicit urban selector required")
    if (selection_mode == "headroom") != family.startswith("headroom_"):
        raise ValueError("headroom cohort requires its separate selector contract")
    if root.exists():
        raise ValueError("refusing to overwrite an existing urban trial")
    verify_assets(assets)
    scene = scene_spec(family)
    route_id = route_id or scene["feasibility_route"]
    if route_id not in scene["routes"]:
        raise ValueError("unknown route")
    check = route_check(scene, scene["routes"][route_id])
    if not check["admissible"]:
        raise ValueError(
            "declared route rejected before simulator startup: " + json.dumps(check)
        )
    if not instruction_ref.strip():
        raise ValueError("retained user instruction reference required")
    session = root / "session"
    session.mkdir(parents=True)
    atomic_json(session / "scene.json", scene)
    atomic_json(session / "planned-route-check.json", check)
    metadata = json.loads(call(["docker", "image", "inspect", image]).stdout)[0]
    camera_path = "/opt/px4-gazebo/share/gz/models/OakD-Lite/model.sdf"
    world_path = "/opt/px4-gazebo/share/gz/worlds/default.sdf"

    def source(path):
        return call(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/bin/cat",
                metadata["Id"],
                path,
            ]
        ).stdout

    camera = ET.fromstring(source(camera_path))
    for sensor in camera.findall("model/link/sensor"):
        sensor.find("update_rate").text = "4"
        if sensor.get("name") == "IMX214":
            sensor.find("camera/image/width").text = "640"
            sensor.find("camera/image/height").text = "360"
    (session / "camera.sdf").write_text(ET.tostring(camera, encoding="unicode"))
    goal = scene["goal_enu_m"]
    # Separate declared destination view, captured before flight. The camera
    # offset follows the pinned x500_depth/OakD transform, facing ENU east.
    goal_sdf = f"""<sdf version="1.9"><model name="aerial_goal_reference"><static>true</static>
<pose>{goal[0] + .13233} {goal[1]} {goal[2] + .26078} 0 0 0</pose>
<link name="goal_camera_link"><sensor name="goal_rgb" type="camera">
<gz_frame_id>goal_camera_link</gz_frame_id><camera><horizontal_fov>1.204</horizontal_fov>
<image><width>640</width><height>360</height></image><clip><near>0.1</near><far>100</far></clip>
</camera><always_on>1</always_on><update_rate>4</update_rate></sensor></link></model></sdf>"""
    (session / "goal-camera.sdf").write_text(goal_sdf)
    world = ET.fromstring(source(world_path))
    contact_sensor(
        world.find("world/model/link"),
        "ground_contact",
        "collision",
    )
    server_config = source("/opt/px4-gazebo/share/gz/server.config")
    if "gz-sim-contact-system" not in server_config:
        raise ValueError("stock server lacks the contact system")
    (session / "server-source.config").write_text(server_config)
    (session / "world.sdf").write_text(ET.tostring(world, encoding="unicode"))
    for model in ("x500_base", "x500", "x500_depth", "OakD-Lite"):
        (session / (model + "-source.sdf")).write_text(
            source(f"/opt/px4-gazebo/share/gz/models/{model}/model.sdf")
        )
    config = {
        "schema_version": "missionos_px4_urban_feasibility_policy.v1",
        "session_id": uuid.uuid4().hex,
        "scene_sha256": scene["scene_sha256"],
        "approved_instruction_ref": instruction_ref,
        "execution_scope": "px4_sitl",
        "hardware_target": False,
        "route_id": route_id,
        "route_sha256": digest(scene["routes"][route_id]),
        "selection_mode": selection_mode,
        "family": family,
        "scene_entities": [
            {
                "name": b["name"],
                "position": dict(zip("xyz", b["translation_enu_m"])),
                "orientation": {"x": 0, "y": 0, "z": 0, "w": 1},
            }
            for b in scene["buildings"]
        ],
        "asset_revision": ASSET_REVISION,
        "asset_sha256": ASSET_FILES,
    }
    atomic_json(session / "config.json", config)
    keypath = session / ".dispatch-key"
    with keypath.open("xb") as stream:
        os.chmod(keypath, 0o600)
        stream.write(secrets.token_bytes(32))
    scripts = session / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").write_text("")
    for name in (
        "px4_urban_wam_trial.py",
        "px4_aerial_flight_session.py",
        "urban_navigation_contract.py",
        "urban_route_observer.py",
        "urban_goal_capture.py",
        "urban_headroom_contract.py",
        "px4_urban_headroom_trial.py",
        "urban_headroom_contact_probe.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    identity = call(
        [
            "docker",
            "run",
            "-d",
            "--name",
            NAME,
            "--network",
            "none",
            "--label",
            "missionos.scope=" + LABEL,
            "--mount",
            f"type=bind,src={session},dst=/session",
            "--mount",
            f"type=bind,src={assets},dst=/assets,readonly",
            "--mount",
            f"type=bind,src={session / 'camera.sdf'},dst={camera_path},readonly",
            "--mount",
            f"type=bind,src={session / 'world.sdf'},dst={world_path},readonly",
            "-e",
            "PX4_SIM_MODEL=gz_x500_depth",
            "-e",
            "PX4_GZ_WORLD=default",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            "-e",
            "LIBGL_ALWAYS_SOFTWARE=1",
            "-e",
            "PX4_SIM_SPEED_FACTOR=0.2",
            metadata["Id"],
            "-d",
        ]
    ).stdout.strip()
    atomic_json(
        root / "container.json",
        {"id": identity, "name": NAME, "image_id": metadata["Id"]},
    )
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        if (
            "Startup script returned successfully"
            in call(["docker", "logs", identity]).stdout
        ):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("PX4 startup did not complete")
    response = call(
        [
            "docker",
            "exec",
            identity,
            "gz",
            "service",
            "-s",
            "/world/default/set_physics",
            "--reqtype",
            "gz.msgs.Physics",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "5000",
            "--req",
            "gravity: {x: 0, y: 0, z: -9.8} real_time_factor: 0.2 max_step_size: 0.004",
        ]
    )
    if "data: true" not in response.stdout:
        raise RuntimeError("gravity/physics restore rejected")
    atomic_json(
        session / "physics-configuration.json",
        {
            "gravity_m_s2": [0, 0, -9.8],
            "real_time_factor": 0.2,
            "max_step_size_seconds": 0.004,
            "request_accepted": True,
        },
    )
    for building in scene["buildings"]:
        sdf = building_sdf(building)
        (session / (building["name"] + ".sdf")).write_text(sdf)
        response = call(
            [
                "docker",
                "exec",
                identity,
                "gz",
                "service",
                "-s",
                "/world/default/create",
                "--reqtype",
                "gz.msgs.EntityFactory",
                "--reptype",
                "gz.msgs.Boolean",
                "--timeout",
                "10000",
                "--req",
                "sdf: " + json.dumps(sdf),
            ]
        )
        if "data: true" not in response.stdout:
            raise RuntimeError("building spawn rejected")
    shutil.copyfile(session / "x500_depth-source.sdf", session / "airframe.sdf")
    response = call(
        [
            "docker",
            "exec",
            identity,
            "gz",
            "service",
            "-s",
            "/world/default/create",
            "--reqtype",
            "gz.msgs.EntityFactory",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "10000",
            "--req",
            "sdf: " + json.dumps(goal_sdf),
        ]
    )
    if "data: true" not in response.stdout:
        raise RuntimeError("declared goal camera spawn rejected")
    call(
        [
            "docker",
            "exec",
            identity,
            "python3",
            "/session/scripts/urban_goal_capture.py",
        ],
        timeout=60,
    )
    # The separately captured reference is not a vehicle sensor. Stop its
    # rendering before collecting the live vehicle's synchronized RGB-D history.
    response = call(
        [
            "docker",
            "exec",
            identity,
            "gz",
            "service",
            "-s",
            "/world/default/remove",
            "--reqtype",
            "gz.msgs.Entity",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "5000",
            "--req",
            'name: "aerial_goal_reference" type: MODEL',
        ]
    )
    if "data: true" not in response.stdout:
        raise RuntimeError("captured reference camera removal rejected")
    atomic_json(
        session / "source-files.json",
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in session.glob("*.sdf")
        },
    )
    return session


class UrbanFlightSession(FlightSession):
    def __init__(self, directory):
        super().__init__(directory)
        self.scene = json.loads((self.directory / "scene.json").read_text())
        if (
            self.scene != scene_spec(self.scene["family"])
            or self.scene["scene_sha256"] != self.config["scene_sha256"]
        ):
            raise ValueError("urban scene contract differs")
        self.route = self.scene["routes"][self.config["route_id"]]
        if (
            digest(self.route) != self.config["route_sha256"]
            or not route_check(self.scene, self.route)["admissible"]
        ):
            raise ValueError("urban route binding or clearance rejected")
        self.previous_point = None
        self.minimum_clearance = math.inf
        self.contact_messages = {b["name"]: 0 for b in self.scene["buildings"]}
        self.contact_messages["ground"] = 0
        self.building_contacts = 0
        self.ground_contacts = 0
        from gz.msgs10.contacts_pb2 import Contacts

        for name in self.contact_messages:
            model = "ground_plane" if name == "ground" else name
            sensor = "ground_contact" if name == "ground" else "building_contact"
            topic = f"/world/default/model/{model}/link/link/sensor/{sensor}/contact"
            if not self.node.subscribe(
                Contacts,
                topic,
                lambda message, key=name: self.on_contact(key, message),
            ):
                raise RuntimeError("contact subscription rejected")
        self.outcome.update(
            experimental_measurement_only=True,
            model_forecast_used_for_dispatch=False,
            jev_judgment_invoked=False,
            selector=self.scene["selector"],
            family=self.scene["family"],
            route_id=self.config["route_id"],
            completed_waypoints=0,
            goal_reached=False,
            hover_position_tolerance_m=0.1,
            hover_speed_limit_mps=0.1,
        )

    def on_contact(self, name, message):
        self.contact_messages[name] += 1
        if len(message.contact):
            if name == "ground":
                self.ground_contacts += 1
            else:
                self.building_contacts += 1

    def stationary_at(self, point, timeout, **kwargs):
        if kwargs.get("position_tolerance") == 0.05:
            kwargs.update(position_tolerance=0.1, speed_limit=0.1)
        return super().stationary_at(point, timeout, **kwargs)

    def check_bounds(self, current):
        point = current["gazebo_pose_enu_m"]
        if (
            not all(
                lo <= x <= hi
                for lo, x, hi in zip(
                    self.scene["geofence_lower_enu_m"],
                    point,
                    self.scene["geofence_upper_enu_m"],
                )
            )
            or not current["scene_static_verified"]
        ):
            raise RuntimeError("urban scene or geofence violation")
        yaw_error = abs(wrap_angle(current["yaw_ned_rad"] - current["px4_yaw_ned_rad"]))
        if self.phase == "taking_off":
            stamp = current["pose_simulation_time_ns"]
            if yaw_error > 0.25:
                raise RuntimeError("heading disagreement during climb")
            if yaw_error > 0.15:
                if getattr(self, "yaw_exceed_since", None) is None:
                    self.yaw_exceed_since = stamp
                elif stamp - self.yaw_exceed_since >= 500_000_000:
                    raise RuntimeError("sustained heading disagreement")
            else:
                self.yaw_exceed_since = None
        elif yaw_error > 0.15:
            raise RuntimeError("heading disagreement")
        if self.phase in ("holding", "flying_candidate") and (
            current["armed"] is not True or current["px4_main_mode"] != 6
        ):
            raise RuntimeError("PX4 OFFBOARD authority lost")
        for building in self.scene["buildings"]:
            clearance = segment_box_clearance(
                self.previous_point or point,
                point,
                building["lower_enu_m"],
                building["upper_enu_m"],
                self.scene["airframe_radius_m"],
            )
            self.minimum_clearance = min(self.minimum_clearance, clearance)
            if clearance < self.scene["required_clearance_m"]:
                raise RuntimeError("observed airframe envelope clearance below bound")
        self.previous_point = point
        if self.building_contacts:
            raise RuntimeError("building contact observed")

    def hold_for_command(self):
        self.event("urban_measurement_ready", route_sha256=self.config["route_sha256"])
        self.wait(
            lambda _: (self.directory / "urban-go.json").exists(),
            300,
            "bounded urban measurement trigger",
        )
        envelope = json.loads((self.directory / "urban-go.json").read_text())
        validate_trigger(envelope, self.config, self.key, time.time())
        command = envelope["command"]
        if self.config.get("selection_mode") == "anwm":
            raw = (self.directory / "urban-selection.json").read_bytes()
            selection = json.loads(raw)
            if (
                hashlib.sha256(raw).hexdigest() != command["selection_sha256"]
                or selection.get("route_id") != command["route_id"]
                or selection.get("route_sha256") != command["route_sha256"]
                or selection.get("session_id") != self.config["session_id"]
                or selection.get("scene_sha256") != self.config["scene_sha256"]
                or selection.get("model_forecast_used_for_dispatch") is not True
            ):
                raise ValueError("urban selection receipt binding differs")
            current = self.status()
            age = time.time() - selection["observation_unix_s"]
            if (
                not 0 <= age <= 180
                or math.dist(
                    current["gazebo_pose_enu_m"], selection["observation_enu_m"]
                )
                > 0.25
                or abs(
                    wrap_angle(
                        current["yaw_ned_rad"] - selection["observation_yaw_ned_rad"]
                    )
                )
                > 0.1
            ):
                raise ValueError(
                    "urban model observation moved or expired before dispatch"
                )
            self.route = self.scene["routes"][command["route_id"]]
            if not route_check(
                self.scene, self.route, start=current["gazebo_pose_enu_m"]
            )["admissible"]:
                raise ValueError("urban model route clearance rejected at dispatch")
            self.config["route_id"] = command["route_id"]
            self.config["route_sha256"] = command["route_sha256"]
            self.outcome.update(
                model_forecast_used_for_dispatch=True,
                selector=selection["selector"],
                route_id=command["route_id"],
                selection_sha256=command["selection_sha256"],
            )
            self.event(
                "urban_model_selection_consumed",
                selection=selection,
                observation_age_seconds=age,
            )
        (self.directory / "urban-go.json").rename(
            self.directory / "urban-go-accepted.json"
        )
        self.execute_urban_route()

    def execute_urban_route(self):
        start = self.status()
        self.check_bounds(start)
        self.event(
            "urban_route_dispatch",
            route_id=self.config["route_id"],
            route_enu_m=self.route,
            observed_start=start,
            model_forecast_used_for_dispatch=self.outcome[
                "model_forecast_used_for_dispatch"
            ],
        )
        for index, target_enu in enumerate(self.route):
            target_ned = [target_enu[1], target_enu[0], -target_enu[2]]
            current = self.status()
            self.execute_candidate(
                {
                    "candidate_id": self.config["route_id"] + "_" + str(index),
                    "target_local_ned_m": target_ned,
                    "route_sha256": self.config["route_sha256"],
                },
                current,
            )
            self.outcome["completed_waypoints"] = index + 1
            self.event(
                "urban_waypoint_observed",
                index=index,
                target_enu_m=target_enu,
                observed=self.status(),
            )
        goal = self.scene["goal_enu_m"]
        self.stationary_at(
            [goal[1], goal[0], -goal[2]],
            60,
            position_tolerance=self.scene["goal_radius_m"],
            stable_sim_seconds=self.scene["goal_dwell_sim_seconds"],
        )
        self.outcome["goal_reached"] = True
        self.event("urban_goal_observed", observed=self.status())

    def run(self):
        code = super().run()
        self.outcome.update(
            minimum_observed_envelope_clearance_m=(
                self.minimum_clearance
                if math.isfinite(self.minimum_clearance)
                else None
            ),
            building_contact_messages=self.building_contacts,
            contact_topic_messages=self.contact_messages,
            ground_contact_positive_control_messages=self.ground_contacts,
            contact_sensor_streams_observed=all(self.contact_messages.values()),
            complete=bool(self.outcome["complete"] and self.outcome["goal_reached"]),
            urban_verification_complete=True,
            scene_mesh_and_collision_identical=True,
            clearance_method="observed_pose_segment_to_mesh_AABB_minus_0.6m_enclosing_sphere",
        )
        atomic_json(self.directory / "flight-result.json", self.outcome)
        return code if self.outcome["complete"] else 1


def start(root):
    session = root / "session"
    metadata = json.loads(call(["docker", "inspect", NAME]).stdout)[0]
    assert_container(metadata, session, name=NAME, label=LABEL)
    if (session / "events.jsonl").exists():
        raise ValueError("urban flight already started")
    call(
        [
            "docker",
            "exec",
            NAME,
            "/opt/px4-gazebo/bin/px4-mavlink",
            "start",
            "-u",
            "14600",
            "-o",
            "14650",
            "-t",
            "127.0.0.1",
            "-m",
            "onboard",
            "-r",
            "400000",
        ]
    )
    for stream, rate in STREAM_RATES.items():
        call(
            [
                "docker",
                "exec",
                NAME,
                "/opt/px4-gazebo/bin/px4-mavlink",
                "stream",
                "-u",
                "14600",
                "-s",
                stream,
                "-r",
                rate,
            ]
        )
    call(
        [
            "docker",
            "exec",
            "-d",
            "-e",
            OPT_IN + "=1",
            NAME,
            "sh",
            "-c",
            "python3 /session/scripts/urban_route_observer.py > /session/observer.log 2>&1",
        ]
    )
    call(
        [
            "docker",
            "exec",
            "-d",
            "-e",
            OPT_IN + "=1",
            NAME,
            "sh",
            "-c",
            "python3 /session/scripts/px4_urban_wam_trial.py --controller /session > /session/controller.log 2>&1",
        ]
    )


def capture_history(root):
    from scripts.probe_px4_aerial_camera import CAPTURE

    session = root / "session"
    history = session / "history"
    history.mkdir()
    for name in ("airframe.sdf", "camera.sdf", "scene.json"):
        shutil.copyfile(session / name, history / name)
    scene = json.loads((session / "scene.json").read_text())
    for building in scene["buildings"]:
        shutil.copyfile(
            session / (building["name"] + ".sdf"), history / (building["name"] + ".sdf")
        )
    atomic_json(
        history / "capture-config.json",
        {
            "frame_count": 16,
            "target_frame_interval_ns": 250000000,
            "target_tolerance_ns": 4000000,
            "require_contiguous_history": True,
            "capture_scope": "px4_sitl_airborne_observation",
            "scene_entity_names": [b["name"] for b in scene["buildings"]],
            "flight_session_status_file": "/session/status.json",
            "scene_geometry_sha256": scene["scene_sha256"],
        },
    )
    return call(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "MISSIONOS_AERIAL_CAPTURE_ROOT=/session/history",
            NAME,
            "python3",
            "-",
        ],
        input=CAPTURE,
        timeout=150,
    )


def wait_file(session, name, predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        path = session / name
        if path.exists():
            value = json.loads(path.read_text())
            if predicate(value):
                return value
        if name != "flight-result.json" and (session / "flight-result.json").exists():
            raise RuntimeError(
                "flight ended before requested state: "
                + (session / "flight-result.json").read_text()
            )
        time.sleep(0.5)
    raise RuntimeError("timed out waiting for " + name)


def cleanup(root):
    metadata_path = root / "container.json"
    if not metadata_path.exists():
        return
    metadata = json.loads(metadata_path.read_text())
    inspection = json.loads(call(["docker", "inspect", metadata["id"]]).stdout)[0]
    assert_container(inspection, root.resolve() / "session", name=NAME, label=LABEL)
    logs = subprocess.run(
        ["docker", "logs", metadata["id"]], text=True, capture_output=True, timeout=15
    )
    (root / "px4-gazebo.log").write_text(logs.stdout + logs.stderr)
    call(["docker", "rm", "-f", metadata["id"]])
    probe = subprocess.run(
        ["docker", "inspect", metadata["id"]], capture_output=True, timeout=15
    )
    atomic_json(
        root / "cleanup.json", {"created_container_removed": probe.returncode != 0}
    )


def run_trial(args):
    root = args.output_dir.resolve()
    if root.exists():
        raise ValueError("refusing to reuse or clean up a previous trial directory")
    session = root / "session"
    try:
        setup(
            root,
            args.assets_dir.resolve(),
            args.scene,
            args.route,
            args.approved_instruction_ref,
            args.image,
            args.selector,
        )
        print(json.dumps({"scene_ready": args.scene}), flush=True)
        start(root)
        hover = wait_file(
            session, "status.json", lambda s: s.get("phase") == "holding", 420
        )
        print(json.dumps({"hover_observed": hover["gazebo_pose_enu_m"]}), flush=True)
        capture_history(root)
        config = json.loads((session / "config.json").read_text())
        command = {
            key: config[key]
            for key in (
                "session_id",
                "scene_sha256",
                "route_sha256",
                "approved_instruction_ref",
            )
        }
        if args.selector == "anwm":
            from scripts.run_urban_anwm_preview import infer

            print(json.dumps({"urban_model_inference_started": True}), flush=True)
            selection = infer(root, args.gpu_config)
            atomic_json(session / "urban-selection.json", selection)
            command.update(
                route_id=selection["route_id"],
                route_sha256=selection["route_sha256"],
                selection_sha256=hashlib.sha256(
                    (session / "urban-selection.json").read_bytes()
                ).hexdigest(),
            )
        command.update(issued_at_unix_s=time.time(), expires_at_unix_s=time.time() + 4)
        atomic_json(
            session / "urban-go.json",
            sign_command(command, (session / ".dispatch-key").read_bytes()),
        )
        print(
            json.dumps(
                {
                    "route_triggered": command.get("route_id", config["route_id"]),
                    "wam_invoked": args.selector == "anwm",
                }
            ),
            flush=True,
        )
        result = wait_file(
            session,
            "flight-result.json",
            lambda s: s.get("urban_verification_complete") is True,
            600,
        )
        time.sleep(2)  # Observer flushes only this trial's already received frames.
        print(json.dumps(result), flush=True)
        if not result.get("complete"):
            raise RuntimeError("urban route did not complete")
    except Exception as exc:
        if root.exists():
            atomic_json(
                root / "host-error.json",
                {
                    "exception": type(exc).__name__,
                    "message": str(exc),
                    "stderr": getattr(exc, "stderr", None),
                },
            )
        raise
    finally:
        if (root / "container.json").exists():
            if (session / "status.json").exists() and not (
                session / "flight-result.json"
            ).exists():
                try:
                    call(
                        [
                            "docker",
                            "exec",
                            NAME,
                            "pkill",
                            "-TERM",
                            "-f",
                            "^python3 /session/scripts/px4_urban_wam_trial.py",
                        ]
                    )
                    wait_file(session, "flight-result.json", lambda _: True, 120)
                except Exception as exc:
                    print(json.dumps({"landing_recovery_error": str(exc)}), flush=True)
            cleanup(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--phase",
        choices=("fetch-assets", "screen", "run", "cleanup"),
        default="screen",
    )
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--scene", choices=("gap", "climb", "detour"), default="gap")
    parser.add_argument("--route")
    parser.add_argument(
        "--selector", choices=("geometric", "anwm"), default="geometric"
    )
    parser.add_argument(
        "--gpu-config",
        type=Path,
        help="local-only transport config for an already budget-capped ANWM VM",
    )
    parser.add_argument("--approved-instruction-ref", default="")
    parser.add_argument("--image", default="px4io/px4-sitl-gazebo:latest")
    args = parser.parse_args()
    if args.controller:
        require_opt_in()
        if (
            os.getenv("RUNS_IN_DOCKER") != "true"
            or os.getenv("PX4_SIM_MODEL") != "gz_x500_depth"
            or args.controller != Path("/session")
        ):
            raise ValueError("urban controller only runs in isolated stock SITL")
        config = json.loads((args.controller / "config.json").read_text())
        if config.get("selection_mode") == "headroom":
            from scripts.px4_urban_headroom_trial import HeadroomFlightSession

            return HeadroomFlightSession(args.controller).run()
        return UrbanFlightSession(args.controller).run()
    if args.phase == "screen":
        scene = scene_spec(args.scene)
        print(
            json.dumps(
                {
                    "scene": args.scene,
                    "routes": {
                        key: route_check(scene, route)
                        for key, route in scene["routes"].items()
                    },
                    "wam_invoked": False,
                },
                indent=2,
            )
        )
    elif args.phase == "fetch-assets":
        if args.assets_dir is None:
            parser.error("--assets-dir required")
        fetch_assets(args.assets_dir)
    else:
        require_opt_in()
        if args.output_dir is None or (args.phase == "run" and args.assets_dir is None):
            parser.error("--output-dir and, for run, --assets-dir required")
        if args.phase == "cleanup":
            cleanup(args.output_dir)
        else:
            if args.selector == "anwm" and (
                args.gpu_config is None or args.route is not None
            ):
                parser.error(
                    "ANWM selection requires --gpu-config and chooses its own declared route"
                )
            run_trial(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
