"""Numerical geometry and malformed-input checks, using synthetic measurements."""

import math

import numpy as np
import pytest

from src.prediction.px4_camera import (
    audit_camera_sdf,
    optical_pose_local_ned,
    pose_matrix,
    register_depth_to_rgb,
)


def pose(x=0.0, y=0.0, z=0.0, yaw=0.0):
    return {
        "position": {"x": x, "y": y, "z": z},
        "orientation": {"z": math.sin(yaw / 2), "w": math.cos(yaw / 2)},
    }


def test_identity_registration_preserves_depth_without_filling_holes():
    depth = np.array([[1.0, np.nan, np.inf], [0.0, -2.0, 3.0]], dtype=np.float32)
    result, valid, source_valid = register_depth_to_rgb(
        depth, np.eye(3), np.eye(3), np.eye(4), depth.shape, near_m=0.1, far_m=20.0
    )
    expected = np.array([[True, False, False], [False, False, True]])
    np.testing.assert_array_equal(valid, expected)
    np.testing.assert_array_equal(source_valid, expected)
    np.testing.assert_array_equal(result[valid], depth[valid])
    assert np.isnan(result[~valid]).all()
    assert np.isnan(depth[0, 1]) and np.isposinf(depth[0, 2])
    assert depth[1, 1] == -2  # The source remains unchanged.


def test_z_buffer_keeps_nearest_measured_surface():
    depth = np.array([[2.0, 1.0]], dtype=np.float32)
    small_K = np.diag([0.1, 0.1, 1.0])
    result, valid, _ = register_depth_to_rgb(
        depth, np.eye(3), small_K, np.eye(4), (1, 2), near_m=0.1, far_m=20.0
    )
    np.testing.assert_array_equal(valid, [[True, False]])
    assert result[0, 0] == 1.0
    assert np.isnan(result[0, 1])


def test_translation_changes_optical_z_not_radial_range():
    transform = np.eye(4)
    transform[:3, 3] = [0.0, 0.0, -1.0]
    # A ray away from the optical axis has range > Z, but Z remains 2-1=1.
    depth = np.array([[np.nan, 2.0]], dtype=np.float32)
    result, valid, _ = register_depth_to_rgb(
        depth, np.eye(3), np.eye(3), transform, (1, 3), near_m=0.1, far_m=20.0
    )
    np.testing.assert_array_equal(valid, [[False, False, True]])
    assert result[0, 2] == 1.0


def test_reprojection_rejects_points_behind_target_and_outside_image():
    transform = np.eye(4)
    transform[:3, 3] = [100.0, 0.0, -2.0]
    result, valid, source_valid = register_depth_to_rgb(
        np.array([[1.0, 3.0]], dtype=np.float32),
        np.eye(3),
        np.eye(3),
        transform,
        (1, 2),
        near_m=0.1,
        far_m=20.0,
    )
    assert source_valid.all()
    assert not valid.any()
    assert np.isnan(result).all()


def test_clip_near_is_axial_and_far_is_radial():
    depth = np.array([[0.05, 3.0, 3.0]], dtype=np.float32)
    _, _, valid = register_depth_to_rgb(
        depth, np.eye(3), np.eye(3), np.eye(4), (1, 3), near_m=0.1, far_m=5.0
    )
    # x=1,z=3 => range sqrt(18)<5; x=2,z=3 => range sqrt(45)>5.
    np.testing.assert_array_equal(valid, [[False, True, False]])


def test_sparse_native_reprojection_does_not_invent_dense_depth():
    target_K = np.diag([3.0, 3.0, 1.0])
    result, valid, _ = register_depth_to_rgb(
        np.ones((2, 2), dtype=np.float32),
        np.eye(3),
        target_K,
        np.eye(4),
        (4, 4),
        near_m=0.1,
        far_m=20.0,
    )
    assert valid.sum() == 4
    assert np.isnan(result).sum() == 12


