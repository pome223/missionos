#!/usr/bin/env python3
"""Opt-in Unitree SDK2 Python import readiness smoke.

Gate off: skip without importing Unitree SDK2.
Gate on: import only the expected Python modules; do not start MuJoCo or send
commands.
"""

from __future__ import annotations

import json
import os

from src.runtime.unitree_mujoco_environment import (
    UNITREE_SDK2_IMPORT_SMOKE_ENV,
    UNITREE_SDK2_PYTHON_EXECUTABLE_ENV,
    build_unitree_sdk2_import_readiness,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(UNITREE_SDK2_IMPORT_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_sdk2_import_readiness",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_SDK2_IMPORT_SMOKE_ENV} is not set to 1; "
                        "Unitree SDK2 was not imported, MuJoCo was not started, "
                        "and no dispatch was sent."
                    ),
                    "import_attempted": False,
                    "unitree_sdk2_imported": False,
                    "mujoco_started": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    readiness = build_unitree_sdk2_import_readiness(
        opt_in=True,
        python_executable=(
            os.environ.get(UNITREE_SDK2_PYTHON_EXECUTABLE_ENV) or None
        ),
    )
    summary = {
        "smoke": "unitree_sdk2_import_readiness",
        "ran": True,
        "unitree_sdk2_imported": readiness.readiness_status == "ready",
        **readiness.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if readiness.readiness_status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
