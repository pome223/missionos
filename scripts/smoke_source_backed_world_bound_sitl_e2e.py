#!/usr/bin/env python3
"""Opt-in source-backed Digital Twin world-bound SITL E2E smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from scripts import smoke_digital_twin_px4_mission_upload as upload_smoke
from scripts import smoke_digital_twin_world_bound_sitl_e2e as fixture_e2e
from scripts import smoke_px4_gazebo_sitl_mission_upload as px4_upload_smoke
from src.runtime.digital_twin_mission_environment import (
    DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    build_digital_twin_stage1_environment,
)
from src.runtime.digital_twin_sitl_execution_result import (
    DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION,
    build_digital_twin_sitl_execution_result,
    digital_twin_sitl_execution_result_ref,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
    build_digital_twin_sitl_mission_upload_receipt,
    digital_twin_candidate_upload_items,
)
from src.runtime.digital_twin_sitl_process_runner import (
    build_digital_twin_sitl_process_run_from_observed_container,
    digital_twin_sitl_process_run_ref,
)


OPT_IN_ENV = "RUN_SOURCE_BACKED_DIGITAL_TWIN_WORLD_BOUND_SITL_E2E_SMOKE"
CONTAINER_NAME = "boiled-claw-source-backed-digital-twin-e2e"
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/digital_twin/source_backed_world_bound_sitl_e2e"
PROMPT = "10km先の3000mの山小屋に水3kgを届ける"
PROMPT_REF = (
    "px4_gazebo_mission_prompt_request:source_backed_digital_twin_world_bound_e2e"
)
NOW = datetime(2026, 5, 8, 2, 0, tzinfo=timezone.utc)
SOURCE_BACKED_TARGET_LATITUDE = 35.35
SOURCE_BACKED_TARGET_LONGITUDE = 138.7274


class _SourceBackedWorldBoundUploadShim:
    CONTAINER_NAME = CONTAINER_NAME
    PX4_MAVLINK_PORT = px4_upload_smoke.PX4_MAVLINK_PORT
    GCS_MAVLINK_PORT = px4_upload_smoke.GCS_MAVLINK_PORT
    _mission_upload_item_tuples = staticmethod(px4_upload_smoke._mission_upload_item_tuples)

    @staticmethod
    def _run(
        command: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT_DIR,
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )


class _ObservedSourceBackedUploader:
    def __init__(self, observed: dict[str, Any]):
        self.observed = observed
        self.heartbeat_observed = bool(observed.get("heartbeat_observed"))

    def upload(self, *, items, target_endpoint, timeout_seconds):
        return (
            tuple(int(item) for item in self.observed["mission_request_sequences"]),
            int(self.observed["mission_ack_type"]),
        )


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run source-backed Digital Twin SITL E2E."
        )


def _source_backed_inputs_summary(digital_twin: dict[str, Any]) -> dict[str, Any]:
    target = digital_twin.get("real_world_target_resolution") or {}
    dem = digital_twin.get("terrain_dem_source_snapshot") or {}
    weather = digital_twin.get("weather_source_snapshot") or {}
    vehicle = digital_twin.get("vehicle_flight_envelope") or {}
    budget = digital_twin.get("mission_energy_budget") or {}
    return {
        "source_backed_target": bool(target.get("source_backed_target")),
        "source_backed_terrain": bool(dem.get("source_backed_terrain")),
        "source_backed_weather": bool(weather.get("source_backed_weather")),
        "vehicle_envelope_present": bool(vehicle),
        "mission_energy_budget_present": bool(budget),
        "real_world_target_resolution_ref": (
            f"real_world_target_resolution:{target.get('resolution_id', '')}"
        ),
        "terrain_dem_source_snapshot_ref": (
            f"terrain_dem_source_snapshot:{dem.get('snapshot_id', '')}"
        ),
        "weather_source_snapshot_ref": (
            f"weather_source_snapshot:{weather.get('snapshot_id', '')}"
        ),
        "vehicle_flight_envelope_ref": (
            f"vehicle_flight_envelope:{vehicle.get('envelope_id', '')}"
        ),
        "mission_energy_budget_ref": (
            f"mission_energy_budget:{budget.get('budget_id', '')}"
        ),
        "terrain_provider_response_status": dem.get("provider_response_status", ""),
        "weather_provider_response_status": weather.get(
            "provider_response_status",
            "",
        ),
    }


def _assert_source_backed_ready(
    digital_twin: dict[str, Any],
    source_summary: dict[str, Any],
) -> None:
    if not all(
        source_summary[key] is True
        for key in (
            "source_backed_target",
            "source_backed_terrain",
            "source_backed_weather",
            "vehicle_envelope_present",
            "mission_energy_budget_present",
        )
    ):
        raise RuntimeError(f"source-backed inputs were incomplete: {source_summary}")
    mission_item = digital_twin["digital_twin_px4_mission_item_candidate"]
    if mission_item["terrain_sampling_mode"] != "anchor_point_sampled":
        raise RuntimeError(
            "source-backed mission item candidate did not sample anchor terrain"
        )
    if mission_item["candidate_status"] != "candidate_generated_for_planning_only":
        raise RuntimeError(
            "source-backed mission item candidate was not generated: "
            + mission_item["candidate_status"]
        )
    binding_gate = digital_twin["digital_twin_sitl_binding_gate"]
    if binding_gate["binding_gate_status"] != (
        "eligible_for_operator_approved_sitl_binding"
    ):
        raise RuntimeError(
            "source-backed binding gate was not eligible: "
            + binding_gate["binding_gate_status"]
        )


def run_smoke() -> dict[str, Any]:
    _require_opt_in()
    fixture_e2e.CONTAINER_NAME = CONTAINER_NAME
    run_dir = RUN_ROOT / NOW.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    digital_twin = build_digital_twin_stage1_environment(
        prompt=PROMPT,
        prompt_request_ref=PROMPT_REF,
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        source_backed_target_latitude=SOURCE_BACKED_TARGET_LATITUDE,
        source_backed_target_longitude=SOURCE_BACKED_TARGET_LONGITUDE,
        use_source_backed_weather=True,
        vehicle_profile_path=DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
        now=NOW,
    )
    source_summary = _source_backed_inputs_summary(digital_twin)
    _assert_source_backed_ready(digital_twin, source_summary)
    world_artifact = digital_twin["gazebo_world_artifact"]
    generated_world_path = ROOT_DIR / world_artifact["world_file_path_or_artifact_uri"]
    world_root = fixture_e2e._copy_px4_world_assets(run_dir)
    prepared_world_path = fixture_e2e._inject_digital_twin_terrain(
        world_root,
        generated_world_path,
    )
    started_at = datetime.now(timezone.utc)
    command: list[str] = []
    pid = 0
    startup_logs = ""
    startup_ok = False
    upload_observed: dict[str, Any] | None = None
    stopped_at = None
    exit_code = 0
    try:
        command, pid, startup_logs, startup_ok = fixture_e2e._start_world_bound_container(
            world_root,
            prepared_world_path,
        )
        if not startup_ok:
            raise RuntimeError(
                "source-backed Digital Twin PX4/Gazebo startup failed"
            )
        items = digital_twin_candidate_upload_items(
            digital_twin["digital_twin_px4_mission_item_candidate"]
        )
        upload_observed = upload_smoke._docker_exec_upload_with_heartbeat(
            _SourceBackedWorldBoundUploadShim,
            items,
        )
        if upload_observed.get("mission_ack_observed") is not True:
            raise RuntimeError(
                "source-backed Digital Twin upload did not observe ACK"
            )
    finally:
        final_logs, exit_code = fixture_e2e._stop_world_bound_container()
        stopped_at = datetime.now(timezone.utc)
        (run_dir / "px4_gazebo.stdout.log").write_text(
            final_logs or startup_logs,
            encoding="utf-8",
        )
        (run_dir / "px4_gazebo.stderr.log").write_text("", encoding="utf-8")

    process_run = build_digital_twin_sitl_process_run_from_observed_container(
        gazebo_world_artifact=world_artifact,
        command=command,
        process_pids=(pid,),
        stdout_ref=str(run_dir / "px4_gazebo.stdout.log"),
        stderr_ref=str(run_dir / "px4_gazebo.stderr.log"),
        started_at=started_at,
        stopped_at=stopped_at,
        exit_status="terminated_after_startup_window",
        exit_code=exit_code,
        startup_error_observed=not startup_ok,
        px4_process_invoked=True,
        world_artifact_load_mode="terrain_injection_into_default_world",
        px4_loaded_world_file_path=str(prepared_world_path),
        repo_root=ROOT_DIR,
    )
    receipt = build_digital_twin_sitl_mission_upload_receipt(
        px4_mission_item_candidate=digital_twin["digital_twin_px4_mission_item_candidate"],
        sitl_process_run=process_run,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        same_run_binding_ref=(
            "digital_twin_sitl_binding_gate:"
            + digital_twin["digital_twin_sitl_binding_gate"]["gate_id"]
        ),
        uploader=_ObservedSourceBackedUploader(upload_observed or {}),
        timeout_seconds=5.0,
        now=NOW,
    )
    execution_result = build_digital_twin_sitl_execution_result(
        gazebo_world_artifact=world_artifact,
        coordinate_transform_candidate=digital_twin["coordinate_transform_candidate"],
        px4_mission_item_candidate=digital_twin["digital_twin_px4_mission_item_candidate"],
        sitl_binding_gate=digital_twin["digital_twin_sitl_binding_gate"],
        sitl_process_run=process_run,
        mission_upload_receipt=receipt,
        source_backed_inputs_summary=source_summary,
        now=NOW,
    )
    mission_item = digital_twin["digital_twin_px4_mission_item_candidate"]
    summary = {
        "schema_version": execution_result.schema_version,
        "schema_version_expected": DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION,
        "execution_result_ref": digital_twin_sitl_execution_result_ref(execution_result),
        "execution_status": execution_result.execution_status,
        "source_backed_inputs_summary": execution_result.source_backed_inputs_summary,
        "source_backed_target": source_summary["source_backed_target"],
        "source_backed_terrain": source_summary["source_backed_terrain"],
        "source_backed_weather": source_summary["source_backed_weather"],
        "terrain_provider_response_status": source_summary[
            "terrain_provider_response_status"
        ],
        "weather_provider_response_status": source_summary[
            "weather_provider_response_status"
        ],
        "terrain_sampling_mode": mission_item["terrain_sampling_mode"],
        "takeoff_terrain_elevation_m": mission_item["takeoff_terrain_elevation_m"],
        "takeoff_altitude_m": mission_item["candidate_items"][0]["altitude_m"],
        "waypoint_altitude_m": mission_item["candidate_items"][1]["altitude_m"],
        "world_artifact_load_mode": execution_result.world_artifact_load_mode,
        "px4_loaded_world_file_path": execution_result.px4_loaded_world_file_path,
        "world_bound": execution_result.world_bound,
        "terrain_artifact_used": execution_result.terrain_artifact_used,
        "prepared_px4_world_path": str(prepared_world_path),
        "generated_world_file_used": world_artifact["world_file_path_or_artifact_uri"],
        "gazebo_world_artifact_ref": execution_result.gazebo_world_artifact_ref,
        "digital_twin_px4_mission_item_candidate_ref": (
            execution_result.digital_twin_px4_mission_item_candidate_ref
        ),
        "process_run_ref": digital_twin_sitl_process_run_ref(process_run),
        "process_launch_attempted": process_run.process_launch_attempted,
        "gazebo_execution_invoked": process_run.gazebo_execution_invoked,
        "px4_process_invoked": process_run.px4_process_invoked,
        "mission_upload_receipt_ref": (
            execution_result.digital_twin_sitl_mission_upload_receipt_ref
        ),
        "mission_items_source": receipt.mission_items_source,
        "mission_upload_observed": execution_result.mission_upload_observed,
        "mission_ack_observed": execution_result.mission_ack_observed,
        "mission_ack_type": execution_result.mission_ack_type,
        "mission_request_sequences": list(receipt.mission_request_sequences),
        "heartbeat_observed": execution_result.heartbeat_observed,
        "flight_telemetry_observed": execution_result.flight_telemetry_observed,
        "payload_release_observed": execution_result.payload_release_observed,
        "dropoff_verified": execution_result.dropoff_verified,
        "observed_facts_only": execution_result.observed_facts_only,
        "hardware_target_allowed": execution_result.hardware_target_allowed,
        "physical_execution_invoked": execution_result.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            execution_result.approval_free_stronger_execution_allowed
        ),
        "blocked_reasons": list(execution_result.blocked_reasons),
        "execution_result_hash_equals_sha256": (
            execution_result.execution_result_hash == execution_result.sha256
        ),
    }
    assert summary["execution_status"] == (
        "terrain_injected_world_upload_ack_telemetry_observed"
    )
    assert summary["source_backed_target"] is True
    assert summary["source_backed_terrain"] is True
    assert summary["source_backed_weather"] is True
    assert summary["terrain_sampling_mode"] == "anchor_point_sampled"
    assert summary["world_artifact_load_mode"] == "terrain_injection_into_default_world"
    assert summary["world_bound"] is False
    assert summary["terrain_artifact_used"] is True
    assert summary["process_launch_attempted"] is True
    assert summary["gazebo_execution_invoked"] is True
    assert summary["px4_process_invoked"] is True
    assert summary["mission_upload_observed"] is True
    assert summary["mission_ack_observed"] is True
    assert summary["heartbeat_observed"] is True
    assert summary["payload_release_observed"] is False
    assert summary["dropoff_verified"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["execution_result_hash_equals_sha256"] is True
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(
        "SMOKE_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
