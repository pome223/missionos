from __future__ import annotations

import subprocess
import sys


def test_runtime_package_does_not_eagerly_import_unrelated_backends() -> None:
    script = """
import json
import sys
import src.runtime
print(json.dumps(sorted(name for name in sys.modules if name.startswith('src.runtime.'))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def test_runtime_submodule_compatibility_import_still_works() -> None:
    script = """
from src.runtime import ros2_nav2_dispatch_bridge
print(ros2_nav2_dispatch_bridge.ROS2_NAV2_BRIDGE_COMMAND_ENV)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ROS2_NAV2_BRIDGE_COMMAND"
