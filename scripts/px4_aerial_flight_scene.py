#!/usr/bin/env python3
"""Opt-in isolated aerial integration scene and observer, without flight commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

from scripts.probe_px4_aerial_camera import CAPTURE, box_sdf, run

NAME = "missionos-aerial-flight-e2e"
OPT_IN = "RUN_PX4_AERIAL_WAM_FLIGHT"
BOXES = [
    {
        "name": "aerial_front_wall",
        "center_xyz_m": [3, 0, 3],
        "size_xyz_m": [0.25, 2, 6],
        "rgba": [0.8, 0.05, 0.05, 1],
    },
    {
        "name": "aerial_backdrop",
        "center_xyz_m": [8, 0, 5],
        "size_xyz_m": [0.25, 20, 10],
        "rgba": [0.05, 0.1, 0.7, 1],
    },
    {
        "name": "aerial_left_marker",
        "center_xyz_m": [4, 5, 3.2],
        "size_xyz_m": [0.5, 1.5, 2],
        "rgba": [0.05, 0.85, 0.1, 1],
    },
    {
        "name": "aerial_right_marker",
        "center_xyz_m": [4, -5, 3.2],
        "size_xyz_m": [0.5, 1.5, 2],
        "rgba": [0.85, 0.05, 0.8, 1],
    },
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


GOAL_CAPTURE = r"""
from datetime import datetime, timezone
import hashlib,json,math,struct,threading,time,zlib
from pathlib import Path
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image
from gz.msgs10.camera_info_pb2 import CameraInfo
from gz.msgs10.pose_v_pb2 import Pose_V
from google.protobuf.json_format import MessageToDict
root=Path('/session/goal');root.mkdir(exist_ok=True)
prefix='/world/default/model/aerial_goal_reference/link/goal_camera_link/sensor/goal_rgb'
buckets={k:{} for k in ('rgb','info','pose')};lock=threading.Lock()
def callback(key,msg):
    stamp=msg.header.stamp.sec*1000000000+msg.header.stamp.nsec
    with lock:
        buckets[key][stamp]=msg
        while len(buckets[key])>512:del buckets[key][min(buckets[key])]
node=Node()
node.subscribe(Image,prefix+'/image',lambda msg:callback('rgb',msg))
node.subscribe(CameraInfo,prefix+'/camera_info',lambda msg:callback('info',msg))
node.subscribe(Pose_V,'/world/default/pose/info',lambda msg:callback('pose',msg))
deadline=time.monotonic()+45;bundle=None
while time.monotonic()<deadline:
    with lock:
        common=set.intersection(*(set(v) for v in buckets.values()))
        if common:
            stamp=max(common);bundle={k:v[stamp] for k,v in buckets.items()};break
    time.sleep(.02)
if bundle is None:raise RuntimeError('synchronized reference camera unavailable')
rgb,info=bundle['rgb'],bundle['info']
if (rgb.width,rgb.height,rgb.step)!=(640,360,1920) or (info.width,info.height)!=(640,360):
    raise ValueError('unexpected goal camera geometry')
poses=[p for p in bundle['pose'].pose if p.name=='aerial_goal_reference']
if len(poses)!=1:raise ValueError('reference camera pose unavailable')
p=poses[0]
if max(abs(a-b) for a,b in zip((p.position.x,p.position.y,p.position.z),(.13233,5,3.26078)))>1e-6:
    raise ValueError('reference camera moved')
if abs(p.orientation.w-1)>1e-6:raise ValueError('reference camera rotated')
def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
rows=b''.join(b'\0'+rgb.data[y*rgb.step:(y+1)*rgb.step] for y in range(rgb.height))
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',640,360,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')
(root/'goal.png').write_bytes(png)
(root/'goal-rgb.bin').write_bytes(rgb.data)
(root/'goal-rgb.pb').write_bytes(rgb.SerializeToString())
(root/'goal-camera-info.pb').write_bytes(info.SerializeToString())
(root/'goal-pose.pb').write_bytes(bundle['pose'].SerializeToString())
scene=json.loads(Path('/session/scene.json').read_text())
sdf=Path('/session/goal-camera.sdf').read_bytes();(root/'goal-camera.sdf').write_bytes(sdf)
K=list(info.intrinsics.k)
result={'schema_version':'missionos_aerial_goal_reference.v1','role':'declared_goal_reference',
 'source_kind':'actual_gazebo_static_camera','rgb_file':'goal.png','rgb_sha256':hashlib.sha256(png).hexdigest(),
 'width':640,'height':360,'intrinsics':[K[0:3],K[3:6],K[6:9]],
 'optical_to_local_ned':[[ -1,0,0,5],[0,0,1,.13233],[0,1,0,-3.26078],[0,0,0,1]],
 'simulation_time_ns':stamp,'received_at':datetime.now(timezone.utc).isoformat(),
 'scene_geometry_sha256':scene['scene_geometry_sha256'],
 'source_message_sha256':hashlib.sha256(rgb.SerializeToString()).hexdigest(),
 'source_message_file':'goal-rgb.pb',
 'camera_info':MessageToDict(info,preserving_proto_field_name=True),
 'observed_reference_pose':MessageToDict(p,preserving_proto_field_name=True),
 'camera_sdf_file':'goal-camera.sdf','camera_sdf_sha256':hashlib.sha256(sdf).hexdigest(),
 'future_outcome_image':False,'candidate_outcome_observed':False,'flight_command_sent':False}
