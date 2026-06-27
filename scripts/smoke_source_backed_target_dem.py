#!/usr/bin/env python3
"""Smoke a source-backed Digital Twin target resolution and GSI DEM fetch."""

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
            "source_backed_target_dem_runtime_smoke"
        ),
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=now,
        source_backed_target_latitude=35.3606,
        source_backed_target_longitude=138.7274,
    )
    summary = result["summary"]
    observed = {
        "source_backed_target": summary["source_backed_target"],
        "source_backed_terrain": summary["source_backed_terrain"],
        "source_unavailable": summary["source_unavailable"],
        "target_resolution_ref": summary["real_world_target_resolution_ref"],
        "terrain_dem_source_snapshot_ref": (
            summary["terrain_dem_source_snapshot_ref"]
        ),
        "terrain_dem_source_snapshot_status": (
            summary["terrain_dem_source_snapshot_status"]
        ),
        "terrain_dem_source_snapshot_provider": (
            summary["terrain_dem_source_snapshot_provider"]
        ),
        "terrain_dem_source_snapshot_source_url": (
            summary["terrain_dem_source_snapshot_source_url"]
        ),
        "terrain_dem_source_snapshot_provider_response_status": (
            summary["terrain_dem_source_snapshot_provider_response_status"]
        ),
        "dem_tile_request_status": summary["dem_tile_request_status"],
        "dem_tile_snapshot_mode": summary["dem_tile_snapshot_mode"],
        "tile_backed_terrain_snapshot_mode": (
            summary["tile_backed_terrain_snapshot_mode"]
        ),
        "heightmap_candidate_ref": summary["terrain_heightmap_candidate_ref"],
        "gazebo_world_artifact_ref": summary["gazebo_world_artifact_ref"],
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    print(json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True))
    if not observed["source_backed_target"]:
        print("source-backed target was not generated", file=sys.stderr)
        return 1
    if observed["source_unavailable"]:
        print("source-backed DEM provider unavailable", file=sys.stderr)
        return 2
    if not observed["source_backed_terrain"]:
        print("source-backed terrain was not generated", file=sys.stderr)
        return 1
    if not observed["heightmap_candidate_ref"] or not observed["gazebo_world_artifact_ref"]:
        print("source-backed DEM did not feed heightmap/world generation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
