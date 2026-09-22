"""CPU geometry for frozen PX4 x500_depth/OakD-Lite captures, without authority.

Gazebo SceneBroadcaster publishes a model pose in world coordinates and its link
poses relative to the model. A camera's SDF pose is relative to that link; the
inertial pose is not a sensor pose. Gazebo cameras look along link +X (FLU).
We expose optical right/down/forward poses in a local NED world obtained from
Gazebo ENU by (x, y, z) -> (y, x, -z). This is not a geodetic location claim.

Gazebo Ogre2DepthCamera publishes the point-cloud X component, i.e. optical Z,
not radial range. Registration forward-projects measured points and keeps the
nearest Z per target pixel. It does not interpolate, fill holes, or infer depth.

Primary implementation references (Gazebo 8): SceneBroadcaster.cc PoseUpdate;
Ogre2DepthCamera.cc PostRender; depth_camera_fs.glsl, all in gazebo-sim sources.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

SCHEMA = "missionos_registered_aerial_capture.v1"
ALGORITHM = "pinhole_forward_projection_nearest_pixel_z_buffer.v1"
LOCAL_NED_FROM_ENU = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])
FLU_FROM_OPTICAL = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _rigid(value: Any) -> NDArray[np.float64]:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError("expected a finite 4x4 rigid transform")
    rotation = matrix[:3, :3]
    if (
        not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-9, rtol=0)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-7, rtol=0)
        or not np.isclose(np.linalg.det(rotation), 1, atol=1e-7, rtol=0)
    ):
        raise ValueError("invalid rigid transform")
    return matrix


def _intrinsics(value: Any) -> NDArray[np.float64]:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("expected finite 3x3 pinhole intrinsics")
    if (
        matrix[0, 0] <= 0
        or matrix[1, 1] <= 0
        or not np.array_equal(matrix[[0, 1], [1, 0]], [0, 0])
        or not np.array_equal(matrix[2], [0, 0, 1])
    ):
        raise ValueError("unsupported pinhole intrinsics")
    return matrix


def pose_matrix(pose: dict[str, Any]) -> NDArray[np.float64]:
    """Decode Gazebo Pose JSON (protobuf omits components that equal zero)."""
    position, orientation = pose["position"], pose["orientation"]
    xyz = np.array([position.get(key, 0.0) for key in "xyz"], dtype=np.float64)
    q = np.array([orientation.get(key, 0.0) for key in "xyzw"], dtype=np.float64)
    if not np.isfinite(xyz).all() or not np.isfinite(q).all():
        raise ValueError("nonfinite pose")
    if not np.isclose(q @ q, 1.0, atol=1e-7, rtol=0):
        raise ValueError("pose quaternion must be unit length")
    x, y, z, w = q
    matrix = np.eye(4)
    matrix[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    matrix[:3, 3] = xyz
    return _rigid(matrix)


def _sdf_pose(element: ET.Element | None) -> NDArray[np.float64]:
    if element is None:
        return np.eye(4)
    if element.attrib:
        raise ValueError("relative or non-Euler SDF poses require explicit frame resolution")
    values = np.asarray([float(v) for v in (element.text or "").split()])
    if values.shape != (6,) or not np.isfinite(values).all():
        raise ValueError("invalid SDF pose")
    roll, pitch, yaw = values[3:]
    cr, cp, cy = np.cos([roll, pitch, yaw])
    sr, sp, sy = np.sin([roll, pitch, yaw])
    result = np.eye(4)
    result[:3, :3] = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]
    result[:3, 3] = values[:3]
    return _rigid(result)


@dataclass(frozen=True)
class CameraGeometry:
    model_from_link: NDArray[np.float64]
    link_from_sensor: dict[str, NDArray[np.float64]]
    image_sizes: dict[str, tuple[int, int]]
    clip: dict[str, tuple[float, float]]
    horizontal_fov: dict[str, float]


def audit_camera_sdf(airframe: bytes, camera: bytes) -> CameraGeometry:
    """Resolve only the observed fixed, merged x500_depth/OakD-Lite chain."""
    air = ET.fromstring(airframe).find("model")
    cam = ET.fromstring(camera).find("model")
    if (
        air is None
        or cam is None
        or air.get("name") != "x500_depth"
        or cam.get("name") != "OakD-Lite"
    ):
        raise ValueError("unsupported camera model")
    includes = [
        item for item in air.findall("include") if item.findtext("uri") == "model://OakD-Lite"
    ]
    if len(includes) != 1 or includes[0].get("merge") != "true":
        raise ValueError("expected one merged OakD-Lite include")
    if not np.array_equal(_sdf_pose(cam.find("pose")), np.eye(4)):
        raise ValueError("nonidentity included model pose is unsupported")
    link = cam.find("link[@name='camera_link']")
    joint = air.find("joint[@name='CameraJoint']")
    if (
        link is None
        or joint is None
        or joint.get("type") != "fixed"
        or joint.findtext("parent") != "base_link"
        or joint.findtext("child") != "camera_link"
    ):
        raise ValueError("expected fixed camera_link joint")
    mount = _sdf_pose(includes[0].find("pose")) @ _sdf_pose(link.find("pose"))
    # The link pose is independently checked against the observed parent-local
    # pose below. A joint pose locates the joint, not the sensor optical origin.
    sensors, sizes, clips, fovs = {}, {}, {}, {}
    for key, name, kind in (("rgb", "IMX214", "camera"), ("depth", "StereoOV7251", "depth_camera")):
        sensor = link.find(f"sensor[@name='{name}']")
        if sensor is None or sensor.get("type") != kind:
            raise ValueError("unexpected camera sensor")
        if sensor.findtext("gz_frame_id") != "camera_link":
            raise ValueError("unexpected camera frame ID")
        sensors[key] = _sdf_pose(sensor.find("pose"))
        sizes[key] = (
            int(sensor.findtext("camera/image/width", "0")),
            int(sensor.findtext("camera/image/height", "0")),
        )
        clips[key] = (
            float(sensor.findtext("camera/clip/near", "nan")),
            float(sensor.findtext("camera/clip/far", "nan")),
        )
        fovs[key] = float(sensor.findtext("camera/horizontal_fov", "nan"))
        if (
            min(sizes[key]) <= 0
            or not 0 < clips[key][0] < clips[key][1]
            or not np.isfinite(clips[key]).all()
            or not 0 < fovs[key] < np.pi
        ):
            raise ValueError("invalid SDF camera calibration")
    return CameraGeometry(mount, sensors, sizes, clips, fovs)


def optical_pose_local_ned(
    model_pose: dict[str, Any],
    link_pose: dict[str, Any],
    link_from_sensor: Any,
    *,
    expected_model_from_link: Any,
) -> NDArray[np.float64]:
    """Compose model/world, parent-local link, sensor, and optical transforms."""
    model_from_link = pose_matrix(link_pose)
    if not np.allclose(model_from_link, _rigid(expected_model_from_link), atol=1e-6, rtol=0):
        raise ValueError("observed camera link pose disagrees with the SDF mount")
    world_change, optical_change = np.eye(4), np.eye(4)
    world_change[:3, :3] = LOCAL_NED_FROM_ENU
    optical_change[:3, :3] = FLU_FROM_OPTICAL
    return _rigid(
        world_change
        @ pose_matrix(model_pose)
        @ model_from_link
        @ _rigid(link_from_sensor)
        @ optical_change
    )


def register_depth_to_rgb(
    depth: Any,
    depth_K: Any,
    rgb_K: Any,
    rgb_from_depth: Any,
    output_shape: tuple[int, int],
    *,
    near_m: float,
    far_m: float,
) -> tuple[NDArray[np.float32], NDArray[np.bool_], NDArray[np.bool_]]:
    """Return (optical-Z metres, output-valid mask, native source-valid mask).

    Pixel centers use integer coordinates. Nearest-pixel ties round toward the
    positive axis. Minimum optical Z wins collisions. NaN is always unknown.
    The source far limit is radial, as in Ogre2's depth shader; near is axial.
    """
    depth = np.asarray(depth)
    if depth.ndim != 2 or depth.dtype.kind != "f":
        raise ValueError("depth must be a two-dimensional floating point array")
    height, width = output_shape
    if (
        type(height) is not int
        or type(width) is not int
        or min(height, width) <= 0
        or not 0 < near_m < far_m
        or not np.isfinite([near_m, far_m]).all()
    ):
        raise ValueError("invalid output size or clipping limits")
    depth_K, rgb_K = _intrinsics(depth_K), _intrinsics(rgb_K)
    transform = _rigid(rgb_from_depth)
    vv, uu = np.indices(depth.shape, dtype=np.float64)
    rays = np.stack(
        (
            (uu - depth_K[0, 2]) / depth_K[0, 0],
            (vv - depth_K[1, 2]) / depth_K[1, 1],
            np.ones(depth.shape),
        ),
        axis=-1,
    )
    source_valid = np.isfinite(depth) & (depth >= near_m) & (depth <= far_m)
    valid_indices = np.flatnonzero(source_valid)
    points = rays.reshape(-1, 3)[valid_indices] * depth.ravel()[valid_indices, None]
    range_valid = np.linalg.norm(points, axis=1) <= far_m + 1e-5
    source_valid.ravel()[valid_indices[~range_valid]] = False
    points = points[range_valid] @ transform[:3, :3].T + transform[:3, 3]
    points = points[np.isfinite(points).all(axis=1) & (points[:, 2] > 0)]
    projected = points @ rgb_K.T
    uv = projected[:, :2] / projected[:, 2, None]
    inside = (
        np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= -0.5)
        & (uv[:, 0] < width - 0.5)
        & (uv[:, 1] >= -0.5)
        & (uv[:, 1] < height - 0.5)
    )
    pixels = np.floor(uv[inside] + 0.5).astype(np.int64)
    result = np.full(height * width, np.inf, dtype=np.float64)
    np.minimum.at(result, pixels[:, 1] * width + pixels[:, 0], points[inside, 2])
    result = result.reshape(height, width)
    valid = np.isfinite(result)
    result[~valid] = np.nan
    return result.astype(np.float32), valid, source_valid


def _read_bound(directory: Path, filename: str, expected: str) -> bytes:
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in ("", ".", "..")
    ):
        raise ValueError("capture assets must be direct child filenames")
    path = directory / filename
    if path.is_symlink():
        raise ValueError("capture asset symlinks are unsupported")
    data = path.read_bytes()
    if _sha(data) != expected:
        raise ValueError(f"capture asset hash mismatch: {filename}")
    return data


def _camera_K(sensor: dict[str, Any], key: str, geometry: CameraGeometry, stamp: int) -> NDArray:
    info = sensor["camera_info"]
    width, height = geometry.image_sizes[key]
    if (sensor["width"], sensor["height"]) != (width, height) or (
        info["width"],
        info["height"],
    ) != (width, height):
        raise ValueError("SDF, image, and CameraInfo dimensions disagree")
    ts = info["header"]["stamp"]
    if int(ts.get("sec", 0)) * 1_000_000_000 + int(ts.get("nsec", 0)) != stamp:
        raise ValueError("CameraInfo timestamp mismatch")
    frames = [
        v for item in info["header"]["data"] if item["key"] == "frame_id" for v in item["value"]
    ]
    if frames != ["camera_link"]:
        raise ValueError("CameraInfo frame mismatch")
    matrix = _intrinsics(np.asarray(info["intrinsics"]["k"]).reshape(3, 3))
    if (
        not np.array_equal(info["distortion"]["k"], np.zeros(5))
        or not np.array_equal(np.asarray(info["rectification_matrix"]).reshape(3, 3), np.eye(3))
        or not np.allclose(
            np.asarray(info["projection"]["p"]).reshape(3, 4),
            np.column_stack((matrix, np.zeros(3))),
            atol=1e-8,
            rtol=0,
        )
    ):
        raise ValueError("distorted or nonidentity rectified cameras are unsupported")
    focal = width / (2 * np.tan(geometry.horizontal_fov[key] / 2))
    expected = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    if not np.allclose(matrix, expected, rtol=2e-6, atol=1e-5):
        raise ValueError("CameraInfo intrinsics disagree with the SDF pinhole camera")
    return matrix


def register_capture(
    capture_path: str | Path,
    *,
    airframe_sdf: str | Path,
    camera_sdf: str | Path,
    output_dir: str | Path,
    output_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Bind recorded inputs and write RGB/depth/mask/K/pose NPZs plus manifest.

    output_size is (width, height). Optional resizing uses nearest native RGB
    samples and the corresponding half-pixel affine change of intrinsics.
    CameraInfo/pose JSON are recorder-decoded fields; their original wire hashes
    are retained as provenance, not independently decoded transport attestation.
    This operation makes no model-timing, live-freshness, or flight claim.
    """
    capture_path, output_dir = Path(capture_path), Path(output_dir)
    capture_bytes = capture_path.read_bytes()
    capture = json.loads(capture_bytes)
    if (
        capture.get("schema_version") != "missionos_px4_aerial_camera_probe.v1"
        or capture.get("source_kind") != "actual_px4_gazebo_sensor_capture"
    ):
        raise ValueError("unsupported capture schema or source")
    for key in (
        "hardware_target_allowed",
        "arm_command_sent",
        "flight_command_sent",
        "model_inference_invoked",
        "dispatch_authority_created",
        "physical_execution_invoked",
    ):
        if capture.get(key) is not False:
            raise ValueError("capture exceeds the grounded, authority-free scope")
    sdf_bytes = {
        "airframe": Path(airframe_sdf).read_bytes(),
        "camera": Path(camera_sdf).read_bytes(),
    }
    hashes = {key: _sha(value) for key, value in sdf_bytes.items()}
    if hashes != capture["model_sdf_sha256"]:
        raise ValueError("SDF hash binding mismatch")
    geometry = audit_camera_sdf(sdf_bytes["airframe"], sdf_bytes["camera"])
    if output_dir.exists():
        raise ValueError("registration output directory must not already exist")
    stamps = [frame["simulation_time_ns"] for frame in capture["frames"]]
    if (
        not stamps
        or any(type(s) is not int or s < 0 for s in stamps)
        or any(b <= a for a, b in pairwise(stamps))
    ):
        raise ValueError("capture timestamps must be distinct and strictly increasing")
    records, arrays = [], []
    for index, frame in enumerate(capture["frames"]):
        stamp = frame["simulation_time_ns"]
        timestamps = frame["source_simulation_timestamps_ns"]
        if (
            frame["source_timestamp_match"] != "exact"
            or set(timestamps) != {"rgb", "depth", "rgb_info", "depth_info", "pose"}
            or any(type(s) is not int or s != stamp for s in timestamps.values())
        ):
            raise ValueError("source timestamps must match exactly")
        pose_filename = f"frame_{index:02d}-pose.pb"
        _read_bound(capture_path.parent, pose_filename, frame["pose_message_sha256"])
        if (
            frame["vehicle_model_pose"].get("name") != "x500_depth_0"
            or frame["reported_camera_link_pose"].get("name") != "camera_link"
        ):
            raise ValueError("unexpected pose entity")
        sensors = frame["sensors"]
        native_K = {key: _camera_K(sensors[key], key, geometry, stamp) for key in ("rgb", "depth")}
        raw, poses = {}, {}
        for key in ("rgb", "depth"):
            sensor = sensors[key]
            width, height = geometry.image_sizes[key]
            channels = 3 if key == "rgb" else 4
            if (
                sensor["pixel_format"] != ("RGB_INT8" if key == "rgb" else "R_FLOAT32")
                or sensor["step"] != width * channels
            ):
                raise ValueError("unsupported raw image layout")
            raw[key] = _read_bound(capture_path.parent, sensor["raw_file"], sensor["raw_sha256"])
            if len(raw[key]) != width * height * channels:
                raise ValueError("raw image byte count mismatch")
            poses[key] = optical_pose_local_ned(
                frame["vehicle_model_pose"],
                frame["reported_camera_link_pose"],
                geometry.link_from_sensor[key],
                expected_model_from_link=geometry.model_from_link,
            )
        if (
            sensors["depth"].get("depth_units") != "metres"
            or sensors["depth"].get("byte_order") != "little"
        ):
            raise ValueError("unsupported depth units or byte order")
        rgb_width, rgb_height = geometry.image_sizes["rgb"]
        depth_width, depth_height = geometry.image_sizes["depth"]
        rgb = np.frombuffer(raw["rgb"], dtype=np.uint8).reshape(rgb_height, rgb_width, 3)
        depth = np.frombuffer(raw["depth"], dtype="<f4").reshape(depth_height, depth_width)
        width, height = output_size if output_size is not None else (rgb_width, rgb_height)
        if (
            type(width) is not int
            or type(height) is not int
            or not 0 < width <= rgb_width
            or not 0 < height <= rgb_height
        ):
            raise ValueError("output must be a positive native-size or smaller RGB grid")
        scale = np.diag([width / rgb_width, height / rgb_height, 1.0])
        scale[0, 2], scale[1, 2] = (width / rgb_width - 1) / 2, (height / rgb_height - 1) / 2
        output_K = scale @ native_K["rgb"]
        columns = np.floor((np.arange(width) + 0.5) * rgb_width / width).astype(int)
        rows = np.floor((np.arange(height) + 0.5) * rgb_height / height).astype(int)
        output_rgb = rgb[rows[:, None], columns]
        rgb_from_depth = np.linalg.inv(poses["rgb"]) @ poses["depth"]
        registered, valid, source_valid = register_depth_to_rgb(
            depth,
            native_K["depth"],
            output_K,
            rgb_from_depth,
            (height, width),
            near_m=geometry.clip["depth"][0],
            far_m=geometry.clip["depth"][1],
        )
        calibration = {
            "source_intrinsics": {key: value.tolist() for key, value in native_K.items()},
            "output_intrinsics": output_K.tolist(),
            "rgb_from_depth_optical": rgb_from_depth.tolist(),
            "model_from_camera_link": geometry.model_from_link.tolist(),
            "camera_link_from_sensors": {
                key: value.tolist() for key, value in geometry.link_from_sensor.items()
            },
            "output_size": [width, height],
            "source_sdf_sha256": hashes,
        }
        filename = f"frame_{index:02d}-registered.npz"
        records.append(
            {
                "file": filename,
                "simulation_time_ns": stamp,
                "calibration": calibration,
                "calibration_sha256": _sha(_json_bytes(calibration)),
                "source_raw_sha256": {key: sensors[key]["raw_sha256"] for key in ("rgb", "depth")},
                "source_camera_info_message_sha256": {
                    key: sensors[key]["camera_info_message_sha256"] for key in ("rgb", "depth")
                },
                "source_pose_message_sha256": frame["pose_message_sha256"],
                "optical_to_local_ned": poses["rgb"].tolist(),
                "valid_depth_pixels": int(valid.sum()),
                "invalid_depth_pixels": int((~valid).sum()),
                "native_valid_depth_pixels": int(source_valid.sum()),
            }
        )
        arrays.append(
            {
                "rgb": output_rgb,
                "depth_m": registered,
                "valid_mask": valid,
                "source_depth_m": depth,
                "source_valid_mask": source_valid,
                "intrinsics": output_K,
                "optical_to_local_ned": poses["rgb"],
                "simulation_time_ns": np.array(stamp, dtype=np.int64),
            }
        )
    # All source inputs are validated before creating output artifacts.
    output_dir.mkdir(parents=True)
    for record, values in zip(records, arrays):
        path = output_dir / record["file"]
        np.savez_compressed(path, **values)
        record["sha256"] = _sha(path.read_bytes())
    manifest = {
        "schema_version": SCHEMA,
        "source_kind": "px4_gazebo_frozen_capture",
        "capture_sha256": _sha(capture_bytes),
        "algorithm": ALGORITHM,
        "source_sdf_sha256": hashes,
        "world_frame": "local_ned_from_gazebo_enu",
        "camera_frame": "optical_right_down_forward",
        "depth_semantics": "optical_z_metres",
        "invalid_depth_policy": "nan_with_explicit_mask_no_fill",
        "rgb_resize": "nearest_half_pixel",
        "simulation_timing_verified": True,
        "model_time_alignment_verified": False,
        "live_freshness_established": False,
        "frozen_replay_only": True,
        "transport_field_decode_independently_verified": False,
        "model_inference_invoked": False,
        "goal_image_declared": False,
        "model_input_ready": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "frames": records,
    }
    (output_dir / "registration.json").write_bytes(_json_bytes(manifest) + b"\n")
    return manifest
