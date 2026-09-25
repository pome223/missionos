"""Independent frozen-capture boundaries using explicitly synthetic sensor data."""

import hashlib
import json

import numpy as np
import pytest

from scripts.aerial_anwm_runtime import target_pose_from_delta
from src.prediction.px4_camera import optical_pose_local_ned, register_capture


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pose(name, *, x=0.0, y=0.0, z=0.0, yaw=0.0):
    return {
        "name": name,
        "position": {"x": x, "y": y, "z": z},
        "orientation": {"x": 0.0, "y": 0.0, "z": float(np.sin(yaw / 2)), "w": float(np.cos(yaw / 2))},
    }


@pytest.mark.parametrize("yaw", [0.0, np.pi / 2])
def test_registered_pose_and_existing_frd_actions_share_right_and_down_conventions(yaw):
    camera_pose = optical_pose_local_ned(
        pose("x500_depth_0", yaw=yaw), pose("camera_link"), np.eye(4),
        expected_model_from_link=np.eye(4),
    )
    # A Gazebo FLU right displacement is (sin(yaw), -cos(yaw), 0).
    # ENU -> local NED gives (-cos(yaw), sin(yaw), 0); down is NED +Z.
    expected_right = np.array([-np.cos(yaw), np.sin(yaw), 0.0])
    np.testing.assert_allclose(camera_pose[:3, 0], expected_right, atol=1e-12)
    np.testing.assert_allclose(camera_pose[:3, 1], [0, 0, 1], atol=1e-12)
    for delta, expected in (([0, 1, 0, 0], expected_right), ([0, 0, 1, 0], [0, 0, 1])):
        candidate = target_pose_from_delta(np, camera_pose, np.array(delta, dtype=float))
        np.testing.assert_allclose(candidate[:3, 3] - camera_pose[:3, 3], expected, atol=1e-12)


@pytest.fixture
def synthetic_capture(tmp_path):
    airframe = b'''<sdf version="1.9"><model name="x500_depth">
      <include merge="true"><uri>model://OakD-Lite</uri><pose>0.12 0 0 0 0 0</pose></include>
      <joint name="CameraJoint" type="fixed"><parent>base_link</parent><child>camera_link</child></joint>
    </model></sdf>'''
    sensor_template = '''<sensor name="{name}" type="{kind}">
      <pose>0 0 0.02 0 0 0</pose><gz_frame_id>camera_link</gz_frame_id>
      <camera><horizontal_fov>{fov}</horizontal_fov><image><width>2</width><height>2</height></image>
      <clip><near>0.1</near><far>10</far></clip></camera></sensor>'''
    sensors = "".join(sensor_template.format(name=name, kind=kind, fov=np.pi / 2)
                      for name, kind in (("IMX214", "camera"), ("StereoOV7251", "depth_camera")))
    camera = ('<sdf version="1.9"><model name="OakD-Lite"><link name="camera_link">'
              '<inertial><pose>99 99 99 0 0 0</pose></inertial>' + sensors + '</link></model></sdf>').encode()
    airframe_path, camera_path = tmp_path / "airframe.sdf", tmp_path / "camera.sdf"
    airframe_path.write_bytes(airframe)
    camera_path.write_bytes(camera)
    matrix = np.array([[1.0, 0.0, 1.0], [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]])
    records = []
    for index, stamp in enumerate((250_000_000, 500_000_000)):
        pose_wire = b"synthetic opaque pose bytes, not an actual transport message"
        (tmp_path / f"frame_{index:02d}-pose.pb").write_bytes(pose_wire)
        record = {
            "simulation_time_ns": stamp,
            "source_timestamp_match": "exact",
            "source_simulation_timestamps_ns": {key: stamp for key in ("rgb", "depth", "rgb_info", "depth_info", "pose")},
            "pose_message_sha256": sha(pose_wire),
            "vehicle_model_pose": pose("x500_depth_0", x=10, y=20),
            "reported_camera_link_pose": pose("camera_link", x=0.12),
            "sensors": {},
        }
        for key in ("rgb", "depth"):
            raw = (np.arange(12, dtype=np.uint8).reshape(2, 2, 3).tobytes() if key == "rgb" else
                   np.array([[1.0, np.nan], [np.inf, 2.0]], dtype="<f4").tobytes())
            filename = f"frame_{index:02d}-{key}.bin"
            (tmp_path / filename).write_bytes(raw)
            info = {
                "width": 2, "height": 2,
                "header": {"stamp": {"sec": 0, "nsec": stamp}, "data": [{"key": "frame_id", "value": ["camera_link"]}]},
                "intrinsics": {"k": matrix.ravel().tolist()},
                "distortion": {"k": [0.0] * 5},
                "rectification_matrix": np.eye(3).ravel().tolist(),
                "projection": {"p": np.column_stack((matrix, np.zeros(3))).ravel().tolist()},
            }
            record["sensors"][key] = {
                "width": 2, "height": 2, "step": 6 if key == "rgb" else 8,
                "pixel_format": "RGB_INT8" if key == "rgb" else "R_FLOAT32",
                "raw_file": filename, "raw_sha256": sha(raw), "camera_info": info,
                "camera_info_message_sha256": sha(b"synthetic calibration wire marker"),
                "depth_units": "metres", "byte_order": "little",
            }
        records.append(record)
    capture = {
        "schema_version": "missionos_px4_aerial_camera_probe.v1",
        "source_kind": "actual_px4_gazebo_sensor_capture",
        "model_sdf_sha256": {"airframe": sha(airframe), "camera": sha(camera)},
        "frames": records,
        **{key: False for key in ("hardware_target_allowed", "arm_command_sent", "flight_command_sent", "model_inference_invoked", "dispatch_authority_created", "physical_execution_invoked")},
    }
    return capture, tmp_path / "capture.json", airframe_path, camera_path


