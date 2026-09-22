#!/usr/bin/env python3
"""Opt-in, grounded PX4/Gazebo RGB-D capture; no model or flight execution.

The disposable container has no network or host ports. RGB, depth, camera info,
and vehicle pose must have exactly equal simulation timestamps. Separate native
camera intrinsics are retained; this probe does not register depth onto RGB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

NAME = "missionos-aerial-camera-probe"
OPT_IN = "RUN_PX4_AERIAL_CAMERA_PROBE"

# Executed with the image's own Gazebo Python bindings. No packages are installed.
CAPTURE = r'''
import collections
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zlib

from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image
from gz.msgs10.camera_info_pb2 import CameraInfo
from gz.msgs10.pose_v_pb2 import Pose_V
from google.protobuf.json_format import MessageToDict

ROOT = Path('/capture')
CONFIG = json.loads((ROOT / 'capture-config.json').read_text())
FRAME_COUNT = CONFIG['frame_count']
MODEL = 'x500_depth_0'
PREFIX = '/world/default/model/' + MODEL + '/link/camera_link/sensor/IMX214'
TOPICS = {'rgb': PREFIX + '/image', 'depth': '/depth_camera',
          'rgb_info': PREFIX + '/camera_info', 'depth_info': '/camera_info',
          'pose': '/world/default/pose/info'}
LOCK = threading.Lock()
BUCKETS = {key: collections.OrderedDict() for key in TOPICS}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def save_rgb_png(path, image):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\x00' + image.data[y*image.step:(y+1)*image.step] for y in range(image.height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', image.width, image.height, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))

def stamp(message):
    return message.header.stamp.sec * 1000000000 + message.header.stamp.nsec

def header_frame(message):
    values = [value for item in message.header.data if item.key == 'frame_id' for value in item.value]
    if values != ['camera_link']:
        raise ValueError('unexpected camera frame ID')

def callback(kind, message):
    key = stamp(message)
    with LOCK:
        BUCKETS[kind][key] = (message, datetime.now(timezone.utc).isoformat(), time.monotonic())
        # Pose publishes faster than the rendered sensors.
        limit = 512 if kind == 'pose' else 12
        while len(BUCKETS[kind]) > limit:
            BUCKETS[kind].popitem(last=False)

def position(pose):
    return [pose.position.x, pose.position.y, pose.position.z]

def rotation(pose):
    q = pose.orientation
    x, y, z, w = q.x, q.y, q.z, q.w
    if not math.isclose(x*x + y*y + z*z + w*w, 1, abs_tol=1e-5):
        raise ValueError('invalid vehicle orientation')
    return [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
            [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
            [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]

def sensor_extrinsics():
    paths = {key: Path('/opt/px4-gazebo/share/gz/models') / name / 'model.sdf'
             for key, name in [('airframe', 'x500_depth'), ('camera', 'OakD-Lite')]}
    models = {key: ET.fromstring(path.read_text()).find('model') for key, path in paths.items()}
    includes = [item for item in models['airframe'].findall('include')
                if item.findtext('uri') == 'model://OakD-Lite']
    if len(includes) != 1 or includes[0].get('merge') != 'true':
        raise ValueError('unsupported camera mount')
    joint = models['airframe'].find("joint[@name='CameraJoint']")
    if joint is None or joint.get('type') != 'fixed' or joint.findtext('parent') != 'base_link' or joint.findtext('child') != 'camera_link':
        raise ValueError('camera mount must be fixed to base_link')
    mount = [float(x) for x in includes[0].findtext('pose').split()]
    result = {}
    for key, name in [('rgb', 'IMX214'), ('depth', 'StereoOV7251')]:
        sensor = models['camera'].find("link[@name='camera_link']/sensor[@name='%s']" % name)
        pose = [float(x) for x in sensor.findtext('pose').split()]
        if len(mount) != 6 or len(pose) != 6 or any(abs(x) > 1e-12 for x in mount[3:] + pose[3:]):
            raise ValueError('unsupported rotated camera extrinsics')
        result[key] = {'sensor_name': name, 'translation_from_model_m': [mount[i] + pose[i] for i in range(3)],
                       'rotation_from_model_rpy_rad': [0, 0, 0],
                       'near_clip_m': float(sensor.findtext('camera/clip/near')),
                       'far_clip_m': float(sensor.findtext('camera/clip/far'))}
    return result, {key: sha(path.read_bytes()) for key, path in paths.items()}

def write_frame(index, sim_ns, bundle, extrinsics):
    messages = {key: value[0] for key, value in bundle.items()}
    vehicle = [item for item in messages['pose'].pose if item.name == MODEL]
    if len(vehicle) != 1:
        raise ValueError('exact vehicle pose unavailable')
    vehicle = vehicle[0]
    camera_link = [item for item in messages['pose'].pose if item.name == 'camera_link']
    if len(camera_link) != 1:
        raise ValueError('exact camera link pose unavailable')
    xyz = position(vehicle)
    if any(not math.isfinite(x) for x in xyz) or abs(xyz[2]) > 0.5:
        raise ValueError('grounded pose boundary violated')
    rot = rotation(vehicle)
    prefix = 'frame_%02d' % index
    record = {'simulation_time_ns': sim_ns, 'source_timestamp_match': 'exact',
              'source_simulation_timestamps_ns': {key: stamp(value) for key, value in messages.items()},
              'received_at': {key: value[1] for key, value in bundle.items()},
              'vehicle_model_pose': MessageToDict(vehicle, preserving_proto_field_name=True),
              'reported_camera_link_pose': MessageToDict(camera_link[0], preserving_proto_field_name=True),
              'pose_message_sha256': sha(messages['pose'].SerializeToString()), 'sensors': {}}
    record['scene_model_poses'] = [MessageToDict(item, preserving_proto_field_name=True)
                                   for item in messages['pose'].pose
                                   if item.name in CONFIG['scene_entity_names']]
    (ROOT / (prefix + '-pose.pb')).write_bytes(messages['pose'].SerializeToString())
    for key in ('rgb', 'depth'):
        image, info = messages[key], messages[key + '_info']
        header_frame(image)
        header_frame(info)
        pixel_name = image.DESCRIPTOR.fields_by_name['pixel_format_type'].enum_type.values_by_number[image.pixel_format_type].name
        expected = 'RGB_INT8' if key == 'rgb' else 'R_FLOAT32'
        channels = 3 if key == 'rgb' else 4
        if pixel_name != expected or image.step != image.width * channels or len(image.data) != image.height * image.step:
            raise ValueError('unsupported raw image layout: ' + pixel_name)
        if (info.width, info.height) != (image.width, image.height) or len(info.intrinsics.k) != 9:
            raise ValueError('image and camera calibration mismatch')
        if not all(math.isfinite(x) for x in info.intrinsics.k) or info.intrinsics.k[0] <= 0 or info.intrinsics.k[4] <= 0:
            raise ValueError('invalid measured intrinsics')
        filename = prefix + '-' + key + '.bin'
        (ROOT / filename).write_bytes(image.data)
        if key == 'rgb':
            save_rgb_png(ROOT / (prefix + '-rgb.png'), image)
        ext = extrinsics[key]
        camera_xyz = [xyz[i] + sum(rot[i][j] * ext['translation_from_model_m'][j] for j in range(3)) for i in range(3)]
        sensor = {'topic': TOPICS[key], 'raw_file': filename, 'raw_sha256': sha(image.data),
                  'source_message_sha256': sha(image.SerializeToString()),
                  'width': image.width, 'height': image.height, 'step': image.step,
                  'pixel_format': pixel_name, 'camera_info': MessageToDict(info, preserving_proto_field_name=True),
                  'camera_info_message_sha256': sha(info.SerializeToString()),
                  'declared_sensor_extrinsics': ext,
                  'camera_pose_world': {'position_m': camera_xyz,
                      'orientation_xyzw': [vehicle.orientation.x, vehicle.orientation.y, vehicle.orientation.z, vehicle.orientation.w],
                      'basis': 'live_model_pose_plus_fixed_SDF_sensor_extrinsics',
                      'frame': 'gazebo_sensor_link_axes_not_optical'},
                  'depth_registered_to_rgb': False}
        if key == 'rgb':
            sensor.update(png_file=prefix + '-rgb.png', png_sha256=sha((ROOT / (prefix + '-rgb.png')).read_bytes()))
        if key == 'depth':
            finite = 0
            minimum, maximum = math.inf, -math.inf
            for (value,) in struct.iter_unpack('<f', image.data):
                if math.isfinite(value):
                    if value < 0:
                        raise ValueError('negative depth sample')
                    finite += 1
                    minimum, maximum = min(minimum, value), max(maximum, value)
            if not finite:
                raise ValueError('no finite depth measurements')
            sensor.update(depth_units='metres', byte_order='little', finite_depth_pixels=finite,
                          invalid_depth_pixels=image.width * image.height - finite,
                          finite_depth_min_m=minimum, finite_depth_max_m=maximum,
                          invalid_depth_values_preserved=True)
        record['sensors'][key] = sensor
    return record

ROOT.mkdir(parents=True, exist_ok=True)
extrinsics, sdf_hashes = sensor_extrinsics()
if extrinsics['rgb']['translation_from_model_m'] != extrinsics['depth']['translation_from_model_m']:
    raise ValueError('this probe only supports verified co-located sensor origins')
node = Node()
for kind, topic in TOPICS.items():
    cls = Pose_V if kind == 'pose' else CameraInfo if kind.endswith('_info') else Image
    node.subscribe(cls, topic, lambda message, key=kind: callback(key, message))
deadline = time.monotonic() + 120
bundles = []
anchor = None
while time.monotonic() < deadline and len(bundles) < FRAME_COUNT:
    chosen = None
    with LOCK:
        common = set.intersection(*(set(bucket) for bucket in BUCKETS.values()))
        target = 0 if anchor is None else anchor + len(bundles) * 250000000
        previous = -1 if not bundles else bundles[-1][0]
        # The default world advances in 4 ms steps. A 250 ms period alternates
        # 248/252 ms; retain these measured frames instead of skipping the one
        # 2 ms before the nominal target and silently doubling the interval.
        valid = [value for value in common
                 if value >= target - CONFIG['target_tolerance_ns'] and value > previous]
        if valid:
            chosen = min(valid)
            bundle = {key: bucket[chosen] for key, bucket in BUCKETS.items()}
    if chosen is None:
        time.sleep(.01)
        continue
    if max(time.monotonic() - item[2] for item in bundle.values()) > 5:
        raise ValueError('sensor capture became stale during collection')
    if anchor is None:
        anchor = chosen
    bundles.append((chosen, bundle))
if len(bundles) != FRAME_COUNT:
    raise RuntimeError('requested synchronized RGB/depth/calibration/pose samples were not observed')
# Retain raw synchronized messages first. PNG compression must not make history
# skip simulator timestamps. Every output remains an independently received frame.
records = [write_frame(index, sim_ns, bundle, extrinsics)
           for index, (sim_ns, bundle) in enumerate(bundles)]
jitter_ns = [record['simulation_time_ns'] - (anchor + index * 250000000)
             for index, record in enumerate(records)]
result = {'schema_version': 'missionos_px4_aerial_camera_probe.v1',
          'source_kind': 'actual_px4_gazebo_sensor_capture', 'source_topics': TOPICS,
          'capture_configuration': CONFIG,
          'history_timing': {'target_period_ns': 250000000,
              'actual_timestamps_ns': [record['simulation_time_ns'] for record in records],
              'target_lateness_ns': jitter_ns, 'maximum_lateness_ns': max(jitter_ns),
              'maximum_absolute_phase_error_ns': max(abs(value) for value in jitter_ns),
              'interpolated_frames': 0, 'duplicated_frames': 0,
              'physical_wall_clock_timing_claimed': False},
          'model_sdf_sha256': sdf_hashes, 'frames': records,
          'model_sdf_files': {'airframe': 'airframe.sdf', 'camera': 'camera.sdf'},
          'rgb_to_depth_sensor_transform': {'translation_m': [0, 0, 0], 'rotation_xyzw': [0, 0, 0, 1],
              'basis': 'identical fixed sensor poses in the observed model SDF',
              'pixel_registration_performed': False},
          'capture_completed_at': datetime.now(timezone.utc).isoformat(),
          'hardware_target_allowed': False, 'arm_command_sent': False, 'flight_command_sent': False,
          'model_inference_invoked': False, 'goal_image_declared': False,
          'depth_registered_to_rgb': False, 'model_input_ready': False,
          'dispatch_authority_created': False, 'physical_execution_invoked': False,
          'limitations': ['RGB and depth retain different native resolutions and intrinsics',
                         'camera pose uses Gazebo sensor axes; optical-frame conversion is not established',
                         'invalid depth values are preserved; no depth filling is performed',
                         'grounded capture does not establish flight, prediction, or collision avoidance']}
(ROOT / 'capture.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
print(json.dumps({'synchronized_frames': len(records), 'model_input_ready': False}))
'''


def run(command, *, timeout=15, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)


def calibration_scene():
    """Declared static geometry for sensor checks, not a navigation benchmark."""
    return [
        {"name": "aerial_calibration_wall", "center_xyz_m": [3, 0, 1.5],
         "size_xyz_m": [0.25, 2, 3], "rgba": [0.9, 0.05, 0.05, 1]},
        {"name": "aerial_calibration_backdrop", "center_xyz_m": [8, 0, 6],
         "size_xyz_m": [0.25, 16, 12], "rgba": [0.05, 0.15, 0.9, 1]},
    ]


def box_sdf(box):
    pose = " ".join(map(str, box["center_xyz_m"]))
    size = " ".join(map(str, box["size_xyz_m"]))
    color = " ".join(map(str, box["rgba"]))
    return (
        f'<sdf version="1.9"><model name="{box["name"]}"><static>true</static>'
        f'<pose>{pose} 0 0 0</pose><link name="box_link">'
        f'<collision name="box_collision"><geometry><box><size>{size}</size>'
        '</box></geometry></collision><visual name="box_visual">'
        f'<geometry><box><size>{size}</size></box></geometry>'
        f'<material><ambient>{color}</ambient><diffuse>{color}</diffuse>'
        '</material></visual></link></model></sdf>'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--image", default="px4io/px4-sitl-gazebo:latest")
    parser.add_argument("--frames", type=int, choices=range(4, 33), default=4)
    parser.add_argument("--scene", choices=("default", "calibration-wall"), default="default")
    parser.add_argument("--sensor-profile", choices=("native", "history-4hz"), default="native")
    parser.add_argument("--sim-speed-factor", type=float, choices=(0.1, 0.25, 1.0), default=1.0)
    args = parser.parse_args()
    if os.environ.get(OPT_IN) != "1":
        parser.error(f"set {OPT_IN}=1 to start the disposable simulator")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    scene = calibration_scene() if args.scene == "calibration-wall" else []
    (output / "capture-config.json").write_text(json.dumps({
        "frame_count": args.frames, "target_frame_interval_ns": 250000000,
        "target_tolerance_ns": 4000000,
        "intervals_uniform": False, "scene": args.scene,
        "sensor_profile": args.sensor_profile,
        "requested_simulation_speed_factor": args.sim_speed_factor,
        "scene_entity_names": [item["name"] for item in scene],
        "purpose": "sensor_calibration_and_candidate_geometry_screen",
    }, indent=2) + "\n")
    image = json.loads(run(["docker", "image", "inspect", args.image]).stdout)[0]
    sensor_mount = []
    if args.sensor_profile == "history-4hz":
        sensor_path = "/opt/px4-gazebo/share/gz/models/OakD-Lite/model.sdf"
        original = run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                        "/bin/cat", image["Id"], sensor_path]).stdout
        (output / "original-camera.sdf").write_text(original)
        tree = ET.fromstring(original)
        sensors = tree.findall("model/link[@name='camera_link']/sensor")
        if {item.get("name") for item in sensors} != {"IMX214", "StereoOV7251"}:
            raise RuntimeError("unsupported camera model for history sensor profile")
        for sensor in sensors:
            sensor.find("update_rate").text = "4"
            if sensor.get("name") == "IMX214":
                sensor.find("camera/image/width").text = "640"
                sensor.find("camera/image/height").text = "360"
        override = ET.tostring(tree, encoding="unicode") + "\n"
        override_path = output / "history-camera.sdf"
        override_path.write_text(override)
        (output / "sensor-profile.json").write_text(json.dumps({
            "name": args.sensor_profile, "original_sdf_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "effective_sdf_sha256": hashlib.sha256(override.encode()).hexdigest(),
            "rgb_native_resolution": [640, 360], "depth_native_resolution": [640, 480],
            "configured_sensor_rate_hz": 4, "frames_interpolated": False,
            "timestamps_are_measured": True,
        }, indent=2) + "\n")
        sensor_mount = ["--mount", f"type=bind,src={override_path},dst={sensor_path},readonly"]
    container_id = None
    cleaned = False
    try:
        container_id = run([
            "docker", "run", "-d", "--name", NAME, "--network", "none",
            "--mount", f"type=bind,src={output},dst=/capture",
            "-e", "PX4_SIM_MODEL=gz_x500_depth", "-e", "PX4_GZ_WORLD=default",
            "-e", "HEADLESS=1", "-e", "PX4_GZ_NO_FOLLOW=1",
            "-e", f"PX4_SIM_SPEED_FACTOR={args.sim_speed_factor}",
            "-e", "LIBGL_ALWAYS_SOFTWARE=1", *sensor_mount, image["Id"], "-d",
        ], timeout=30).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise RuntimeError("Docker did not return the created container identity")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            logs = run(["docker", "logs", container_id]).stdout
            if "Startup script returned successfully" in logs and "model: x500_depth_0" in logs:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("PX4 depth-camera startup timed out")
        for key, model in (("airframe", "x500_depth"), ("camera", "OakD-Lite")):
            run(["docker", "cp",
                 f"{container_id}:/opt/px4-gazebo/share/gz/models/{model}/model.sdf",
                 str(output / f"{key}.sdf")])
        scene_receipts = []
        for box in scene:
            sdf = box_sdf(box)
            (output / (box["name"] + ".sdf")).write_text(sdf + "\n")
            response = run([
                "docker", "exec", container_id, "gz", "service", "-s", "/world/default/create",
                "--reqtype", "gz.msgs.EntityFactory", "--reptype", "gz.msgs.Boolean",
                "--timeout", "10000", "--req", "sdf: " + json.dumps(sdf),
            ], timeout=15)
            if "data: true" not in response.stdout:
                raise RuntimeError("calibration scene entity creation was not acknowledged")
            scene_receipts.append({**box, "spawn_response": response.stdout.strip()})
        (output / "scene.json").write_text(json.dumps({
            "schema_version": "missionos_px4_calibration_scene.v1", "boxes": scene_receipts,
            "flight_outcomes_observed": False,
        }, indent=2) + "\n")
        capture = run(["docker", "exec", "-i", container_id, "python3", "-"],
                      timeout=140, input=CAPTURE)
        (output / "capture-process.json").write_text(json.dumps({
            "image_id": image["Id"], "image_architecture": image["Architecture"],
            "container_id": container_id, "network_mode": "none",
            "capture_exit_code": capture.returncode, "capture_stdout": capture.stdout,
        }, indent=2) + "\n")
    finally:
        if container_id and re.fullmatch(r"[0-9a-f]{64}", container_id):
            try:
                logs = subprocess.run(["docker", "logs", container_id], capture_output=True, text=True, timeout=10)
                (output / "px4-gazebo.log").write_text(logs.stdout + logs.stderr)
            finally:
                run(["docker", "rm", "-f", container_id])
                probe = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, timeout=10)
                cleaned = probe.returncode != 0
                (output / "cleanup.json").write_text(json.dumps({"container_removed": cleaned,
                    "other_containers_modified": False}) + "\n")
    if not cleaned:
        raise RuntimeError("probe container cleanup was not verified")
    print(json.dumps({"capture": str(output / "capture.json"), "container_removed": cleaned,
                      "model_input_ready": False}))


if __name__ == "__main__":
    main()
