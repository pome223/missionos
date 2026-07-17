from datetime import datetime, timezone
import json
from typing import Any

from src.runtime.digital_twin_mission_environment import (
    DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    build_digital_twin_stage1_environment,
)


NOW = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)


def build_fixture_backed_vehicle_environment() -> dict[str, Any]:
    """Build source-shaped vehicle evidence without any external I/O."""

    return build_digital_twin_stage1_environment(
        prompt="10km先の3000mの山小屋に水3kgを届ける",
        prompt_request_ref="px4_gazebo_mission_prompt_request:vehicle_fixture",
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=NOW,
        source_backed_target_latitude=35.3606,
        source_backed_target_longitude=138.7274,
        source_backed_dem_fetcher=lambda _url: (
            "http_200",
            "2820,2821\n2822,2823",
        ),
        use_source_backed_weather=True,
        source_backed_weather_fetcher=lambda _url: (
            "http_200",
            json.dumps(
                {
                    "current": {
                        "time": "2026-05-08T03:00",
                        "temperature_2m": 12,
                        "precipitation": 0,
                        "wind_speed_10m": 7.2,
                        "wind_direction_10m": 245,
                        "wind_gusts_10m": 18,
                        "surface_pressure": 900,
                    }
                }
            ),
        ),
        vehicle_profile_path=DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    )