def register_fixture(fixture):
    capture, capture_path, airframe_path, camera_path = fixture
    capture_path.write_text(json.dumps(capture))
    return register_capture(capture_path, airframe_sdf=airframe_path, camera_sdf=camera_path,
                            output_dir=capture_path.parent / "registered")


def test_frozen_receipt_binds_measured_geometry_and_preserves_unknown_depth(synthetic_capture):
    manifest = register_fixture(synthetic_capture)
    _, capture_path, _, _ = synthetic_capture
    assert manifest["source_kind"] == "px4_gazebo_frozen_capture"
    assert manifest["capture_sha256"] == sha(capture_path.read_bytes())
    assert manifest["simulation_timing_verified"] is True
    assert manifest["frozen_replay_only"] is True
    for key in ("live_freshness_established", "model_time_alignment_verified", "model_input_ready",
                "model_inference_invoked", "goal_image_declared", "dispatch_authority_created",
                "physical_execution_invoked", "transport_field_decode_independently_verified"):
        assert manifest[key] is False
    assert manifest["world_frame"] == "local_ned_from_gazebo_enu"
    assert manifest["camera_frame"] == "optical_right_down_forward"
    for record in manifest["frames"]:
        artifact = capture_path.parent / "registered" / record["file"]
        assert record["sha256"] == sha(artifact.read_bytes())
        with np.load(artifact, allow_pickle=False) as data:
            np.testing.assert_array_equal(data["valid_mask"], [[True, False], [False, True]])
            assert np.isnan(data["depth_m"][~data["valid_mask"]]).all()
            np.testing.assert_array_equal(data["depth_m"][data["valid_mask"]], [1.0, 2.0])
            np.testing.assert_allclose(data["optical_to_local_ned"][:3, 3], [20.0, 10.12, -0.02])
            np.testing.assert_array_equal(data["intrinsics"], record["calibration"]["output_intrinsics"])
            assert int(data["simulation_time_ns"]) == record["simulation_time_ns"]


@pytest.mark.parametrize("defect", ["duplicate_time", "unsynchronized_pose", "wrong_link_pose", "intrinsics", "path_traversal", "changed_raw", "authority"])
def test_unbound_capture_is_rejected_before_any_registered_artifacts(synthetic_capture, defect):
    capture, capture_path, _, _ = synthetic_capture
    frame = capture["frames"][0]
    if defect == "duplicate_time":
        capture["frames"][1]["simulation_time_ns"] = frame["simulation_time_ns"]
    elif defect == "unsynchronized_pose":
        frame["source_simulation_timestamps_ns"]["pose"] += 1
    elif defect == "wrong_link_pose":
        frame["reported_camera_link_pose"]["position"]["y"] = 0.5
    elif defect == "intrinsics":
        info = frame["sensors"]["rgb"]["camera_info"]
        info["intrinsics"]["k"][0] = 1.2
        info["projection"]["p"][0] = 1.2
    elif defect == "path_traversal":
        frame["sensors"]["rgb"]["raw_file"] = "../outside-private-fixture.bin"
    elif defect == "changed_raw":
        (capture_path.parent / frame["sensors"]["rgb"]["raw_file"]).write_bytes(b"changed")
    elif defect == "authority":
        capture["flight_command_sent"] = True
    with pytest.raises(ValueError):
        register_fixture(synthetic_capture)
    assert not (capture_path.parent / "registered").exists()
