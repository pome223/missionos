#!/usr/bin/env python3
"""Smoke source-backed Digital Twin weather through Open-Meteo JMA."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
)


def main() -> int:
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    result = build_digital_twin_stage1_environment(
        prompt="10km先の3000mの山小屋に水3kgを届ける",
        prompt_request_ref=(
            "px4_gazebo_mission_prompt_request:"
            "source_backed_weather_runtime_smoke"
        ),
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=now,
        source_backed_target_latitude=35.3606,
        source_backed_target_longitude=138.7274,
        source_backed_dem_fetcher=lambda _url: ("http_200", "2820,2821\n2822,2823"),
        use_source_backed_weather=True,
    )
    summary = result["summary"]
    observed = {
        "source_backed_target": summary["source_backed_target"],
        "source_backed_weather": summary["source_backed_weather"],
        "source_weather_unavailable": summary["source_weather_unavailable"],
        "weather_source_snapshot_ref": summary["weather_source_snapshot_ref"],
        "weather_source_snapshot_status": summary["weather_source_snapshot_status"],
        "weather_source_snapshot_provider": summary["weather_source_snapshot_provider"],
        "weather_source_snapshot_source_url": summary["weather_source_snapshot_source_url"],
        "weather_source_snapshot_provider_response_status": (
            summary["weather_source_snapshot_provider_response_status"]
        ),
        "weather_precipitation_mm_per_hour": (
            summary["weather_precipitation_mm_per_hour"]
        ),
        "weather_wind_speed_mps": summary["weather_wind_speed_mps"],
        "weather_external_weather_required": (
            summary["weather_external_weather_required"]
        ),
        "weather_external_weather_observed": (
            summary["weather_external_weather_observed"]
        ),
        "weather_policy_gate_status": summary["weather_policy_gate_status"],
        "weather_policy_blocked_reasons": summary["weather_policy_blocked_reasons"],
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    print(json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True))
    if observed["source_weather_unavailable"]:
        print("source-backed weather provider unavailable", file=sys.stderr)
        return 2
    if not observed["source_backed_weather"]:
        print("source-backed weather was not generated", file=sys.stderr)
        return 1
    if not observed["weather_external_weather_observed"]:
        print("external weather was not observed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
