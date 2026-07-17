"""Pure-function tests for camera frame capture image conversion (issue #31).

_image_message_to_rgb_array has no ROS2 dependency itself (rclpy is only
imported lazily inside the subscription-handling functions), so it can be
exercised directly against fake sensor_msgs/Image-shaped objects without a
ROS2 environment. The gate-controlled and dependency-missing subprocess
paths are covered in test_ros2_nav2_dispatch_bridge.py.
"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.ros2_nav2_turtlebot4_bridge import (
    _image_message_to_rgb_array,
    _write_camera_frame_png,
)


def _fake_image(*, width: int, height: int, encoding: str, data: bytes):
    return SimpleNamespace(width=width, height=height, encoding=encoding, data=data)


def test_rgb8_passes_through_unchanged() -> None:
    pixel = bytes([10, 20, 30])
    message = _fake_image(width=1, height=1, encoding="rgb8", data=pixel)
    array = _image_message_to_rgb_array(message)
    assert array.shape == (1, 1, 3)
    assert list(array[0, 0]) == [10, 20, 30]


def test_bgr8_channels_are_swapped_to_rgb() -> None:
    pixel = bytes([30, 20, 10])  # B, G, R
    message = _fake_image(width=1, height=1, encoding="bgr8", data=pixel)
    array = _image_message_to_rgb_array(message)
    assert list(array[0, 0]) == [10, 20, 30]  # R, G, B


def test_rgba8_drops_alpha_channel() -> None:
    pixel = bytes([10, 20, 30, 255])
    message = _fake_image(width=1, height=1, encoding="rgba8", data=pixel)
    array = _image_message_to_rgb_array(message)
    assert array.shape == (1, 1, 3)
    assert list(array[0, 0]) == [10, 20, 30]


def test_bgra8_swaps_channels_and_drops_alpha() -> None:
    pixel = bytes([30, 20, 10, 255])  # B, G, R, A
    message = _fake_image(width=1, height=1, encoding="bgra8", data=pixel)
    array = _image_message_to_rgb_array(message)
    assert list(array[0, 0]) == [10, 20, 30]


def test_mono8_is_broadcast_to_three_channels() -> None:
    pixel = bytes([128])
    message = _fake_image(width=1, height=1, encoding="mono8", data=pixel)
    array = _image_message_to_rgb_array(message)
    assert array.shape == (1, 1, 3)
    assert list(array[0, 0]) == [128, 128, 128]


def test_multi_pixel_frame_reshapes_correctly() -> None:
    width, height = 2, 2
    data = bytes(range(width * height * 3))
    message = _fake_image(width=width, height=height, encoding="rgb8", data=data)
    array = _image_message_to_rgb_array(message)
    assert array.shape == (height, width, 3)
    assert list(array[0, 0]) == [0, 1, 2]
    assert list(array[1, 1]) == [9, 10, 11]


def test_unsupported_encoding_raises_value_error() -> None:
    message = _fake_image(width=1, height=1, encoding="yuv422", data=b"\x00\x00")
    with pytest.raises(ValueError, match="unsupported camera image encoding"):
        _image_message_to_rgb_array(message)


def test_write_camera_frame_png_round_trips_and_hashes(tmp_path) -> None:
    width, height = 4, 3
    array = np.arange(width * height * 3, dtype=np.uint8).reshape((height, width, 3))
    output_path = tmp_path / "nested" / "frame.png"

    frame_bytes = _write_camera_frame_png(array, output_path)

    assert output_path.exists()
    assert frame_bytes == output_path.read_bytes()
    assert frame_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert sha256(frame_bytes).hexdigest() == sha256(output_path.read_bytes()).hexdigest()

    from PIL import Image as PILImage

    reopened = PILImage.open(output_path)
    assert reopened.size == (width, height)
    assert np.array(reopened.convert("RGB")).tolist() == array.tolist()


def test_captured_frame_feeds_perception_sidecar_command_override(
    tmp_path, monkeypatch
) -> None:
    """End-to-end: a captured frame's path/hash feed the VLM sidecar exactly.

    Confirms the produced file is a valid input to
    turtlebot3_perception_sidecar.py without needing a real ROS2/Gazebo
    camera — the missing piece was only the file, and this proves the file
    this bridge writes is consumable end to end.
    """

    from src.intelligence.turtlebot3_perception_sidecar import (
        TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV,
        TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
        run_turtlebot3_perception_sidecar,
    )

    array = np.full((2, 2, 3), 200, dtype=np.uint8)
    output_path = tmp_path / "frame.png"
    frame_bytes = _write_camera_frame_png(array, output_path)
    expected_ref = f"sha256:{sha256(frame_bytes).hexdigest()}"

    script = tmp_path / "fake_sidecar.py"
    script.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'claim_kind': 'path_clear', 'confidence': 0.6}))\n",
        encoding="utf-8",
    )
    import shlex
    import sys as _sys

    monkeypatch.setenv(
        TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
        f"{shlex.quote(_sys.executable)} {shlex.quote(str(script))}",
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")

    result = run_turtlebot3_perception_sidecar(image_path=output_path)

    assert result["sidecar_status"] == "classified"
    assert result["camera_observation"]["source_frame_ref"] == expected_ref


def test_camera_topic_resolution_treats_blank_env_as_unset(monkeypatch) -> None:
    """Regression: a live Gazebo/Nav2 run crashed the bridge because the
    docker launch script's -e VAR=${VAR:-} pattern set the topic env to an
    empty string, which os.environ.get-with-default passed through to rclpy
    as an invalid empty topic name."""

    from scripts.ros2_nav2_turtlebot4_bridge import (
        ROS2_NAV2_CAMERA_TOPIC_ENV,
        _camera_topic_from,
    )

    monkeypatch.setenv(ROS2_NAV2_CAMERA_TOPIC_ENV, "")
    assert _camera_topic_from({}) == "/camera/image_raw"
    assert _camera_topic_from({"camera_topic": ""}) == "/camera/image_raw"
    assert _camera_topic_from({"camera_topic": "   "}) == "/camera/image_raw"

    monkeypatch.setenv(ROS2_NAV2_CAMERA_TOPIC_ENV, "/custom/image")
    assert _camera_topic_from({}) == "/custom/image"
    assert _camera_topic_from({"camera_topic": "/payload/image"}) == (
        "/payload/image"
    )

    monkeypatch.delenv(ROS2_NAV2_CAMERA_TOPIC_ENV, raising=False)
    assert _camera_topic_from({}) == "/camera/image_raw"
