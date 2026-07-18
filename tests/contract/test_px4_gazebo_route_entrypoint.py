from __future__ import annotations

import importlib
import sys

from src.runtime import missionos_sitl_dispatch_runtime
from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run


RUNTIME_MODULE = "src.runtime.px4_gazebo_route.entrypoint"
LEGACY_SMOKE_MODULE = "scripts.smoke_px4_gazebo_horizontal_route_delivery"


def test_legacy_smoke_module_is_runtime_entrypoint_alias() -> None:
    runtime = importlib.import_module(RUNTIME_MODULE)
    legacy = importlib.import_module(LEGACY_SMOKE_MODULE)

    assert legacy is runtime
    assert legacy.main is runtime.main
    assert legacy.OPT_IN_ENV == "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"


def test_missionos_dispatch_defaults_to_formal_runtime_module() -> None:
    assert missionos_sitl_dispatch_runtime._default_command() == [
        sys.executable,
        "-m",
        RUNTIME_MODULE,
    ]


def test_live_flight_defaults_to_formal_runtime_module() -> None:
    assert (
        px4_gazebo_mission_designer_sitl_live_flight_run._horizontal_route_runtime_command()
        == [sys.executable, "-m", RUNTIME_MODULE]
    )
