#!/usr/bin/env python3
"""Opt-in Unitree MuJoCo Python simulator process launch smoke.

This starts then terminates the external Unitree MuJoCo Python simulator only
when explicitly gated. It does not import Unitree SDK2 in MissionOS and does not
send any dispatch request.
"""

from __future__ import annotations

import json
import os

from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_PROCESS_SMOKE_ENV,
    UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV,
    UNITREE_MUJOCO_ROOT_ENV,
    launch_unitree_mujoco_python_simulator,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(UNITREE_MUJOCO_PROCESS_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_mujoco_process_launch",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_MUJOCO_PROCESS_SMOKE_ENV} is not set to 1; "
                        "MuJoCo was not started and no dispatch was sent."
                    ),
                    "process_started": False,
                    "mujoco_started": False,
                    "scene_loaded_observed": False,
                    "robot_motion_observed": False,
                    "unitree_sdk2_imported": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = launch_unitree_mujoco_python_simulator(
        checkout_root=os.environ.get(UNITREE_MUJOCO_ROOT_ENV),
        opt_in=True,
        python_executable=(
            os.environ.get(UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV) or None
        ),
    )
    summary = {
        "smoke": "unitree_mujoco_process_launch",
        "ran": True,
        **result.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.launch_status == "started" else 2


if __name__ == "__main__":
    raise SystemExit(main())
