#!/usr/bin/env python3
"""Smoke repo-local Digital Twin vehicle envelope and energy budget evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.runtime.digital_twin_mission_environment import (
    DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    build_digital_twin_stage1_environment,
)


def main() -> int:
    result = build_digital_twin_stage1_environment(
        prompt="10km先の3000mの山小屋に水3kgを届ける",
        prompt_request_ref="px4_gazebo_mission_prompt_request:vehicle_envelope_smoke",
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
        source_backed_target_latitude=35.3606,
        source_backed_target_longitude=138.7274,
        source_backed_dem_fetcher=lambda _url: ("http_200", "2820,2821\n2822,2823"),
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
    summary = result["summary"]
    observed = {
        "vehicle_flight_envelope_ref": summary["vehicle_flight_envelope_ref"],
        "vehicle_profile_ref": summary["vehicle_profile_ref"],
        "vehicle_envelope_status": summary["vehicle_envelope_status"],
        "vehicle_envelope_blocked_reasons": summary["vehicle_envelope_blocked_reasons"],
        "mission_energy_budget_ref": summary["mission_energy_budget_ref"],
        "mission_energy_budget_status": summary["mission_energy_budget_status"],
        "mission_energy_required_wh": summary["mission_energy_required_wh"],
        "mission_energy_remaining_wh": summary["mission_energy_remaining_wh"],
        "mission_energy_blocked_reasons": summary["mission_energy_blocked_reasons"],
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    print(json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True))
    if observed["vehicle_envelope_status"] != "passed":
        return 1
    if observed["mission_energy_budget_status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