(root/'reference.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'goal_reference_captured':True,'simulation_time_ns':stamp}))
"""


def setup(root, image):
    if (root / "container.json").exists():
        raise ValueError("scene already initialized")
    session = root / "session"
    session.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(run(["docker", "image", "inspect", image]).stdout)[0]
    model_path = "/opt/px4-gazebo/share/gz/models/OakD-Lite/model.sdf"
    original = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/cat",
            metadata["Id"],
            model_path,
        ]
    ).stdout
    tree = ET.fromstring(original)
    for sensor in tree.findall("model/link/sensor"):
        sensor.find("update_rate").text = "4"
        if sensor.get("name") == "IMX214":
            sensor.find("camera/image/width").text = "640"
            sensor.find("camera/image/height").text = "360"
    (session / "camera.sdf").write_text(ET.tostring(tree, encoding="unicode") + "\n")
    (session / "original-camera.sdf").write_text(original)
    # The reference is a separate static camera at the declared goal, captured
    # before candidate execution. It is not an observed candidate outcome.
    goal_sdf = """<sdf version="1.9"><model name="aerial_goal_reference"><static>true</static>
<pose>.13233 5 3.26078 0 0 0</pose><link name="goal_camera_link">
<sensor name="goal_rgb" type="camera"><gz_frame_id>goal_camera_link</gz_frame_id>
<camera><horizontal_fov>1.204</horizontal_fov><image><width>640</width><height>360</height></image>
<clip><near>0.1</near><far>100</far></clip></camera><always_on>1</always_on><update_rate>4</update_rate>
</sensor></link></model></sdf>"""
    (session / "goal-camera.sdf").write_text(goal_sdf)
    scene_geometry = {
        "schema_version": "missionos_px4_calibration_scene.v1",
        "flight_outcomes_observed": False,
        "boxes": BOXES,
        "goal_camera_pose_enu": [0.13233, 5, 3.26078, 0, 0, 0],
    }
    box_records = [
        {
            "name": b["name"],
            "center_xyz_m": list(map(float, b["center_xyz_m"])),
            "size_xyz_m": list(map(float, b["size_xyz_m"])),
            "sdf_sha256": sha(box_sdf(b).encode()),
        }
        for b in BOXES
    ]
    digest = sha(json.dumps(box_records, sort_keys=True, separators=(",", ":")).encode())
    write_json(session / "scene.json", {**scene_geometry, "scene_geometry_sha256": digest})
    entities = [
        {
            "name": b["name"],
            "position": dict(zip("xyz", b["center_xyz_m"])),
            "orientation": {"x": 0, "y": 0, "z": 0, "w": 1},
        }
        for b in BOXES
    ]
    entities.append(
        {
            "name": "aerial_goal_reference",
            "position": {"x": 0.13233, "y": 5, "z": 3.26078},
            "orientation": {"x": 0, "y": 0, "z": 0, "w": 1},
        }
    )
    write_json(session / "scene-entities.json", entities)
    identity = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            NAME,
            "--network",
            "none",
            "--label",
            "missionos.scope=aerial-wam-jev-sitl",
            "--mount",
            f"type=bind,src={session},dst=/session",
            "--mount",
            f"type=bind,src={session / 'camera.sdf'},dst={model_path},readonly",
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
            "PX4_SIM_SPEED_FACTOR=0.1",
            metadata["Id"],
            "-d",
        ],
        timeout=30,
    ).stdout.strip()
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("created container identity invalid")
    write_json(
        root / "container.json",
        {"id": identity, "name": NAME, "image_id": metadata["Id"], "scene_sha256": digest},
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "Startup script returned successfully" in run(["docker", "logs", identity]).stdout:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("PX4 startup timed out; cleanup the recorded container")
    # PX4's speed-factor update sends a Physics request without gravity. The
    # installed Gazebo version applies the protobuf default (zero) at runtime.
    # Restore the world's declared gravity before any authorized flight starts.
    physics = {
        "gravity_m_s2": [0.0, 0.0, -9.8],
        "real_time_factor": 0.1,
        "max_step_size_seconds": 0.004,
    }
    response = run(
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
            "gravity: {x: 0, y: 0, z: -9.8} real_time_factor: 0.1 max_step_size: 0.004",
        ],
        timeout=10,
    )
    if "data: true" not in response.stdout:
        raise RuntimeError("explicit simulator physics configuration rejected")
    write_json(
        session / "physics-configuration.json",
        {
            "schema_version": "missionos_px4_sitl_physics_configuration.v1",
            **physics,
            "service": "/world/default/set_physics",
            "request_accepted": True,
            "flight_outcome_observed": False,
        },
    )
    run(
        [
            "docker",
            "cp",
            f"{identity}:/opt/px4-gazebo/share/gz/models/x500_depth/model.sdf",
            str(session / "airframe.sdf"),
        ]
    )
    for name, sdf in [(b["name"], box_sdf(b)) for b in BOXES] + [
        ("aerial_goal_reference", goal_sdf)
    ]:
        (session / f"{name}.sdf").write_text(sdf)
        response = run(
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
            ],
            timeout=15,
        )
        if "data: true" not in response.stdout:
            raise RuntimeError("scene spawn rejected")
    print(json.dumps({"scene_ready": True, "container": NAME, "scene_sha256": digest}))


def observe(root, phase):
    metadata = json.loads((root / "container.json").read_text())
    identity, session = metadata["id"], root / "session"
    info = json.loads(run(["docker", "inspect", identity]).stdout)[0]
    if (
        info["HostConfig"]["NetworkMode"] != "none"
        or info["Config"]["Labels"].get("missionos.scope") != "aerial-wam-jev-sitl"
    ):
        raise ValueError("not the isolated aerial simulator")
    if phase == "goal":
        result = run(
            ["docker", "exec", "-i", identity, "python3", "-"], input=GOAL_CAPTURE, timeout=55
        )
    else:
        capture = session / "history"
        capture.mkdir()
        for name in (
            "airframe.sdf",
            "camera.sdf",
            "scene.json",
            *(b["name"] + ".sdf" for b in BOXES),
        ):
            (capture / name).write_bytes((session / name).read_bytes())
        write_json(
            capture / "capture-config.json",
            {
                "frame_count": 16,
                "target_frame_interval_ns": 250000000,
                "target_tolerance_ns": 4000000,
                "capture_scope": "px4_sitl_airborne_observation",
                "scene_entity_names": [b["name"] for b in BOXES],
                "flight_session_status_file": "/session/status.json",
                "scene_geometry_sha256": metadata["scene_sha256"],
            },
        )
        result = run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "MISSIONOS_AERIAL_CAPTURE_ROOT=/session/history",
                identity,
                "python3",
                "-",
            ],
            input=CAPTURE,
            timeout=150,
        )
    print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("setup", "goal", "history", "cleanup"), required=True)
    parser.add_argument("--image", default="px4io/px4-sitl-gazebo:latest")
    args = parser.parse_args()
    if os.environ.get(OPT_IN) != "1":
        parser.error(f"set {OPT_IN}=1 for the isolated simulator")
    root = args.output_dir.resolve()
    if args.phase == "setup":
        setup(root, args.image)
    elif args.phase == "cleanup":
        metadata = json.loads((root / "container.json").read_text())
        logs = subprocess.run(
            ["docker", "logs", metadata["id"]], text=True, capture_output=True, timeout=10
        )
        (root / "px4-gazebo.log").write_text(logs.stdout + logs.stderr)
        run(["docker", "rm", "-f", metadata["id"]])
        probe = subprocess.run(
            ["docker", "inspect", metadata["id"]], capture_output=True, timeout=10
        )
        write_json(root / "cleanup.json", {"created_container_removed": probe.returncode != 0})
    else:
        observe(root, args.phase)


if __name__ == "__main__":
    main()