@pytest.mark.parametrize(
    "matrix",
    [
        np.diag([-1.0, 1.0, 1.0]),
        np.ones((3, 3)),
        np.array([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
    ],
)
def test_unsupported_intrinsics_fail(matrix):
    with pytest.raises(ValueError, match="intrinsics"):
        register_depth_to_rgb(
            np.ones((1, 1)), matrix, np.eye(3), np.eye(4), (1, 1), near_m=0.1, far_m=20.0
        )


def test_reflection_is_not_an_admissible_rigid_transform():
    reflection = np.diag([-1.0, 1.0, 1.0, 1.0])
    with pytest.raises(ValueError, match="rigid"):
        register_depth_to_rgb(
            np.ones((1, 1)), np.eye(3), np.eye(3), reflection, (1, 1), near_m=0.1, far_m=20.0
        )


def test_optical_right_down_forward_axes_in_local_ned():
    result = optical_pose_local_ned(pose(), pose(), np.eye(4), expected_model_from_link=np.eye(4))
    np.testing.assert_array_equal(
        result[:3, :3], [[-1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]]
    )
    assert np.linalg.det(result[:3, :3]) == 1.0


def test_camera_origin_composes_link_and_sensor_with_rotated_model():
    link = pose(0.12, 0.03, 0.242)
    sensor = pose_matrix(pose(0.01233, -0.03, 0.01878))
    result = optical_pose_local_ned(
        pose(10.0, 20.0, 30.0, math.pi / 2),
        link,
        sensor,
        expected_model_from_link=pose_matrix(link),
    )
    np.testing.assert_allclose(result[:3, 3], [20.13233, 10.0, -30.26078], atol=1e-12)
    np.testing.assert_allclose(result[:3, 2], [1.0, 0.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(result[:3, 0], [0.0, 1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(result[:3, 1], [0.0, 0.0, 1.0], atol=1e-12)


def test_world_link_pose_cannot_be_mistaken_for_parent_local_pose():
    with pytest.raises(ValueError, match="disagrees"):
        optical_pose_local_ned(
            pose(10.0, 20.0, 30.0),
            pose(10.12, 20.03, 30.242),
            np.eye(4),
            expected_model_from_link=pose_matrix(pose(0.12, 0.03, 0.242)),
        )


@pytest.mark.parametrize("bad_q", [{"w": 2.0}, {"w": float("nan")}, {}])
def test_invalid_quaternion_rejected(bad_q):
    with pytest.raises(ValueError):
        pose_matrix({"position": {}, "orientation": bad_q})


def make_sdf():
    airframe = b"""<sdf><model name='x500_depth'>
      <include merge='true'><uri>model://OakD-Lite</uri><pose>.12 .03 .242 0 0 0</pose></include>
      <joint name='CameraJoint' type='fixed'><parent>base_link</parent><child>camera_link</child></joint>
    </model></sdf>"""
    sensors = ""
    for name, kind in (("IMX214", "camera"), ("StereoOV7251", "depth_camera")):
        sensors += f"""<sensor name='{name}' type='{kind}'><gz_frame_id>camera_link</gz_frame_id>
          <pose>.01233 -.03 .01878 0 0 0</pose><camera><horizontal_fov>1.2</horizontal_fov>
          <image><width>640</width><height>480</height></image>
          <clip><near>.2</near><far>19.1</far></clip></camera></sensor>"""
    # Deliberately huge inertial COM: it must never become the camera transform.
    camera = (
        "<sdf><model name='OakD-Lite'><pose>0 0 0 0 0 0</pose><link name='camera_link'>"
        "<inertial><pose>50 60 70 0 0 0</pose></inertial>" + sensors + "</link></model></sdf>"
    ).encode()
    return airframe, camera


def test_sdf_link_sensor_and_inertial_frames_are_separate():
    result = audit_camera_sdf(*make_sdf())
    np.testing.assert_allclose(result.model_from_link[:3, 3], [0.12, 0.03, 0.242])
    np.testing.assert_allclose(result.link_from_sensor["rgb"][:3, 3], [0.01233, -0.03, 0.01878])
    np.testing.assert_array_equal(result.link_from_sensor["rgb"], result.link_from_sensor["depth"])
    assert result.image_sizes["rgb"] == (640, 480)


def test_unresolved_sdf_relative_frame_fails_closed():
    airframe, camera = make_sdf()
    camera = camera.replace(b"<pose>.01233", b"<pose relative_to='unresolved'>.01233")
    with pytest.raises(ValueError, match="frame resolution"):
        audit_camera_sdf(airframe, camera)


def test_moving_camera_mount_fails_closed():
    airframe, camera = make_sdf()
    with pytest.raises(ValueError, match="fixed"):
        audit_camera_sdf(airframe.replace(b"type='fixed'", b"type='revolute'"), camera)
