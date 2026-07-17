#!/usr/bin/env python3
"""Opt-in smoke for actual PX4/Gazebo horizontal pickup-to-dropoff route."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from tempfile import TemporaryDirectory
from typing import Any, Callable
import xml.etree.ElementTree as ET

from scripts import smoke_px4_gazebo_collision_contact_event as contact_event_smoke
from scripts import smoke_px4_gazebo_sitl_mission_upload as mission_upload_smoke
from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.px4_gazebo_coupled_delivery import (
    validate_px4_gazebo_coupled_command_dispatch,
)
from src.runtime.px4_gazebo_emergency_dispatcher import (
    build_px4_gazebo_emergency_command_allowlist,
    build_px4_gazebo_emergency_command_approval,
    run_px4_gazebo_emergency_command_dispatch,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    build_px4_gazebo_route_deviation_abort,
    build_px4_gazebo_route_recovery_completion,
    derive_px4_gazebo_route_target_ned,
)
from src.runtime.px4_gazebo_route.embedded_mavlink import (
    MAVLINK_HEARTBEAT_OBSERVER_HELPER,
    MAVLINK_LINK_LOSS_APPLICATOR_HELPER,
    MAVLINK_ROUTE_HELPER,
)
from src.runtime.px4_gazebo_route.execution import (
    apply_bounded_mavlink_link_loss as _execute_bounded_mavlink_link_loss,
    observe_mavlink_heartbeat_gap as _execute_mavlink_heartbeat_observer,
    run_route_with_monitor as _execute_route_with_monitor,
    send_embedded_helper as _execute_embedded_helper,
)
from src.runtime.px4_gazebo_route.environmental_realism import (
    project_landing_zone_blocked_realism as _project_landing_zone_blocked_realism,
    project_operational_markers_realism as _project_operational_markers_realism,
    project_visibility_realism as _project_visibility_realism,
    run_sensor_failure_realism as _run_sensor_failure_realism,
    run_thermal_weather_realism as _run_thermal_weather_realism,
)
from src.runtime.px4_gazebo_route.dynamic_observation import (
    observe_moving_actor_pose as _observe_moving_actor_pose,
    project_moving_actor_proximity as _project_moving_actor_proximity,
    run_moving_actor_waypoint_motion_application as _run_moving_actor_waypoint_motion_application,
)
from src.runtime.px4_gazebo_route.collision_observation import (
    project_route_blocking_candidate as _project_route_blocking_candidate,
    run_collision_obstacle_evidence as _run_collision_obstacle_evidence,
)
from src.runtime.px4_gazebo_route.contact_integration import (
    project_horizontal_contact_topic_integration as _project_horizontal_contact_topic_integration,
)
from src.runtime.px4_gazebo_route.finalization import (
    RouteFinalizationInputs as _RouteFinalizationInputs,
    RouteFinalizationResult as _RouteFinalizationResult,
    finalize_route_observation as _finalize_route_observation,
)
from src.runtime.px4_gazebo_route.route_decision import (
    observe_route_blocking_decision as _observe_route_blocking_decision,
)
from src.runtime.px4_gazebo_route.terminal_action import (
    execute_route_terminal_action as _orchestrate_route_terminal_action,
)
from src.runtime.px4_gazebo_route.reporting import (
    RouteSummaryInputs as _RouteSummaryInputs,
    build_route_summary as _build_route_summary,
)
from src.runtime.px4_gazebo_route.recovery_outcomes import (
    PAYLOAD_RECOVERY_ACTION_REF,
    RecoveryCycleOutcome as _RecoveryCycleOutcome,
    build_payload_post_recovery_action as _build_payload_post_recovery_action,
    build_payload_recovery_action as _build_payload_recovery_action,
    payload_recovery_terminal_status as _payload_recovery_terminal_status,
    recovery_task_artifacts as _recovery_task_artifacts,
)
from src.runtime.px4_gazebo_route.recovery_execution import (
    observe_dispatched_recovery as _observe_dispatched_recovery,
)
from src.runtime.px4_gazebo_route.recovery_persistence import (
    RouteDeviationRecoveryPersistenceInputs as _RouteDeviationRecoveryPersistenceInputs,
    persist_route_deviation_recovery as _persist_route_deviation_recovery,
)
from src.runtime.px4_gazebo_route.recovery_workflow import (
    assemble_route_deviation_recovery as _assemble_route_deviation_recovery,
)
from src.runtime.px4_gazebo_route.recovery_reporting import (
    PayloadRecoverySummaryInputs as _PayloadRecoverySummaryInputs,
    RouteDeviationRecoverySummaryInputs as _RouteDeviationRecoverySummaryInputs,
    build_payload_recovery_summary as _build_payload_recovery_summary,
    build_route_deviation_recovery_summary as _build_route_deviation_recovery_summary,
    recovery_pose_rows as _recovery_pose_rows,
)
from src.runtime.px4_gazebo_route.observation import (
    battery_status_from_listener_output as _battery_status_from_listener_output,
    contact_topic_observation as _contact_topic_observation,
    distance_to_segment_xy as _distance_to_segment_xy,
    listener_field as _listener_field,
    point_to_segment_distance_m as _point_to_segment_distance_m,  # noqa: F401
    pose_rows as _pose_rows,
    select_contact_topic as _select_contact_topic,
    terminal_pose_summary_fields as _terminal_pose_summary_fields,
    xy_pairs_match as _xy_pairs_match,  # noqa: F401
)
from src.runtime.px4_gazebo_route.operational_verification import (
    build_alternate_landing_candidate_evidence as _build_alternate_landing_candidate_evidence,
    build_operational_incident_report as _build_operational_incident_report,
    build_route_blocking_verification as _build_route_blocking_verification,
    build_traffic_conflict_verification as _build_traffic_conflict_verification,
)
from src.runtime.px4_gazebo_route.operational_outcomes import (
    project_alternate_landing_outcome as _project_alternate_landing_outcome,
    project_rth_outcome as _project_rth_outcome,
)
from src.runtime.px4_gazebo_route.alternate_route import (
    alternate_mission_upload_payloads as _alternate_route_mission_upload_payloads,
    execute_alternate_route_rewrite as _run_alternate_route_rewrite,
    project_alternate_mission_upload as _project_alternate_mission_upload,
)
from src.runtime.px4_gazebo_route import scenario as _route_scenario
from src.runtime.px4_gazebo_route import supervision as _route_supervision
from src.runtime.px4_gazebo_route.artifacts import (
    create_run_directory as _new_run_dir,
    mark_cleanup_observed as _mark_cleanup_observed,
    snapshot_task_database_evidence as _snapshot_task_database_evidence,
    write_recovery_run_artifacts as _write_recovery_run_artifacts,
    write_run_artifacts as _write_run_artifacts,
)
from src.runtime.px4_gazebo_route.audit import (
    PayloadRecoveryAuditExpectations as _PayloadRecoveryAuditExpectations,
    RouteAuditExpectations as _RouteAuditExpectations,
    RouteDeviationRecoveryAuditExpectations as _RouteDeviationRecoveryAuditExpectations,
    audit_payload_recovery_summary as _audit_payload_recovery_summary,
    audit_route_deviation_recovery_summary as _audit_route_deviation_recovery_summary,
    audit_route_summary as _audit_route_summary,
)
from src.runtime.px4_gazebo_route.bootstrap import (
    RouteBootstrapResult as _RouteBootstrapResult,
    bootstrap_route_task as _bootstrap_route_task,
)
from src.runtime.px4_gazebo_route.configuration import (
    PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX,
    parse_route_args as _parse_args,
    payload_advisory_recovery_requested as _payload_advisory_recovery_requested,
    validate_payload_advisory_recovery_args as _validate_payload_advisory_recovery_args,
    validate_planned_route_stream_budget as _assert_planned_route_stream_budget,
)
from src.runtime.px4_gazebo_route.environment import (
    PAYLOAD_RELEASE_MODEL_ENV,
    alternate_landing_marker_requested as _alternate_landing_marker_requested,
    battery_requested_profile as _battery_requested_profile,
    collision_obstacle_contact_topic_requested as _collision_obstacle_contact_topic_requested,
    collision_obstacle_motion_spec as _collision_obstacle_motion_spec,
    collision_obstacle_requested as _collision_obstacle_requested,
    form2a_wind_compensation_request as _form2a_wind_compensation_request,
    landing_zone_blocked_requested as _landing_zone_blocked_requested,
    mavlink_link_degradation_mode_request as _mavlink_link_degradation_mode_request,
    moving_actor_marker_requested as _moving_actor_marker_requested,
    multi_drone_conflict_probe_requested as _multi_drone_conflict_probe_requested,
    no_fly_zone_marker_requested as _no_fly_zone_marker_requested,
    payload_mass_request as _payload_mass_request,
    payload_model_enabled as _payload_model_enabled,
    rth_behavior_requested as _rth_behavior_requested,
    sensor_failure_requested_profile as _sensor_failure_requested_profile,
    telemetry_dropout_mode_request as _telemetry_dropout_mode_request,
    thermal_weather_requested_profile as _thermal_weather_requested_profile,
    traffic_conflict_marker_requested as _traffic_conflict_marker_requested,
    visibility_mode_request as _visibility_mode_request,
    wind_requested_profile as _wind_requested_profile,
)
from src.runtime.px4_gazebo_route.verification import (
    application_status_is_materialized as _application_status_is_materialized,
    px4_param_set_applied as _px4_param_set_applied,
    px4_param_value_matches as _px4_param_value_matches,
    route_corridor_obstacle_application_source_check as _route_corridor_obstacle_application_source_check,
    wind_readback_status as _wind_readback_status,
)
from src.runtime.px4_gazebo_route.world import (
    VISIBILITY_FOG_RENDER_MARKER_ID,
    alternate_landing_world_sdf_patch as _alternate_landing_world_sdf_patch,
    collision_obstacle_world_sdf_patch as _world_collision_obstacle_world_sdf_patch,
    inject_visibility_fog_render_marker as _inject_visibility_fog_render_marker,
    landing_zone_blocked_world_sdf_patch as _landing_zone_blocked_world_sdf_patch,
    moving_actor_waypoint_motion_spec as _moving_actor_waypoint_motion_spec,
    moving_actor_waypoint_trajectory_definition_sha256 as _moving_actor_waypoint_trajectory_definition_sha256,
    moving_actor_world_sdf_patch as _moving_actor_world_sdf_patch,
    no_fly_zone_world_sdf_patch as _no_fly_zone_world_sdf_patch,
    payload_model_sdf_patch as _payload_model_sdf_patch,
    payload_world_sdf_patch as _payload_world_sdf_patch,
    traffic_conflict_world_sdf_patch as _traffic_conflict_world_sdf_patch,
    wind_effects_world_sdf_patch as _wind_effects_world_sdf_patch,
)
from src.runtime.task_store import TaskStore

_form2a_wind_compensation_xy_offset = _route_scenario.wind_compensation_xy_offset
_form2a_wind_feed_forward_xy_mps = _route_scenario.wind_feed_forward_xy_mps
_thermal_battery_drain_factor_from_temperature = (
    _route_scenario.thermal_battery_drain_factor_from_temperature
)
_thermal_motor_derate_factor_from_temperature = (
    _route_scenario.thermal_motor_derate_factor_from_temperature
)
_wind_vector = _route_scenario.wind_vector

OPT_IN_ENV = "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"
PREUPLOAD_MISSION_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_PREUPLOAD_MISSION"
SKIP_EMERGENCY_MAVLINK_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_SKIP_EMERGENCY_MAVLINK"
TERRAIN_WORLD_SDF_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SDF"
TERRAIN_WORLD_SHA256_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SHA256"
TERRAIN_WORLD_SOURCE_REF_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SOURCE_REF"
TERRAIN_PROVIDER_STATUS_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_PROVIDER_STATUS"
TERRAIN_SAMPLING_MODE_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_SAMPLING_MODE"
TERRAIN_VERTICAL_REFERENCE_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_VERTICAL_REFERENCE"
TERRAIN_COLLISION_MODE_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_COLLISION_MODE"
CONTAINER_NAME = "boiled-claw-px4-gazebo-horizontal-route-smoke"
ROUTE_MAVLINK_LOCAL_PORT = 14650
ROUTE_MAVLINK_PX4_PORT = 14600
EMERGENCY_MAVLINK_LOCAL_PORT = 14651
EMERGENCY_MAVLINK_PX4_PORT = 14601
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_HORIZONTAL_ROUTE_IMAGE",
    "px4io/px4-sitl-gazebo:latest",
)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LAND = 21
PREUPLOAD_SUMMARY: dict[str, Any] | None = None
PAYLOAD_RELEASE_SUMMARY: dict[str, Any] | None = None
WIND_REALISM_SUMMARY: dict[str, Any] | None = None
THERMAL_WEATHER_REALISM_SUMMARY: dict[str, Any] | None = None
VEHICLE_REALISM_SUMMARY: dict[str, Any] | None = None
BATTERY_REALISM_SUMMARY: dict[str, Any] | None = None
SENSOR_REALISM_SUMMARY: dict[str, Any] | None = None
WORLD_REALISM_SUMMARY: dict[str, Any] | None = None
VISIBILITY_REALISM_SUMMARY: dict[str, Any] | None = None
OPERATIONAL_REALISM_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_LINEAR_MOTION_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_POSE_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_PROXIMITY_SUMMARY: dict[str, Any] | None = None
COLLISION_OBSTACLE_SUMMARY: dict[str, Any] | None = None
ROUTE_BLOCKING_CANDIDATE_SUMMARY: dict[str, Any] | None = None
HORIZONTAL_CONTACT_TOPIC_SUMMARY: dict[str, Any] | None = None
OPERATIONAL_INCIDENT_REPORT_SUMMARY: dict[str, Any] | None = None
TRAFFIC_CONFLICT_VERIFICATION_SUMMARY: dict[str, Any] | None = None
ROUTE_BLOCKING_VERIFICATION_SUMMARY: dict[str, Any] | None = None
ALTERNATE_LANDING_CANDIDATE_SUMMARY: dict[str, Any] | None = None
ALTERNATE_LANDING_EXECUTION_SUMMARY: dict[str, Any] | None = None
RTH_BEHAVIOR_SUMMARY: dict[str, Any] | None = None
ALTERNATE_MISSION_UPLOAD_SUMMARY: dict[str, Any] | None = None
TELEMETRY_REALISM_SUMMARY: dict[str, Any] | None = None
MAVLINK_LINK_REALISM_SUMMARY: dict[str, Any] | None = None
TERRAIN_WORLD_REALISM_SUMMARY: dict[str, Any] | None = None
LIVE_POSE_TRACE_PATH: Path | None = None
TELEMETRY_DROPOUT_EVENTS: list[dict[str, Any]] = []
TELEMETRY_OBSERVER_SAMPLE_EVENTS: list[dict[str, Any]] = []
BATTERY_STATUS_SAMPLE_INTERVAL_SECONDS = 5.0
BATTERY_STATUS_SAMPLE_TIMEOUT_SECONDS = 1
_LAST_BATTERY_STATUS_SAMPLE_AT = 0.0
_LAST_BATTERY_STATUS_SAMPLE: dict[str, Any] = {
    "battery_status_observed": False,
    "battery_state_source": "px4-listener:battery_status_not_observed",
}
PAYLOAD_MODEL_CONTAINER_PATH = "/tmp/boiled-claw-payload-release-models"
PAYLOAD_DETACH_TOPIC = "/model/x500_0/delivery_payload/detach"
COLLISION_OBSTACLE_CONTACT_TOPIC = "/mission_designer/collision_obstacle/contacts"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo horizontal route smoke.")


def _run(
    command: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _collision_obstacle_world_sdf_patch(*, contact_topic_enabled: bool = True) -> str:
    return _world_collision_obstacle_world_sdf_patch(
        motion=_collision_obstacle_motion_spec(),
        contact_topic=COLLISION_OBSTACLE_CONTACT_TOPIC,
        contact_topic_enabled=contact_topic_enabled,
    )


def _enable_wind_on_x500_base(model_root: Path) -> dict[str, Any]:
    x500_base_sdf_path = model_root / "x500_base" / "model.sdf"
    if not x500_base_sdf_path.exists():
        return {
            "wind_enabled_on_vehicle_links": False,
            "wind_enabled_link_count": 0,
            "x500_base_sdf_path": str(x500_base_sdf_path),
            "error": "x500_base_model_sdf_missing",
        }
    x500_base_sdf = x500_base_sdf_path.read_text(encoding="utf-8")
    if "<enable_wind>true</enable_wind>" not in x500_base_sdf:
        x500_base_sdf = re.sub(
            r'(<link name="[^"]+">\n)',
            r"\1      <enable_wind>true</enable_wind>\n",
            x500_base_sdf,
        )
        x500_base_sdf_path.write_text(x500_base_sdf, encoding="utf-8")
    wind_enabled_link_count = x500_base_sdf_path.read_text(encoding="utf-8").count(
        "<enable_wind>true</enable_wind>"
    )
    return {
        "wind_enabled_on_vehicle_links": wind_enabled_link_count > 0,
        "wind_enabled_link_count": wind_enabled_link_count,
        "x500_base_sdf_path": str(x500_base_sdf_path),
    }


def _prepare_payload_model_root(
    run_dir: Path,
    *,
    payload_mass_kg: float,
    payload_enabled: bool,
    wind_effects_enabled: bool,
    landing_zone_blocked: bool,
    visibility_mode: str | None,
    no_fly_zone_marker: bool,
    traffic_conflict_marker: bool,
    alternate_landing_marker: bool,
    moving_actor_marker: bool,
    collision_obstacle: bool,
    collision_obstacle_contact_topic: bool,
    terrain_world_sdf: Path | None,
) -> Path:
    model_root = (run_dir / "payload_release_models").resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    if not (model_root / "x500" / "model.sdf").exists():
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-v",
                f"{model_root}:/out",
                PX4_GAZEBO_IMAGE,
                "-lc",
                (
                    "rm -rf /out/x500 /out/x500_base; "
                    "mkdir -p /out/worlds; "
                    "cp -a /opt/px4-gazebo/share/gz/models/x500 /out/x500; "
                    "cp -a /opt/px4-gazebo/share/gz/models/x500_base /out/x500_base; "
                    "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /out/worlds/default.sdf"
                ),
            ],
            timeout=120,
        )
    world_path = model_root / "worlds" / "default.sdf"
    terrain_source_hash = ""
    if terrain_world_sdf is not None:
        if not terrain_world_sdf.exists():
            raise FileNotFoundError(f"terrain world SDF missing: {terrain_world_sdf}")
        terrain_source_hash = hashlib.sha256(terrain_world_sdf.read_bytes()).hexdigest()
        world_text = _inject_terrain_model_into_default_world(
            default_world_text=world_path.read_text(encoding="utf-8"),
            terrain_world_sdf=terrain_world_sdf,
            model_root=model_root,
        )
        world_path.write_text(world_text, encoding="utf-8")
    world_text = world_path.read_text(encoding="utf-8")
    if terrain_source_hash:
        world_text = world_text.replace(
            '<world name="default">',
            (
                '<world name="default">\n'
                "    <!-- mission_designer_terrain_source_sha256:"
                f"{terrain_source_hash} -->"
            ),
            1,
        )
    if terrain_world_sdf is not None:
        terrain_heightmap_root = terrain_world_sdf.parent.parent / "heightmaps"
        if terrain_heightmap_root.exists():
            model_heightmap_root = model_root / "heightmaps"
            if model_heightmap_root.exists():
                shutil.rmtree(model_heightmap_root)
            shutil.copytree(terrain_heightmap_root, model_heightmap_root)
    if payload_enabled:
        sdf_path = model_root / "x500" / "model.sdf"
        sdf_text = sdf_path.read_text(encoding="utf-8")
        if "delivery_payload" not in sdf_text:
            sdf_text = sdf_text.replace(
                "  </model>\n</sdf>",
                _payload_model_sdf_patch() + "  </model>\n</sdf>",
            )
            sdf_path.write_text(sdf_text, encoding="utf-8")
    if payload_enabled and "delivery_payload" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _payload_world_sdf_patch(payload_mass_kg=payload_mass_kg) + "  </world>\n</sdf>",
        )
    if wind_effects_enabled:
        wind_requested = _wind_requested_profile()["requested"]
        wind_mean = float(wind_requested["wind_mean_mps"] or 0.0)
        wind_direction = float(wind_requested["wind_direction_deg"] or 0.0)
        wind_x, wind_y = _wind_vector(mean_mps=wind_mean, direction_deg=wind_direction)
        if "gz::sim::systems::WindEffects" not in world_text:
            world_text = world_text.replace(
                "  </world>\n</sdf>",
                _wind_effects_world_sdf_patch(
                    wind_x_mps=wind_x,
                    wind_y_mps=wind_y,
                )
                + "  </world>\n</sdf>",
            )
        _enable_wind_on_x500_base(model_root)
    if landing_zone_blocked and "mission_designer_landing_zone_blocked_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _landing_zone_blocked_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if visibility_mode == "fog" and VISIBILITY_FOG_RENDER_MARKER_ID not in world_text:
        world_text = _inject_visibility_fog_render_marker(world_text)
    if no_fly_zone_marker and "mission_designer_no_fly_zone_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _no_fly_zone_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if traffic_conflict_marker and "mission_designer_traffic_conflict_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _traffic_conflict_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if alternate_landing_marker and "mission_designer_alternate_landing_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _alternate_landing_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if moving_actor_marker and "mission_designer_moving_actor_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _moving_actor_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if collision_obstacle and "mission_designer_collision_obstacle" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _collision_obstacle_world_sdf_patch(contact_topic_enabled=False) + "  </world>\n</sdf>",
        )
    world_path.write_text(world_text, encoding="utf-8")
    return model_root


def _inject_terrain_model_into_default_world(
    *,
    default_world_text: str,
    terrain_world_sdf: Path,
    model_root: Path,
) -> str:
    terrain_world_text = terrain_world_sdf.read_text(encoding="utf-8")
    match = re.search(
        r'    <model name="digital_twin_heightmap_terrain">.*?\n    </model>',
        terrain_world_text,
        re.S,
    )
    if not match:
        raise RuntimeError("terrain world SDF did not include Digital Twin terrain model")
    terrain_model = match.group(0)
    terrain_model = re.sub(
        r"\n        <collision name=\"terrain_collision\">.*?\n        </collision>",
        "\n        <!-- terrain_collision_removed_for_visual_only_horizontal_route -->",
        terrain_model,
        flags=re.S,
    )
    heightmap_uris = sorted(set(re.findall(r"<uri>([^<]+)</uri>", terrain_model)))
    heightmap_root = model_root / "heightmaps"
    heightmap_root.mkdir(parents=True, exist_ok=True)
    for uri in heightmap_uris:
        source = Path(uri)
        if not source.is_absolute():
            source = Path(__file__).resolve().parents[1] / source
        if not source.exists():
            raise FileNotFoundError(f"terrain heightmap URI missing: {uri}")
        shutil.copy2(source, heightmap_root / source.name)
        terrain_model = terrain_model.replace(uri, f"../heightmaps/{source.name}")
    if "digital_twin_heightmap_terrain" in default_world_text:
        return default_world_text
    return default_world_text.replace("  </world>", terrain_model + "\n  </world>", 1)


def _terrain_world_sdf_request() -> Path | None:
    raw = os.getenv(TERRAIN_WORLD_SDF_ENV, "").strip()
    return Path(raw) if raw else None


def _terrain_world_readback(payload_model_root: Path | None) -> dict[str, Any]:
    requested_path = _terrain_world_sdf_request()
    result: dict[str, Any] = {
        "schema_version": "px4_gazebo_horizontal_route_terrain_world_readback.v1",
        "terrain_world_requested": requested_path is not None,
        "terrain_world_loaded_into_sitl": False,
        "terrain_artifact_used": False,
        "world_artifact_load_mode": "flat_default_world",
        "requested_world_sdf_path": str(requested_path) if requested_path else "",
        "terrain_world_source_ref": os.getenv(TERRAIN_WORLD_SOURCE_REF_ENV, ""),
        "terrain_provider_response_status": os.getenv(TERRAIN_PROVIDER_STATUS_ENV, ""),
        "terrain_sampling_mode": os.getenv(TERRAIN_SAMPLING_MODE_ENV, ""),
        "terrain_vertical_reference": os.getenv(TERRAIN_VERTICAL_REFERENCE_ENV, ""),
        "terrain_collision_mode": os.getenv(TERRAIN_COLLISION_MODE_ENV, ""),
    }
    if requested_path is None:
        return result
    if payload_model_root is None:
        result["error"] = "custom_world_root_missing"
        return result
    world_path = payload_model_root / "worlds" / "default.sdf"
    result["world_sdf_path"] = str(world_path)
    if not world_path.exists():
        result["error"] = "world_sdf_missing"
        return result
    world_text = world_path.read_text(encoding="utf-8")
    observed_sha = hashlib.sha256(world_path.read_bytes()).hexdigest()
    expected_sha = os.getenv(TERRAIN_WORLD_SHA256_ENV, "").strip()
    requested_sha = (
        hashlib.sha256(requested_path.read_bytes()).hexdigest() if requested_path.exists() else ""
    )
    result.update(
        {
            "world_sdf_sha256": observed_sha,
            "expected_world_sdf_sha256": expected_sha,
            "world_sdf_hash_match": bool(expected_sha and observed_sha == expected_sha),
            "source_world_sdf_sha256": requested_sha,
            "source_world_sdf_hash_match": bool(expected_sha and requested_sha == expected_sha),
            "terrain_model_present": "digital_twin_heightmap_terrain" in world_text,
            "terrain_collision_present": '<collision name="terrain_collision"' in world_text,
            "terrain_collision_removed_for_visual_only_runtime": (
                "terrain_collision_removed_for_visual_only_horizontal_route" in world_text
            ),
            "terrain_visual_present": "terrain_visual" in world_text,
            "heightmap_file_count": len(list((payload_model_root / "heightmaps").glob("*"))),
            "world_artifact_load_mode": "terrain_injection_into_default_world",
        }
    )
    result["terrain_artifact_used"] = (
        result["terrain_model_present"] is True
        and result["terrain_visual_present"] is True
        and result["heightmap_file_count"] > 0
    )
    result["terrain_world_loaded_into_sitl"] = result["terrain_artifact_used"] is True and (
        result["source_world_sdf_hash_match"] is True or not expected_sha
    )
    return result


def _terrain_world_loaded_into_sitl() -> bool:
    return bool((TERRAIN_WORLD_REALISM_SUMMARY or {}).get("terrain_world_loaded_into_sitl"))


def _terrain_relative_xy_origin(pickup_pose: dict[str, float]) -> tuple[float, float]:
    return _route_scenario.terrain_relative_xy_origin(
        pickup_pose,
        terrain_world_loaded=_terrain_world_loaded_into_sitl(),
    )


def _landing_z_threshold(pickup_pose: dict[str, float]) -> float:
    return _route_scenario.landing_z_threshold(
        pickup_pose,
        terrain_world_loaded=_terrain_world_loaded_into_sitl(),
    )


def _start_container(run_dir: Path) -> Path | None:
    global PREUPLOAD_SUMMARY
    payload_model_enabled = _payload_model_enabled()
    landing_zone_blocked = _landing_zone_blocked_requested()
    visibility_mode = _visibility_mode_request()
    no_fly_zone_marker = _no_fly_zone_marker_requested()
    traffic_conflict_marker = _traffic_conflict_marker_requested()
    alternate_landing_marker = _alternate_landing_marker_requested()
    moving_actor_marker = _moving_actor_marker_requested()
    collision_obstacle = _collision_obstacle_requested()
    collision_obstacle_contact_topic = _collision_obstacle_contact_topic_requested()
    wind_effects_enabled = _wind_requested_profile()["requested_present"]
    terrain_world_sdf = _terrain_world_sdf_request()
    payload_mass_kg = _payload_mass_request() or 0.05
    payload_model_root = (
        _prepare_payload_model_root(
            run_dir,
            payload_mass_kg=payload_mass_kg,
            payload_enabled=payload_model_enabled,
            wind_effects_enabled=wind_effects_enabled,
            landing_zone_blocked=landing_zone_blocked,
            visibility_mode=visibility_mode,
            no_fly_zone_marker=no_fly_zone_marker,
            traffic_conflict_marker=traffic_conflict_marker,
            alternate_landing_marker=alternate_landing_marker,
            moving_actor_marker=moving_actor_marker,
            collision_obstacle=collision_obstacle,
            collision_obstacle_contact_topic=collision_obstacle_contact_topic,
            terrain_world_sdf=terrain_world_sdf,
        )
        if payload_model_enabled
        or wind_effects_enabled
        or landing_zone_blocked
        or visibility_mode in ("fog", "smoke")
        or no_fly_zone_marker
        or traffic_conflict_marker
        or alternate_landing_marker
        or moving_actor_marker
        or collision_obstacle
        or terrain_world_sdf is not None
        else None
    )
    extra_args: list[str] = []
    if payload_model_root is not None:
        extra_args.extend(
            [
                "-v",
                f"{payload_model_root}:{PAYLOAD_MODEL_CONTAINER_PATH}:ro",
                "-e",
                f"PX4_GZ_MODELS={PAYLOAD_MODEL_CONTAINER_PATH}",
                "-e",
                (
                    "GZ_SIM_RESOURCE_PATH="
                    f"{PAYLOAD_MODEL_CONTAINER_PATH}:"
                    "/opt/px4-gazebo/share/gz/models"
                ),
                "-e",
                f"PX4_GZ_WORLDS={PAYLOAD_MODEL_CONTAINER_PATH}/worlds",
            ]
        )
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"{EMERGENCY_MAVLINK_PX4_PORT}:{EMERGENCY_MAVLINK_PX4_PORT}/udp",
            "-e",
            "PX4_SIM_MODEL=gz_x500",
            "-e",
            "PX4_GZ_WORLD=default",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            *extra_args,
            PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    _wait_for_startup()
    if os.getenv(PREUPLOAD_MISSION_ENV) == "1":
        mission_upload_smoke.CONTAINER_NAME = CONTAINER_NAME
        PREUPLOAD_SUMMARY = mission_upload_smoke._actual_upload()
        assert PREUPLOAD_SUMMARY["mission_ack_observed"] is True
        assert PREUPLOAD_SUMMARY["mission_ack_type"] == 0
    else:
        PREUPLOAD_SUMMARY = None
    _start_route_ack_mavlink_instance()
    if os.getenv(SKIP_EMERGENCY_MAVLINK_ENV) != "1":
        _start_emergency_mavlink_instance()
    return payload_model_root


def _stop_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _start_route_ack_mavlink_instance() -> None:
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                f"/opt/px4-gazebo/bin/px4-mavlink start "
                f"-u {ROUTE_MAVLINK_PX4_PORT} -r 400000 "
                f"-t 127.0.0.1 -o {ROUTE_MAVLINK_LOCAL_PORT} -m onboard"
            ),
        ],
        timeout=20,
    )
    time.sleep(1)


def _start_emergency_mavlink_instance() -> None:
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                "HOST_IP=$(getent ahostsv4 host.docker.internal | "
                "awk '{print $1; exit}'); "
                'test -n "$HOST_IP"; '
                f"/opt/px4-gazebo/bin/px4-mavlink start "
                f"-u {EMERGENCY_MAVLINK_PX4_PORT} -r 400000 "
                f'-t "$HOST_IP" -o {EMERGENCY_MAVLINK_LOCAL_PORT} '
                "-m onboard"
            ),
        ],
        timeout=20,
    )
    time.sleep(1)


def _logs(tail: str = "260") -> str:
    return _run(["docker", "logs", "--tail", tail, CONTAINER_NAME], check=False).stdout


def _all_logs() -> str:
    return _run(["docker", "logs", CONTAINER_NAME], check=False).stdout


def _reset_battery_status_cache() -> None:
    global _LAST_BATTERY_STATUS_SAMPLE_AT, _LAST_BATTERY_STATUS_SAMPLE
    _LAST_BATTERY_STATUS_SAMPLE_AT = 0.0
    _LAST_BATTERY_STATUS_SAMPLE = {
        "battery_status_observed": False,
        "battery_state_source": "px4-listener:battery_status_not_observed",
    }


def _wind_physics_world_readback(
    payload_model_root: Path | None,
    *,
    expected_x: float,
    expected_y: float,
) -> dict[str, Any]:
    if payload_model_root is None:
        return {
            "wind_effects_world_sdf_readback_observed": False,
            "wind_effects_plugin_materialized": False,
            "wind_world_linear_velocity_matches_requested": False,
            "wind_enabled_on_vehicle_links": False,
            "wind_enabled_link_count": 0,
            "source": "custom_world_not_used",
        }
    world_path = payload_model_root / "worlds" / "default.sdf"
    x500_base_sdf_path = payload_model_root / "x500_base" / "model.sdf"
    result: dict[str, Any] = {
        "wind_effects_world_sdf_readback_observed": False,
        "wind_effects_plugin_materialized": False,
        "wind_world_linear_velocity_matches_requested": False,
        "wind_enabled_on_vehicle_links": False,
        "wind_enabled_link_count": 0,
        "world_sdf_path": str(world_path),
        "x500_base_sdf_path": str(x500_base_sdf_path),
        "source": "gazebo_world_sdf_and_x500_base_sdf",
    }
    if not world_path.exists():
        result["error"] = "world_sdf_missing"
        return result
    try:
        world_text = world_path.read_text(encoding="utf-8")
        root = ET.fromstring(world_text)
    except Exception as exc:
        result["error"] = f"world_sdf_parse_failed:{str(exc)[-200:]}"
        return result
    result["world_sdf_sha256"] = hashlib.sha256(world_path.read_bytes()).hexdigest()
    result["wind_effects_plugin_materialized"] = any(
        plugin.attrib.get("name") == "gz::sim::systems::WindEffects"
        for plugin in root.iter("plugin")
    )
    velocity_text = (root.findtext(".//wind/linear_velocity") or "").strip()
    try:
        parts = [float(part) for part in velocity_text.split()]
    except ValueError:
        parts = []
    if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
        result["world_wind_vector_x_mps"] = parts[0]
        result["world_wind_vector_y_mps"] = parts[1]
        result["wind_world_linear_velocity_matches_requested"] = (
            abs(parts[0] - expected_x) <= 1e-6 and abs(parts[1] - expected_y) <= 1e-6
        )
    if x500_base_sdf_path.exists():
        x500_base_text = x500_base_sdf_path.read_text(encoding="utf-8")
        result["x500_base_sdf_sha256"] = hashlib.sha256(x500_base_sdf_path.read_bytes()).hexdigest()
        result["wind_enabled_link_count"] = x500_base_text.count("<enable_wind>true</enable_wind>")
        result["wind_enabled_on_vehicle_links"] = result["wind_enabled_link_count"] > 0
    result["wind_effects_world_sdf_readback_observed"] = (
        result["wind_effects_plugin_materialized"] is True
        and result["wind_world_linear_velocity_matches_requested"] is True
        and result["wind_enabled_on_vehicle_links"] is True
    )
    return result


def _wind_runtime_gazebo_readback(payload_model_root: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "gazebo_runtime_world_model_readback_observed": False,
        "gazebo_runtime_world_path_observed": False,
        "gazebo_runtime_world_ready_observed": False,
        "gazebo_runtime_model_bridge_observed": False,
        "gazebo_runtime_vehicle_pose_observed": False,
        "source": "docker_logs_and_gz_pose_info",
    }
    if payload_model_root is None:
        result["source"] = "custom_world_not_used"
        return result

    expected_world_path = f"{PAYLOAD_MODEL_CONTAINER_PATH}/worlds/default.sdf"
    result["expected_runtime_world_path"] = expected_world_path
    logs = _logs("400")
    result["gazebo_runtime_world_path_observed"] = (
        f"Starting gazebo with world: {expected_world_path}" in logs
    )
    result["gazebo_runtime_world_ready_observed"] = "Gazebo world is ready" in logs
    result["gazebo_runtime_model_bridge_observed"] = (
        "gz_bridge] world: default, model: x500_0" in logs
    )
    pose_result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        check=False,
        timeout=10,
    )
    result["gazebo_runtime_pose_topic_returncode"] = pose_result.returncode
    if pose_result.returncode == 0:
        try:
            pose = parse_gz_sim_entity_pose(pose_result.stdout, entity_name="x500_0")
            result["gazebo_runtime_vehicle_pose_observed"] = True
            result["gazebo_runtime_vehicle_pose"] = {
                key: float(pose[key]) for key in ("x", "y", "z")
            }
        except Exception as exc:
            result["gazebo_runtime_vehicle_pose_error"] = str(exc)[-200:]
    result["gazebo_runtime_world_model_readback_observed"] = (
        result["gazebo_runtime_world_path_observed"] is True
        and result["gazebo_runtime_world_ready_observed"] is True
        and result["gazebo_runtime_model_bridge_observed"] is True
        and result["gazebo_runtime_vehicle_pose_observed"] is True
    )
    return result


def _apply_wind_realism(payload_model_root: Path | None = None) -> dict[str, Any]:
    profile = _wind_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    wind_mean_capability_status = "not_requested"
    wind_gust_capability_status = "not_requested"
    wind_variance_capability_status = "not_requested"
    application_status = "not_requested"
    observation_status = "not_requested"
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    if requested_present:
        mean = float(requested["wind_mean_mps"] or 0.0)
        direction = float(requested["wind_direction_deg"] or 0.0)
        gust = float(requested["wind_gust_mps"] or mean)
        variance = float(requested["wind_variance"] or 0.0)
        wind_x, wind_y = _wind_vector(mean_mps=mean, direction_deg=direction)
        if requested["wind_gust_mps"] is not None or requested["wind_variance"] is not None:
            approximation_reasons.append(
                "gazebo_wind_message_applies_constant_linear_velocity_only"
            )
        message = f"enable_wind: true linear_velocity {{ x: {wind_x} y: {wind_y} z: 0 }}"
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        target_topic = "/world/default/wind"
        result = _run(
            [
                "docker",
                "exec",
                CONTAINER_NAME,
                "sh",
                "-lc",
                (
                    "command -v gz >/dev/null 2>&1 && "
                    "readback_file=$(mktemp) && "
                    "readback_err=$(mktemp) && "
                    f"gz topic -t {shlex.quote(target_topic)} "
                    f"-m gz.msgs.Wind -p {shlex.quote(message)}; "
                    "publish_status=$?; "
                    f"timeout 3 gz topic -e -t {shlex.quote(target_topic)} -n 1 "
                    '>"$readback_file" 2>"$readback_err" & '
                    "reader_pid=$! && "
                    "sleep 1 && "
                    "for _i in 1 2 3 4 5; do "
                    f"gz topic -t {shlex.quote(target_topic)} "
                    f"-m gz.msgs.Wind -p {shlex.quote(message)}; "
                    "candidate_status=$?; "
                    "if [ $candidate_status -eq 0 ]; then publish_status=0; fi; "
                    "sleep 0.2; "
                    "done; "
                    "wait $reader_pid; readback_status=$?; "
                    'cat "$readback_file"; '
                    'printf "\\n__BC_WIND_PUBLISH_STATUS=%s\\n" "$publish_status"; '
                    'printf "__BC_WIND_READBACK_STATUS=%s\\n" "$readback_status"; '
                    'cat "$readback_err" >&2; '
                    'rm -f "$readback_file" "$readback_err"; '
                    "test $publish_status -eq 0"
                ),
            ],
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            readback = _wind_readback_status(
                result.stdout,
                expected_x=wind_x,
                expected_y=wind_y,
            )
            physics_readback = _wind_physics_world_readback(
                payload_model_root,
                expected_x=wind_x,
                expected_y=wind_y,
            )
            runtime_readback = _wind_runtime_gazebo_readback(payload_model_root)
            terminal_physics_observed = bool(
                physics_readback["wind_effects_world_sdf_readback_observed"]
                and runtime_readback["gazebo_runtime_world_model_readback_observed"]
            )
            if not physics_readback["wind_effects_world_sdf_readback_observed"]:
                unsupported_reasons.append("gazebo_wind_terminal_physics_not_observed")
            if not runtime_readback["gazebo_runtime_world_model_readback_observed"]:
                unsupported_reasons.append("gazebo_wind_runtime_world_model_not_observed")
            wind_mean_capability_status = (
                "supported" if terminal_physics_observed else "unsupported"
            )
            wind_gust_capability_status = (
                "unsupported"
                if not terminal_physics_observed and requested["wind_gust_mps"] is not None
                else ("approximated" if requested["wind_gust_mps"] is not None else "not_requested")
            )
            wind_variance_capability_status = (
                "unsupported"
                if not terminal_physics_observed and requested["wind_variance"] is not None
                else ("approximated" if requested["wind_variance"] is not None else "not_requested")
            )
            application_status = (
                "unsupported"
                if not terminal_physics_observed
                else ("applied_with_approximations" if approximation_reasons else "applied")
            )
            observation_status = (
                "applied_config_observed" if terminal_physics_observed else "unsupported"
            )
            applied = {
                "method": "gz_topic_wind_message",
                "target": target_topic,
                "topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "terminal_physics_method": (
                    "gazebo_wind_effects_world_sdf"
                    if physics_readback["wind_effects_world_sdf_readback_observed"]
                    else "not_observed"
                ),
                "requested_mps": mean,
                "applied_mps": mean,
                "publish_attempt_count": 6,
                "requested_direction_deg": direction,
                "applied_direction_deg": direction,
                "applied_fields": [
                    "wind_mean_mps",
                    "wind_direction_deg",
                ],
                "approximated_fields": [
                    field
                    for field, value in (
                        ("wind_gust_mps", requested["wind_gust_mps"]),
                        ("wind_variance", requested["wind_variance"]),
                    )
                    if value is not None
                ],
                "wind_vector_x_mps": wind_x,
                "wind_vector_y_mps": wind_y,
                "gust_mps": gust,
                "variance": variance,
                "applied_message": message,
                "applied_message_sha256": message_sha256,
                "applied_file_path": physics_readback.get("world_sdf_path"),
                "applied_file_sha256": physics_readback.get("world_sdf_sha256"),
                "wind_effects_plugin_materialized": physics_readback[
                    "wind_effects_plugin_materialized"
                ],
                "wind_enabled_on_vehicle_links": physics_readback["wind_enabled_on_vehicle_links"],
                "wind_enabled_link_count": physics_readback["wind_enabled_link_count"],
                "wind_world_linear_velocity_matches_requested": physics_readback[
                    "wind_world_linear_velocity_matches_requested"
                ],
                "gazebo_runtime_world_model_readback_observed": runtime_readback[
                    "gazebo_runtime_world_model_readback_observed"
                ],
                "gazebo_runtime_world_path_observed": runtime_readback[
                    "gazebo_runtime_world_path_observed"
                ],
                "gazebo_runtime_world_ready_observed": runtime_readback[
                    "gazebo_runtime_world_ready_observed"
                ],
                "gazebo_runtime_model_bridge_observed": runtime_readback[
                    "gazebo_runtime_model_bridge_observed"
                ],
                "gazebo_runtime_vehicle_pose_observed": runtime_readback[
                    "gazebo_runtime_vehicle_pose_observed"
                ],
                "gazebo_runtime_source": runtime_readback["source"],
                "px4_param_snapshot_ref": None,
                "source": "mission_designer_coordinate_route_env",
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            observed = {
                "source": (
                    "gz_topic_echo_readback"
                    if readback["readback_observed"]
                    else "gz_topic_publish_returncode"
                ),
                "observed": terminal_physics_observed,
                "returncode": result.returncode,
                "wind_topic_readback_observed": readback["readback_observed"],
                "wind_topic_readback_status": readback["readback_status"],
                "wind_topic_readback_publish_status": readback["readback_publish_status"],
                "wind_topic_publish_attempt_count": 6,
                "wind_mean_mps": mean,
                "wind_direction_deg": direction,
                "wind_vector_x_mps": wind_x,
                "wind_vector_y_mps": wind_y,
                "readback_wind_vector_x_mps": readback["readback_wind_vector_x_mps"],
                "readback_wind_vector_y_mps": readback["readback_wind_vector_y_mps"],
                "target_topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "applied_message_sha256": message_sha256,
                "readback_message_sha256": readback["readback_message_sha256"],
                "wind_effects_world_sdf_readback_observed": physics_readback[
                    "wind_effects_world_sdf_readback_observed"
                ],
                "wind_effects_plugin_materialized": physics_readback[
                    "wind_effects_plugin_materialized"
                ],
                "wind_world_linear_velocity_matches_requested": physics_readback[
                    "wind_world_linear_velocity_matches_requested"
                ],
                "wind_enabled_on_vehicle_links": physics_readback["wind_enabled_on_vehicle_links"],
                "wind_enabled_link_count": physics_readback["wind_enabled_link_count"],
                "world_sdf_sha256": physics_readback.get("world_sdf_sha256"),
                "x500_base_sdf_sha256": physics_readback.get("x500_base_sdf_sha256"),
                "gazebo_runtime_world_model_readback_observed": runtime_readback[
                    "gazebo_runtime_world_model_readback_observed"
                ],
                "gazebo_runtime_world_path_observed": runtime_readback[
                    "gazebo_runtime_world_path_observed"
                ],
                "gazebo_runtime_world_ready_observed": runtime_readback[
                    "gazebo_runtime_world_ready_observed"
                ],
                "gazebo_runtime_model_bridge_observed": runtime_readback[
                    "gazebo_runtime_model_bridge_observed"
                ],
                "gazebo_runtime_vehicle_pose_observed": runtime_readback[
                    "gazebo_runtime_vehicle_pose_observed"
                ],
                "gazebo_runtime_vehicle_pose": runtime_readback.get("gazebo_runtime_vehicle_pose"),
                "gazebo_runtime_expected_world_path": runtime_readback.get(
                    "expected_runtime_world_path"
                ),
                "gazebo_runtime_source": runtime_readback["source"],
            }
        else:
            wind_mean_capability_status = "unsupported"
            wind_gust_capability_status = "unsupported"
            wind_variance_capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_wind_topic_publish_failed")
            observed = {
                "source": "gz_topic_publish_returncode",
                "observed": False,
                "returncode": result.returncode,
                "target_topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "attempted_message_sha256": message_sha256,
                "stdout_tail": result.stdout[-500:],
                "stderr_tail": result.stderr[-500:],
            }
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_wind_gust",
        "wind_mean": wind_mean_capability_status,
        "wind_gust": wind_gust_capability_status,
        "wind_variance": wind_variance_capability_status,
        "support_detection_method": (
            "gz_topic_wind_effects_config_and_runtime_world_model_readback"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "environment_condition_profile": profile,
        "simulator_capability_matrix": capability,
        "simulator_condition_application": application,
        "observed_environment_evidence": evidence,
    }


def _wind_realism_summary_artifacts(*, cleanup_status: str) -> dict[str, Any]:
    return {
        "environment_condition_profile": (WIND_REALISM_SUMMARY or {}).get(
            "environment_condition_profile", {}
        ),
        "simulator_capability_matrix": (WIND_REALISM_SUMMARY or {}).get(
            "simulator_capability_matrix", {}
        ),
        "simulator_condition_application": (WIND_REALISM_SUMMARY or {}).get(
            "simulator_condition_application", {}
        ),
        "observed_environment_evidence": (WIND_REALISM_SUMMARY or {}).get(
            "observed_environment_evidence", {}
        ),
        "scenario_cleanup_receipt": {
            "schema_version": "scenario_cleanup_receipt.v1",
            "cleanup_id": "scenario_cleanup_receipt:horizontal_route_isolated_container",
            "cleanup_scope": "isolated_px4_gazebo_container",
            "cleanup_status": cleanup_status,
            "container_name": CONTAINER_NAME,
            "condition_refs": [
                "environment_condition_profile:mission_designer_wind_gust",
                "thermal_weather_condition_profile:mission_designer_temperature",
                "vehicle_condition_profile:mission_designer_payload_mass",
                "battery_condition_profile:mission_designer_battery_threshold",
                "sensor_condition_profile:mission_designer_sensor_failure",
                "gazebo_world_condition_profile:mission_designer_landing_zone_blocked",
                "visibility_condition_profile:mission_designer_visibility",
                "operational_condition_profile:mission_designer_operational_markers",
                "traffic_conflict_profile:mission_designer_visual_marker",
                "alternate_landing_profile:mission_designer_visual_marker",
                "dynamic_actor_profile:mission_designer_moving_visual_marker",
                "telemetry_degradation_profile:mission_designer_observer_dropout",
                "mavlink_link_degradation_profile:mission_designer_link_probe",
            ],
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _vehicle_payload_mass_realism(
    *,
    payload_model_root: Path | None,
    payload_release_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_mass = _payload_mass_request()
    requested_present = requested_mass is not None
    condition = {
        "schema_version": "vehicle_condition_profile.v1",
        "condition_id": "vehicle_condition_profile:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "requested": {
            "payload_mass_kg": requested_mass,
            "payload_mounted": _payload_model_enabled(),
            "release_mechanism": "gazebo_detachable_joint",
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    observed: dict[str, Any] = {}
    if requested_present:
        world_path = (
            None if payload_model_root is None else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is not None and world_path.exists():
            world_text = world_path.read_text(encoding="utf-8")
            applied_mass = None
            for model in ET.fromstring(world_text).iter("model"):
                if model.attrib.get("name") != "delivery_payload":
                    continue
                mass = model.find("./link/inertial/mass")
                if mass is not None and mass.text is not None:
                    applied_mass = float(mass.text)
                break
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            if applied_mass is not None and math.isclose(
                applied_mass,
                float(requested_mass),
                rel_tol=0.0,
                abs_tol=0.000001,
            ):
                application_status = "applied"
                observation_status = "model_sdf_observed"
                world_sdf_hash_match = True
                applied = {
                    "method": "payload_model_sdf_mass",
                    "world_sdf_path": str(world_path),
                    "payload_model": "delivery_payload",
                    "payload_link": "payload_link",
                    "payload_mass_kg": applied_mass,
                    "world_sdf_sha256": world_sha256,
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "model_materialized": True,
                    "payload_mass_materialized": True,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
                observed = {
                    "source": "payload_model_world_sdf",
                    "observed": True,
                    "payload_mass_kg": applied_mass,
                    "requested_payload_mass_kg": float(requested_mass),
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "model_materialized": True,
                    "payload_mass_materialized": True,
                    "world_sdf_sha256": world_sha256,
                }
                if (
                    payload_release_summary
                    and payload_release_summary.get("payload_release_observed") is True
                ):
                    observation_status = "model_sdf_and_payload_release_observed"
                    observed["payload_release_observed"] = True
                    observed["payload_release_event_source"] = payload_release_summary.get(
                        "payload_release_event_source"
                    )
                    observed["payload_release_observed_at"] = payload_release_summary.get(
                        "payload_release_observed_at"
                    )
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append("payload_mass_not_materialized_in_world_sdf")
                observed = {
                    "source": "payload_model_world_sdf",
                    "observed": False,
                    "requested_payload_mass_kg": float(requested_mass),
                    "payload_mass_kg": applied_mass,
                    "world_sdf_hash_match": False,
                    "model_materialized": applied_mass is not None,
                    "payload_mass_materialized": False,
                    "world_sdf_sha256": world_sha256,
                }
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("payload_model_world_sdf_missing")
    capability_status = (
        "supported"
        if application_status == "applied"
        else "unsupported"
        if unsupported_reasons
        else "not_requested"
    )
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_payload_mass",
        "payload_mass": capability_status,
        "support_detection_method": (
            "payload_model_world_sdf" if requested_present else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "application_status": application_status,
        "requested_condition_ref": condition["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_vehicle_condition_evidence.v1",
        "evidence_id": "observed_vehicle_condition_evidence:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "observation_status": observation_status,
        "requested_condition_ref": condition["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "payload_release_does_not_verify_dropoff": True,
        "delivery_completion_claimed": False,
    }
    return {
        "vehicle_condition_profile": condition,
        "payload_simulator_capability_matrix": capability,
        "payload_simulator_condition_application": application,
        "observed_vehicle_condition_evidence": evidence,
    }


def _px4_param_show(param_name: str) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-param show {param_name}",
        ],
        check=False,
        timeout=5,
    )
    output = (result.stdout + result.stderr).strip()
    value = _listener_field(output, param_name)
    if value is None:
        match = re.search(
            rf"\b{re.escape(param_name)}(?:\s+\[[^\]]+\])?\s*:\s*(-?\d+(?:\.\d+)?)",
            output,
        )
        value = float(match.group(1)) if match else None
    if value is None:
        value = _listener_field(output, "value")
    return {
        "param": param_name,
        "returncode": result.returncode,
        "value": value,
        "output_tail": output[-500:],
    }


def _px4_param_set(param_name: str, value: float) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-param set {param_name} {value:.6f}",
        ],
        check=False,
        timeout=5,
    )
    return {
        "param": param_name,
        "requested_value": value,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def _thermal_weather_realism() -> dict[str, Any]:
    return _run_thermal_weather_realism(
        profile=_thermal_weather_requested_profile(),
        param_show=_px4_param_show,
        param_set=_px4_param_set,
        reset_battery_status_cache=_reset_battery_status_cache,
        battery_status_sample=_battery_status_sample,
    )


def _battery_realism() -> dict[str, Any]:
    profile = _battery_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    scenario = requested.get("battery_scenario")
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    capability_status = "not_requested"
    before_params: dict[str, Any] = {}
    set_results: list[dict[str, Any]] = []
    if requested_present:
        if scenario not in ("battery_low", "battery_critical"):
            unsupported_reasons.append("battery_scenario_unsupported")
            application_status = "unsupported"
            observation_status = "unsupported"
            capability_status = "unsupported"
        else:
            _reset_battery_status_cache()
            before_sample = _battery_status_sample()
            observed_remaining_percent = before_sample.get("battery_remaining_percent")
            requested_remaining = requested.get("requested_remaining_percent")
            threshold_percent = (
                float(observed_remaining_percent) + 5.0
                if observed_remaining_percent is not None
                else float(requested_remaining or 20.0) + 35.0
            )
            threshold = max(0.01, min(0.99, threshold_percent / 100.0))
            before_params = {
                "BAT_LOW_THR": _px4_param_show("BAT_LOW_THR"),
                "BAT_CRIT_THR": _px4_param_show("BAT_CRIT_THR"),
            }
            set_results.append(_px4_param_set("BAT_LOW_THR", threshold))
            if scenario == "battery_critical":
                set_results.append(_px4_param_set("BAT_CRIT_THR", threshold))
            applied_params = {item["param"]: item["requested_value"] for item in set_results}
            after_params = {
                "BAT_LOW_THR": _px4_param_show("BAT_LOW_THR"),
                "BAT_CRIT_THR": _px4_param_show("BAT_CRIT_THR"),
            }
            param_readback = {
                name: _px4_param_value_matches(after_params.get(name) or {}, value)
                for name, value in applied_params.items()
            }
            params_set = all(_px4_param_set_applied(item) for item in set_results)
            params_read_back = bool(param_readback) and all(param_readback.values())
            if params_set and params_read_back:
                application_status = "applied_with_approximations"
                capability_status = "supported"
                applied = {
                    "method": "px4_runtime_param_threshold_override",
                    "target": "px4_runtime_params",
                    "applied_params": applied_params,
                    "before_params": before_params,
                    "after_params": after_params,
                    "param_readback_matches_requested": param_readback,
                    "requested_remaining_percent": requested_remaining,
                    "requested_remaining_does_not_spoof_px4_battery_status": True,
                    "battery_remaining_target_materialized": False,
                    "battery_remaining_target_commitment": (
                        "not_materialized_as_px4_battery_status_remaining"
                    ),
                    "battery_warning_threshold_materialized": True,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                application_status = "unsupported"
                capability_status = "unsupported"
                observation_status = "unsupported"
                if not params_set:
                    unsupported_reasons.append("px4_battery_param_set_failed")
                if not params_read_back:
                    unsupported_reasons.append("px4_battery_param_readback_mismatch")
            _reset_battery_status_cache()
            time.sleep(1)
            after_sample = _battery_status_sample()
            observed_remaining = after_sample.get("battery_remaining_percent")
            observed_remaining_matches_requested = (
                requested_remaining is not None
                and observed_remaining is not None
                and math.isclose(
                    float(observed_remaining),
                    float(requested_remaining),
                    abs_tol=0.5,
                )
            )
            observed = {
                "source": "px4-listener:battery_status",
                "observed": after_sample.get("battery_status_observed") is True,
                "battery_status": after_sample,
                "requested_remaining_percent": requested_remaining,
                "observed_remaining_percent": observed_remaining,
                "observed_remaining_matches_requested": observed_remaining_matches_requested,
                "observed_warning": after_sample.get("battery_warning"),
                "requested_remaining_does_not_spoof_px4_battery_status": True,
                "battery_remaining_target_materialized": False,
                "battery_remaining_target_commitment": (
                    "not_materialized_as_px4_battery_status_remaining"
                ),
                "battery_warning_threshold_materialized": _application_status_is_materialized(
                    application_status
                ),
            }
            expected_warning = requested.get("requested_warning_level")
            observed["failsafe_behavior_status"] = (
                "not_requested_for_battery_low_warning"
                if scenario == "battery_low"
                else "unsupported_without_dedicated_critical_battery_recovery_smoke"
            )
            if (
                _application_status_is_materialized(application_status)
                and after_sample.get("battery_status_observed") is True
            ):
                observation_status = "battery_status_observed"
                if expected_warning is not None and (
                    after_sample.get("battery_warning") is None
                    or int(after_sample.get("battery_warning") or 0) < int(expected_warning)
                ):
                    observation_status = "battery_status_observed_warning_not_reached"
                    unsupported_reasons.append("px4_battery_warning_threshold_not_observed")
            elif _application_status_is_materialized(application_status):
                observation_status = "battery_status_not_observed"
                unsupported_reasons.append("px4_battery_status_not_observed")
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_battery_threshold",
        "battery_threshold": capability_status,
        "battery_failsafe_behavior": (
            "not_requested"
            if scenario == "battery_low"
            else "unsupported"
            if scenario == "battery_critical"
            else capability_status
        ),
        "support_detection_method": (
            "px4_param_set_and_battery_status_listener" if requested_present else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": (
            ["requested_remaining_percent_is_warning_threshold_input_not_px4_remaining_target"]
            if requested_present and scenario in ("battery_low", "battery_critical")
            else []
        ),
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": capability["approximation_reasons"],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_vehicle_condition_evidence.v1",
        "evidence_id": "observed_vehicle_condition_evidence:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_remaining_does_not_spoof_px4_battery_status": True,
        "delivery_completion_claimed": False,
    }
    return {
        "battery_condition_profile": profile,
        "battery_simulator_capability_matrix": capability,
        "battery_simulator_condition_application": application,
        "observed_battery_condition_evidence": evidence,
    }


def _latest_trace_battery_status() -> dict[str, Any] | None:
    if LIVE_POSE_TRACE_PATH is None or not LIVE_POSE_TRACE_PATH.exists():
        return None
    rows = LIVE_POSE_TRACE_PATH.read_text().splitlines()
    for row in reversed(rows):
        try:
            payload = json.loads(row)
        except json.JSONDecodeError:
            continue
        battery_status = payload.get("battery_status")
        if (
            isinstance(battery_status, dict)
            and battery_status.get("battery_status_observed") is True
        ):
            return battery_status
    return None


def _refresh_battery_realism_observation_from_trace() -> None:
    if not BATTERY_REALISM_SUMMARY:
        return
    profile = BATTERY_REALISM_SUMMARY.get("battery_condition_profile") or {}
    requested = profile.get("requested") or {}
    if profile.get("requested_present") is not True:
        return
    latest = _latest_trace_battery_status()
    if not latest:
        return
    application = BATTERY_REALISM_SUMMARY.get("battery_simulator_condition_application") or {}
    application_status = application.get("application_status")
    expected_warning = requested.get("requested_warning_level")
    observed_warning = latest.get("battery_warning")
    warning_reached = _application_status_is_materialized(application_status) and (
        expected_warning is None
        or (observed_warning is not None and int(observed_warning) >= int(expected_warning))
    )
    evidence = BATTERY_REALISM_SUMMARY.get("observed_battery_condition_evidence") or {}
    observed = dict(evidence.get("observed") or {})
    observed.update(
        {
            "source": "px4-listener:battery_status",
            "observed": True,
            "battery_status": latest,
            "requested_remaining_percent": requested.get("requested_remaining_percent"),
            "observed_remaining_percent": latest.get("battery_remaining_percent"),
            "observed_remaining_matches_requested": (
                requested.get("requested_remaining_percent") is not None
                and latest.get("battery_remaining_percent") is not None
                and math.isclose(
                    float(latest.get("battery_remaining_percent")),
                    float(requested.get("requested_remaining_percent")),
                    abs_tol=0.5,
                )
            ),
            "observed_warning": observed_warning,
            "requested_remaining_does_not_spoof_px4_battery_status": True,
            "battery_remaining_target_materialized": False,
            "battery_remaining_target_commitment": (
                "not_materialized_as_px4_battery_status_remaining"
            ),
            "battery_warning_threshold_materialized": _application_status_is_materialized(
                application_status
            ),
            "failsafe_behavior_status": (
                "not_requested_for_battery_low_warning"
                if requested.get("battery_scenario") == "battery_low"
                else "unsupported_without_dedicated_critical_battery_recovery_smoke"
            ),
        }
    )
    evidence["observed"] = observed
    if _application_status_is_materialized(application_status):
        evidence["observation_status"] = (
            "battery_status_observed"
            if warning_reached
            else "battery_status_observed_warning_not_reached"
        )
    else:
        evidence["observation_status"] = evidence.get("observation_status") or "unsupported"
    evidence["observed_at"] = datetime.now(timezone.utc).isoformat()
    BATTERY_REALISM_SUMMARY["observed_battery_condition_evidence"] = evidence
    if warning_reached:
        for key in (
            "battery_simulator_capability_matrix",
            "battery_simulator_condition_application",
        ):
            record = dict(BATTERY_REALISM_SUMMARY.get(key) or {})
            record["unsupported_reasons"] = [
                reason
                for reason in record.get("unsupported_reasons", [])
                if reason != "px4_battery_warning_threshold_not_observed"
            ]
            BATTERY_REALISM_SUMMARY[key] = record


def _sensor_gps_sample(*, timeout_seconds: int = 2) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"timeout {timeout_seconds} /opt/px4-gazebo/bin/px4-listener sensor_gps 1",
        ],
        check=False,
        timeout=timeout_seconds + 2,
    )
    output = (result.stdout + result.stderr).strip()
    observed = result.returncode == 0 and bool(output)
    return {
        "sensor_gps_observed": observed,
        "source": "px4-listener:sensor_gps",
        "returncode": result.returncode,
        "timestamp": _listener_field(output, "timestamp"),
        "satellites_used": _listener_field(output, "satellites_used"),
        "fix_type": _listener_field(output, "fix_type"),
        "output_tail": output[-500:],
    }


def _px4_failure_injection_command(
    component: str,
    failure_type: str,
) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-failure {component} {failure_type}",
        ],
        check=False,
        timeout=5,
    )
    combined = (result.stdout + result.stderr).strip()
    return {
        "component": component,
        "failure_type": failure_type,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
        "unsupported_message_observed": "unsupported" in combined.lower(),
    }


def _sensor_failure_realism() -> dict[str, Any]:
    return _run_sensor_failure_realism(
        profile=_sensor_failure_requested_profile(),
        param_set=_px4_param_set,
        sensor_gps_sample=_sensor_gps_sample,
    )


def _landing_zone_blocked_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    return _project_landing_zone_blocked_realism(
        requested=_landing_zone_blocked_requested(),
        payload_model_root=payload_model_root,
    )


def _visibility_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    return _project_visibility_realism(
        mode=_visibility_mode_request(),
        payload_model_root=payload_model_root,
    )


def _operational_no_fly_zone_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    return _project_operational_markers_realism(
        payload_model_root=payload_model_root,
        no_fly_zone_requested=_no_fly_zone_marker_requested(),
        traffic_conflict_requested=_traffic_conflict_marker_requested(),
        alternate_landing_requested=_alternate_landing_marker_requested(),
        rth_behavior_requested=_rth_behavior_requested(),
        moving_actor_requested=_moving_actor_marker_requested(),
        collision_obstacle_requested=_collision_obstacle_requested(),
        collision_contact_topic_requested=(_collision_obstacle_contact_topic_requested()),
        multi_drone_conflict_probe_requested=(_multi_drone_conflict_probe_requested()),
        moving_actor_motion_spec=_moving_actor_waypoint_motion_spec(),
        moving_actor_trajectory_definition_sha256=(
            _moving_actor_waypoint_trajectory_definition_sha256()
        ),
        collision_obstacle_motion_spec=_collision_obstacle_motion_spec(),
        collision_obstacle_contact_topic=COLLISION_OBSTACLE_CONTACT_TOPIC,
    )


def _vehicle_realism_summary_artifacts() -> dict[str, Any]:
    return {
        "vehicle_condition_profile": (VEHICLE_REALISM_SUMMARY or {}).get(
            "vehicle_condition_profile", {}
        ),
        "payload_simulator_capability_matrix": (VEHICLE_REALISM_SUMMARY or {}).get(
            "payload_simulator_capability_matrix", {}
        ),
        "payload_simulator_condition_application": (VEHICLE_REALISM_SUMMARY or {}).get(
            "payload_simulator_condition_application", {}
        ),
        "observed_vehicle_condition_evidence": (VEHICLE_REALISM_SUMMARY or {}).get(
            "observed_vehicle_condition_evidence", {}
        ),
        "battery_condition_profile": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_condition_profile", {}
        ),
        "battery_simulator_capability_matrix": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_simulator_capability_matrix", {}
        ),
        "battery_simulator_condition_application": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_simulator_condition_application", {}
        ),
        "observed_battery_condition_evidence": (BATTERY_REALISM_SUMMARY or {}).get(
            "observed_battery_condition_evidence", {}
        ),
        "thermal_weather_condition_profile": (THERMAL_WEATHER_REALISM_SUMMARY or {}).get(
            "thermal_weather_condition_profile", {}
        ),
        "thermal_weather_simulator_capability_matrix": (THERMAL_WEATHER_REALISM_SUMMARY or {}).get(
            "thermal_weather_simulator_capability_matrix", {}
        ),
        "thermal_weather_simulator_condition_application": (
            THERMAL_WEATHER_REALISM_SUMMARY or {}
        ).get("thermal_weather_simulator_condition_application", {}),
        "observed_thermal_weather_evidence": (THERMAL_WEATHER_REALISM_SUMMARY or {}).get(
            "observed_thermal_weather_evidence", {}
        ),
        "sensor_condition_profile": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_condition_profile", {}
        ),
        "sensor_simulator_capability_matrix": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_simulator_capability_matrix", {}
        ),
        "sensor_failure_injection_application": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_failure_injection_application", {}
        ),
        "observed_sensor_condition_evidence": (SENSOR_REALISM_SUMMARY or {}).get(
            "observed_sensor_condition_evidence", {}
        ),
        "gazebo_world_condition_profile": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_condition_profile", {}
        ),
        "gazebo_world_capability_matrix": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_capability_matrix", {}
        ),
        "gazebo_world_application": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_application", {}
        ),
        "obstacle_manifest": (WORLD_REALISM_SUMMARY or {}).get("obstacle_manifest", {}),
        "observed_world_condition_evidence": (WORLD_REALISM_SUMMARY or {}).get(
            "observed_world_condition_evidence", {}
        ),
        "visibility_condition_profile": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_condition_profile", {}
        ),
        "visibility_capability_matrix": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_capability_matrix", {}
        ),
        "visibility_application": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_application", {}
        ),
        "observed_visibility_condition_evidence": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "observed_visibility_condition_evidence", {}
        ),
        "operational_condition_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_condition_profile", {}
        ),
        "geofence_condition_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "geofence_condition_profile", {}
        ),
        "traffic_conflict_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "traffic_conflict_profile", {}
        ),
        "alternate_landing_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "alternate_landing_profile", {}
        ),
        "dynamic_actor_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "dynamic_actor_profile", {}
        ),
        "collision_obstacle_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "collision_obstacle_profile", {}
        ),
        "gazebo_route_corridor_obstacle_spawn_application": (
            _gazebo_route_corridor_obstacle_spawn_application_realism()
        ).get("gazebo_route_corridor_obstacle_spawn_application", {}),
        "multi_vehicle_frame_contract": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "multi_vehicle_frame_contract", {}
        ),
        "moving_actor_pose_observation": (MOVING_ACTOR_POSE_SUMMARY or {}).get(
            "moving_actor_pose_observation", {}
        ),
        "moving_actor_waypoint_motion_application": (MOVING_ACTOR_LINEAR_MOTION_SUMMARY or {}).get(
            "moving_actor_waypoint_motion_application", {}
        ),
        "moving_actor_proximity_evidence": (MOVING_ACTOR_PROXIMITY_SUMMARY or {}).get(
            "moving_actor_proximity_evidence", {}
        ),
        "collision_obstacle_evidence": (COLLISION_OBSTACLE_SUMMARY or {}).get(
            "collision_obstacle_evidence", {}
        ),
        "route_blocking_candidate_evidence": (ROUTE_BLOCKING_CANDIDATE_SUMMARY or {}).get(
            "route_blocking_candidate_evidence", {}
        ),
        "horizontal_route_contact_topic_integration": (HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}).get(
            "horizontal_route_contact_topic_integration", {}
        ),
        "horizontal_route_contact_event_incident_evidence": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_event_incident_evidence", {}),
        "horizontal_route_contact_operational_incident_report": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_operational_incident_report", {}),
        "horizontal_route_contact_scoped_verifier_candidate": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_scoped_verifier_candidate", {}),
        "horizontal_route_contact_incident_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_incident_verification", {}),
        "horizontal_route_incident_informed_traffic_conflict_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_incident_informed_traffic_conflict_verification", {}),
        "horizontal_route_incident_informed_route_blocking_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_incident_informed_route_blocking_verification", {}),
        "operational_incident_report": (OPERATIONAL_INCIDENT_REPORT_SUMMARY or {}).get(
            "operational_incident_report", {}
        ),
        "traffic_conflict_verification": (TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {}).get(
            "traffic_conflict_verification", {}
        ),
        "route_blocking_verification": (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
            "route_blocking_verification", {}
        ),
        "alternate_landing_candidate_evidence": (ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}).get(
            "alternate_landing_candidate_evidence", {}
        ),
        "alternate_landing_execution_request": (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
            "alternate_landing_execution_request", {}
        ),
        "alternate_mission_upload_request": (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
            "alternate_mission_upload_request", {}
        ),
        "alternate_mission_upload_receipt": (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
            "alternate_mission_upload_receipt", {}
        ),
        "alternate_route_behavior_observation": (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
            "alternate_route_behavior_observation", {}
        ),
        "alternate_route_command_dispatch": (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
            "alternate_route_command_dispatch", {}
        ),
        "alternate_route_execution_evidence": (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
            "alternate_route_execution_evidence", {}
        ),
        "alternate_landing_command_dispatch": (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
            "alternate_landing_command_dispatch", {}
        ),
        "alternate_landing_behavior_observation": (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
            "alternate_landing_behavior_observation", {}
        ),
        "alternate_landing_outcome": (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
            "alternate_landing_outcome", {}
        ),
        "rth_execution_request": (RTH_BEHAVIOR_SUMMARY or {}).get("rth_execution_request", {}),
        "rth_command_dispatch": (RTH_BEHAVIOR_SUMMARY or {}).get("rth_command_dispatch", {}),
        "rth_behavior_observation": (RTH_BEHAVIOR_SUMMARY or {}).get(
            "rth_behavior_observation", {}
        ),
        "rth_outcome": (RTH_BEHAVIOR_SUMMARY or {}).get("rth_outcome", {}),
        "operational_capability_matrix": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_capability_matrix", {}
        ),
        "operational_application": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_application", {}
        ),
        "observed_operational_condition_evidence": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "observed_operational_condition_evidence", {}
        ),
        "telemetry_degradation_profile": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_degradation_profile", {}
        ),
        "telemetry_degradation_application": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_degradation_application", {}
        ),
        "observed_telemetry_gap_evidence": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "observed_telemetry_gap_evidence", {}
        ),
        "telemetry_freshness_report": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_freshness_report", {}
        ),
        "mavlink_link_degradation_profile": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "mavlink_link_degradation_profile", {}
        ),
        "mavlink_link_degradation_capability_matrix": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "mavlink_link_degradation_capability_matrix", {}
        ),
        "mavlink_link_degradation_application": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "mavlink_link_degradation_application", {}
        ),
        "observed_mavlink_gap_evidence": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "observed_mavlink_gap_evidence", {}
        ),
        "terrain_world_readback": TERRAIN_WORLD_REALISM_SUMMARY or {},
    }


def _gazebo_route_corridor_obstacle_spawn_application_realism() -> dict[str, Any]:
    profile = (OPERATIONAL_REALISM_SUMMARY or {}).get("collision_obstacle_profile", {})
    application = (OPERATIONAL_REALISM_SUMMARY or {}).get("operational_application", {})
    evidence = (OPERATIONAL_REALISM_SUMMARY or {}).get(
        "observed_operational_condition_evidence", {}
    )
    requested = _collision_obstacle_requested()
    profile_obstacles = profile.get("obstacles") if isinstance(profile, dict) else []
    obstacle = (
        profile_obstacles[0] if isinstance(profile_obstacles, list) and profile_obstacles else {}
    )
    fallback_motion = _collision_obstacle_motion_spec()
    applied = application.get("applied") if isinstance(application, dict) else {}
    observed = evidence.get("observed") if isinstance(evidence, dict) else {}
    model_names = applied.get("model_names") if isinstance(applied, dict) else []
    applied_world_sdf_path = str(applied.get("world_sdf_path", ""))
    applied_world_sdf_sha256 = str(applied.get("world_sdf_sha256", ""))
    observed_world_sdf_sha256 = str(observed.get("world_sdf_sha256", ""))
    world_sdf_hash_match = bool(
        applied_world_sdf_path
        and applied_world_sdf_sha256
        and observed_world_sdf_sha256
        and applied_world_sdf_sha256 == observed_world_sdf_sha256
    )
    model_materialized = (
        requested
        and isinstance(model_names, list)
        and "mission_designer_collision_obstacle" in model_names
        and world_sdf_hash_match
        and observed.get("collision_obstacle_name") == "mission_designer_collision_obstacle"
        and bool(observed.get("collision_obstacle_collision_present"))
        and bool(observed.get("collision_obstacle_trajectory_follower_present"))
    )
    unsupported_reasons: list[str] = []
    if not requested:
        application_status = "not_requested"
    elif model_materialized:
        application_status = "applied"
    else:
        application_status = "unsupported"
        unsupported_reasons.append("gazebo_collision_obstacle_model_not_materialized")
        if not applied_world_sdf_path:
            unsupported_reasons.append("world_sdf_path_missing")
        if not world_sdf_hash_match:
            unsupported_reasons.append("world_sdf_hash_mismatch_or_missing")
    return {
        "gazebo_route_corridor_obstacle_spawn_application": {
            "schema_version": "gazebo_route_corridor_obstacle_spawn_application.v1",
            "application_id": (
                "gazebo_route_corridor_obstacle_spawn_application:"
                "mission_designer_collision_obstacle"
            ),
            "condition_kind": "gazebo_route_corridor_collision_obstacle_spawn",
            "application_status": application_status,
            "observation_status": application_status,
            "requested_present": requested,
            "requested": {
                "source": "mission_designer_coordinate_route",
                "obstacle_id": "mission_designer_collision_obstacle",
                "frame": "gazebo_world_local",
                "start_xy_m": obstacle.get("start_xy_m") or fallback_motion["start_xy_m"],
                "end_xy_m": obstacle.get("end_xy_m") or fallback_motion["end_xy_m"],
                "collision_enabled": requested,
                "trajectory_follower_requested": requested,
            },
            "applied": {
                "method": (
                    "gazebo_world_sdf_model_injection_before_sitl_start"
                    if model_materialized
                    else ""
                ),
                "world_sdf_path": applied_world_sdf_path,
                "world_sdf_sha256": applied_world_sdf_sha256,
                "model_name": ("mission_designer_collision_obstacle" if model_materialized else ""),
                "collision_name": ("collision_obstacle_collision" if model_materialized else ""),
                "trajectory_follower_plugin_enabled": bool(
                    observed.get("collision_obstacle_trajectory_follower_present")
                ),
                "contact_sensor_enabled": bool(
                    observed.get("collision_obstacle_contact_sensor_present")
                ),
                "contact_topic": observed.get("collision_obstacle_contact_topic", ""),
            },
            "observed": {
                "source": "gazebo_world_sdf",
                "observed": model_materialized,
                "world_sdf_hash_match": world_sdf_hash_match,
                "model_materialized": bool(
                    observed.get("collision_obstacle_name") == "mission_designer_collision_obstacle"
                ),
                "collision_geometry_materialized": bool(
                    observed.get("collision_obstacle_collision_present")
                ),
                "trajectory_follower_materialized": bool(
                    observed.get("collision_obstacle_trajectory_follower_present")
                ),
                "world_sdf_sha256": observed_world_sdf_sha256,
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "incident_verified": False,
                "auto_gate": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "unsupported_reasons": unsupported_reasons,
            "simulator_applicator": True,
            "verifier": False,
            "behavior_reactor": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _telemetry_observer_dropout_realism() -> dict[str, Any]:
    mode = _telemetry_dropout_mode_request()
    requested_present = bool(mode)
    supported = mode == "observer_sample_pause"
    unsupported_reasons = (
        [] if (not requested_present or supported) else ["telemetry_dropout_mode_not_supported"]
    )
    gap_events = list(TELEMETRY_DROPOUT_EVENTS)
    sample_events = list(TELEMETRY_OBSERVER_SAMPLE_EVENTS)
    gap_durations = [float(event.get("gap_duration_seconds") or 0.0) for event in gap_events]
    max_gap_seconds = max(gap_durations) if gap_durations else 0.0
    gap_count = len(gap_events)
    first_pause_index = min(
        (
            int(event.get("sample_index"))
            for event in gap_events
            if event.get("sample_index") is not None
        ),
        default=None,
    )
    observed_sample_indexes = [
        int(event.get("sample_index"))
        for event in sample_events
        if event.get("event") == "observer_sample_observed"
        and event.get("sample_index") is not None
    ]
    baseline_observer_sample_observed = (
        requested_present
        and supported
        and first_pause_index is not None
        and any(index < first_pause_index for index in observed_sample_indexes)
    )
    observer_sample_pause_performed = requested_present and supported and bool(gap_events)
    observer_sample_gap_observed = observer_sample_pause_performed and gap_count > 0
    post_pause_observer_sample_observed = (
        requested_present
        and supported
        and first_pause_index is not None
        and any(index > first_pause_index for index in observed_sample_indexes)
    )
    observer_sample_pause_observed = (
        baseline_observer_sample_observed
        and observer_sample_pause_performed
        and observer_sample_gap_observed
        and post_pause_observer_sample_observed
    )
    if requested_present and supported:
        if not baseline_observer_sample_observed:
            unsupported_reasons.append("telemetry_observer_baseline_sample_not_observed")
        if not observer_sample_pause_performed:
            unsupported_reasons.append("telemetry_observer_sample_pause_not_performed")
        if not observer_sample_gap_observed:
            unsupported_reasons.append("telemetry_observer_sample_gap_not_observed")
        if not post_pause_observer_sample_observed:
            unsupported_reasons.append("telemetry_observer_post_pause_sample_not_observed")
    application_status = (
        "applied"
        if observer_sample_pause_observed
        else "unsupported"
        if requested_present
        else "not_requested"
    )
    observation_status = (
        "observer_sample_pause_gap_observed"
        if observer_sample_pause_observed
        else (
            "observer_sample_pause_gap_not_observed"
            if requested_present and supported
            else "unsupported"
            if requested_present
            else "not_requested"
        )
    )
    requested_condition_ref = "telemetry_degradation_profile:mission_designer_observer_dropout"
    application_ref = "telemetry_degradation_application:mission_designer_observer_dropout"
    profile = {
        "schema_version": "telemetry_degradation_profile.v1",
        "condition_id": requested_condition_ref,
        "condition_kind": "observer_side_telemetry_dropout",
        "requested": {
            "telemetry_dropout_mode": mode or None,
            "affected_streams": ["pose_samples"] if requested_present else [],
            "observer_side_only": True,
            "publisher_transport_loss_claimed": False,
            "vehicle_recovery_behavior_claimed": False,
            "mission_failure_claimed": False,
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    application = {
        "schema_version": "telemetry_degradation_application.v1",
        "application_id": application_ref,
        "condition_kind": "observer_side_telemetry_dropout",
        "application_status": application_status,
        "requested_condition_ref": requested_condition_ref,
        "applied": (
            {
                "method": "mission_os_observer_pose_sample_pause",
                "mode": "observer_sample_pause",
                "affected_streams": ["pose_samples"],
                "gap_event_count": gap_count,
                "baseline_observer_sample_observed": baseline_observer_sample_observed,
                "observer_sample_pause_performed": observer_sample_pause_performed,
                "observer_sample_gap_observed": observer_sample_gap_observed,
                "post_pause_observer_sample_observed": post_pause_observer_sample_observed,
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
                "publisher_transport_loss_claimed": False,
                "vehicle_recovery_behavior_claimed": False,
                "mission_failure_claimed": False,
                "px4_command_path_mutated": False,
                "gazebo_command_path_mutated": False,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            if observer_sample_pause_observed
            else {}
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": (
            ["observer_sample_pause_only_consumer_side"] if requested_present and supported else []
        ),
        "observer_process_mutated": False,
        "publisher_state_mutated": False,
        "mission_upload_path_mutated": False,
        "mission_progress_mutated": False,
        "publisher_transport_loss_claimed": False,
        "vehicle_recovery_behavior_claimed": False,
        "mission_failure_claimed": False,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_telemetry_gap_evidence.v1",
        "evidence_id": "observed_telemetry_gap_evidence:mission_designer_observer_dropout",
        "condition_kind": "observer_side_telemetry_dropout",
        "observation_status": observation_status,
        "requested_condition_ref": requested_condition_ref,
        "application_ref": application_ref,
        "observed": {
            "max_gap_seconds": max_gap_seconds,
            "gap_count": gap_count,
            "missing_sample_count": sum(
                int(event.get("missing_sample_count") or 0) for event in gap_events
            ),
            "affected_streams": ["pose_samples"] if requested_present else [],
            "gap_events": gap_events,
            "sample_events": sample_events,
            "baseline_observer_sample_observed": baseline_observer_sample_observed,
            "observer_sample_pause_performed": observer_sample_pause_performed,
            "observer_sample_gap_observed": observer_sample_gap_observed,
            "post_pause_observer_sample_observed": post_pause_observer_sample_observed,
            "publisher_state_mutated": False,
            "mission_upload_path_mutated": False,
            "mission_progress_mutated": False,
            "publisher_transport_loss_observed": False,
            "vehicle_recovery_behavior_observed": False,
            "mission_failure_claimed": False,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    freshness = {
        "schema_version": "telemetry_freshness_report.v1",
        "report_id": "telemetry_freshness_report:mission_designer_observer_dropout",
        "condition_kind": "observer_side_telemetry_dropout",
        "freshness_status": (
            "gap_observed"
            if gap_count
            else (
                "no_gap_observed"
                if requested_present and supported
                else "unsupported"
                if requested_present
                else "not_requested"
            )
        ),
        "max_gap_seconds": max_gap_seconds,
        "gap_count": gap_count,
        "affected_streams": ["pose_samples"] if requested_present else [],
        "stale_telemetry_is_not_current_telemetry": True,
        "observer_dropout_does_not_claim_publisher_transport_loss": True,
        "observer_dropout_does_not_fail_task_status": True,
        "delivery_completion_claimed": False,
    }
    return {
        "telemetry_degradation_profile": profile,
        "telemetry_degradation_application": application,
        "observed_telemetry_gap_evidence": evidence,
        "telemetry_freshness_report": freshness,
    }


def _mavlink_link_degradation_realism() -> dict[str, Any]:
    mode = _mavlink_link_degradation_mode_request()
    requested_present = bool(mode)
    known_request = mode in (
        "",
        "bounded_link_loss",
        "link_loss_probe",
        "heartbeat_observer",
    )
    unsupported_reasons: list[str] = []
    if requested_present and not known_request:
        unsupported_reasons.append("mavlink_link_degradation_mode_not_supported")
    elif mode == "link_loss_probe":
        unsupported_reasons.extend(
            [
                "safe_mavlink_link_loss_applicator_not_implemented",
                "observer_dropout_not_a_mavlink_link_loss_proxy",
            ]
        )
    heartbeat_observation: dict[str, Any] = {}
    link_loss_application: dict[str, Any] = {}
    if mode == "heartbeat_observer":
        heartbeat_observation = _observe_mavlink_heartbeat_gap()
    elif mode == "bounded_link_loss":
        link_loss_application = _apply_bounded_mavlink_link_loss()
    bounded_stop_restart_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("applicator_status") == "completed"
        and link_loss_application.get("endpoint_stop_performed") is True
        and link_loss_application.get("endpoint_restart_performed") is True
    )
    bounded_baseline_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("baseline_heartbeat_observed") is True
    )
    bounded_gap_observed = (
        mode == "bounded_link_loss" and link_loss_application.get("heartbeat_gap_observed") is True
    )
    bounded_restart_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("post_restart_heartbeat_observed") is True
    )
    bounded_endpoint_interruption_observed = (
        bounded_stop_restart_observed
        and bounded_baseline_observed
        and bounded_gap_observed
        and bounded_restart_observed
    )
    if mode == "bounded_link_loss":
        if not bounded_stop_restart_observed:
            unsupported_reasons.append("mavlink_endpoint_stop_restart_not_observed")
        if not bounded_baseline_observed:
            unsupported_reasons.append("mavlink_baseline_heartbeat_not_observed")
        if not bounded_gap_observed:
            unsupported_reasons.append("mavlink_heartbeat_gap_not_observed")
        if not bounded_restart_observed:
            unsupported_reasons.append("mavlink_post_restart_heartbeat_not_observed")
    capability_status = (
        "supported_bounded_sitl_applicator"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "supported_read_only_observer"
            if mode == "heartbeat_observer"
            else "unsupported"
            if requested_present
            else "not_requested"
        )
    )
    application_status = (
        "applied"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "unsupported"
            if mode == "bounded_link_loss"
            else "observed"
            if mode == "heartbeat_observer"
            else capability_status
        )
    )
    observation_status = (
        "bounded_link_loss_gap_observed"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "bounded_link_loss_unsupported"
            if mode == "bounded_link_loss"
            else (
                "heartbeat_gap_observed"
                if heartbeat_observation.get("heartbeat_gap_observed") is True
                else (
                    "heartbeat_observed_no_gap"
                    if mode == "heartbeat_observer"
                    and heartbeat_observation.get("heartbeat_count", 0) > 0
                    else (
                        "heartbeat_not_observed"
                        if mode == "heartbeat_observer"
                        else "unsupported"
                        if requested_present
                        else "not_requested"
                    )
                )
            )
        )
    )
    requested_condition_ref = "mavlink_link_degradation_profile:mission_designer_link_probe"
    application_ref = "mavlink_link_degradation_application:mission_designer_link_probe"
    profile = {
        "schema_version": "mavlink_link_degradation_profile.v1",
        "condition_id": requested_condition_ref,
        "condition_kind": "mavlink_link_degradation",
        "requested": {
            "mavlink_link_degradation_mode": mode or None,
            "requested_link_loss": mode in ("bounded_link_loss", "link_loss_probe"),
            "requested_bounded_link_loss": mode == "bounded_link_loss",
            "requested_heartbeat_observer": mode == "heartbeat_observer",
            "observer_side_dropout_requested": False,
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    capability = {
        "schema_version": "mavlink_link_degradation_capability_matrix.v1",
        "capability_id": ("mavlink_link_degradation_capability_matrix:mission_designer_link_probe"),
        "mavlink_link_loss": (
            "supported_bounded_sitl_applicator"
            if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
            else "unsupported"
            if requested_present
            else "not_requested"
        ),
        "heartbeat_gap_observer": (
            "supported_read_only_observer" if mode == "heartbeat_observer" else "not_requested"
        ),
        "support_detection_method": (
            "px4_mavlink_stop_restart_bounded_sitl"
            if mode == "bounded_link_loss"
            else (
                "read_only_udp_heartbeat_observer"
                if mode == "heartbeat_observer"
                else (
                    "mission_designer_allowlist_check_no_safe_link_loss_applicator"
                    if requested_present
                    else "not_requested"
                )
            )
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "mavlink_link_degradation_application.v1",
        "application_id": application_ref,
        "condition_kind": "mavlink_link_degradation",
        "application_status": application_status,
        "requested_condition_ref": requested_condition_ref,
        "applied": (
            {
                "method": "px4_mavlink_stop_restart_bounded_sitl",
                "scope": "all_px4_mavlink_instances_stop_restart",
                "stop_command": "px4-mavlink stop-all",
                "restart_scope": "route_and_emergency_mavlink_instances",
                "source": link_loss_application.get(
                    "source", f"udp://127.0.0.1:{ROUTE_MAVLINK_LOCAL_PORT}"
                ),
                "duration_seconds": link_loss_application.get("duration_seconds", 0.0),
                "gap_threshold_seconds": link_loss_application.get("gap_threshold_seconds", 0.0),
                "endpoint_stop_performed": link_loss_application.get("endpoint_stop_performed")
                is True,
                "endpoint_restart_performed": link_loss_application.get(
                    "endpoint_restart_performed"
                )
                is True,
                "emergency_endpoint_restart_requested": link_loss_application.get(
                    "emergency_endpoint_restart_requested"
                )
                is True,
                "baseline_heartbeat_observed": bounded_baseline_observed,
                "heartbeat_gap_observed": bounded_gap_observed,
                "post_restart_heartbeat_observed": bounded_restart_observed,
                "observer_sent_packets": False,
                "packet_drop_performed": False,
                "rf_link_loss_claimed": False,
                "vehicle_failsafe_claimed": False,
            }
            if mode == "bounded_link_loss"
            else (
                {
                    "method": "read_only_udp_heartbeat_observer",
                    "source": heartbeat_observation.get("source", "udp://127.0.0.1:14650"),
                    "duration_seconds": heartbeat_observation.get("duration_seconds", 0.0),
                    "gap_threshold_seconds": heartbeat_observation.get(
                        "gap_threshold_seconds", 0.0
                    ),
                    "observer_sent_packets": False,
                    "packet_drop_performed": False,
                }
                if mode == "heartbeat_observer"
                else {}
            )
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "mavlink_link_loss_claimed": mode == "bounded_link_loss"
        and bounded_endpoint_interruption_observed,
        "bounded_sitl_endpoint_link_loss_claimed": mode == "bounded_link_loss"
        and bounded_endpoint_interruption_observed,
        "rf_link_loss_claimed": False,
        "heartbeat_gap_observer_requested": mode == "heartbeat_observer",
        "px4_command_path_mutated": mode == "bounded_link_loss",
        "px4_mavlink_endpoint_mutated": mode == "bounded_link_loss",
        "gazebo_command_path_mutated": False,
        "mission_upload_path_mutated": mode == "bounded_link_loss",
        "mission_upload_interruption_observed": False,
        "packet_drop_performed": False,
        "observer_dropout_used_as_proxy": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_mavlink_gap_evidence.v1",
        "evidence_id": "observed_mavlink_gap_evidence:mission_designer_link_probe",
        "condition_kind": "mavlink_link_degradation",
        "observation_status": observation_status,
        "requested_condition_ref": requested_condition_ref,
        "application_ref": application_ref,
        "observed": {
            "mavlink_link_loss_observed": mode == "bounded_link_loss"
            and bounded_endpoint_interruption_observed,
            "bounded_sitl_endpoint_link_loss_observed": mode == "bounded_link_loss"
            and bounded_endpoint_interruption_observed,
            "rf_link_loss_observed": False,
            "heartbeat_observer_status": (
                heartbeat_observation.get("observer_status")
                if mode == "heartbeat_observer"
                else (
                    link_loss_application.get("applicator_status")
                    if mode == "bounded_link_loss"
                    else None
                )
            ),
            "heartbeat_count": int(
                (
                    heartbeat_observation if mode == "heartbeat_observer" else link_loss_application
                ).get("heartbeat_count")
                or 0
            ),
            "baseline_heartbeat_observed": bounded_baseline_observed,
            "warmup_heartbeat_count": int(link_loss_application.get("warmup_heartbeat_count") or 0),
            "interruption_heartbeat_count": int(
                link_loss_application.get("interruption_heartbeat_count") or 0
            ),
            "post_restart_heartbeat_count": int(
                link_loss_application.get("post_restart_heartbeat_count") or 0
            ),
            "post_restart_heartbeat_observed": bounded_restart_observed,
            "heartbeat_gap_observed": (
                heartbeat_observation if mode == "heartbeat_observer" else link_loss_application
            ).get("heartbeat_gap_observed")
            is True,
            "heartbeat_gap_count": int(
                (
                    heartbeat_observation if mode == "heartbeat_observer" else link_loss_application
                ).get("heartbeat_gap_count")
                or 0
            ),
            "max_heartbeat_interval_seconds": float(
                (
                    heartbeat_observation if mode == "heartbeat_observer" else link_loss_application
                ).get("max_heartbeat_interval_seconds")
                or 0.0
            ),
            "gap_threshold_seconds": float(
                (
                    heartbeat_observation if mode == "heartbeat_observer" else link_loss_application
                ).get("gap_threshold_seconds")
                or 0.0
            ),
            "endpoint_stop_performed": link_loss_application.get("endpoint_stop_performed") is True,
            "endpoint_restart_performed": link_loss_application.get("endpoint_restart_performed")
            is True,
            "mission_upload_interruption_observed": False,
            "vehicle_failsafe_observed": False,
            "observer_dropout_used_as_proxy": False,
            "packet_drop_performed": False,
            "px4_mavlink_endpoint_mutated": mode == "bounded_link_loss",
            "source": (
                heartbeat_observation.get("source")
                if mode == "heartbeat_observer"
                else (
                    link_loss_application.get("source")
                    if mode == "bounded_link_loss"
                    else "support_detection_only"
                )
            ),
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "mavlink_link_degradation_profile": profile,
        "mavlink_link_degradation_capability_matrix": capability,
        "mavlink_link_degradation_application": application,
        "observed_mavlink_gap_evidence": evidence,
    }


def _battery_status_sample() -> dict[str, Any]:
    global _LAST_BATTERY_STATUS_SAMPLE_AT
    global _LAST_BATTERY_STATUS_SAMPLE

    now = time.monotonic()
    if (
        _LAST_BATTERY_STATUS_SAMPLE_AT > 0
        and now - _LAST_BATTERY_STATUS_SAMPLE_AT < BATTERY_STATUS_SAMPLE_INTERVAL_SECONDS
    ):
        return dict(_LAST_BATTERY_STATUS_SAMPLE)

    try:
        result = _run(
            [
                "docker",
                "exec",
                CONTAINER_NAME,
                "/opt/px4-gazebo/bin/px4-listener",
                "battery_status",
                "1",
            ],
            check=False,
            timeout=BATTERY_STATUS_SAMPLE_TIMEOUT_SECONDS,
        )
    except Exception:
        _LAST_BATTERY_STATUS_SAMPLE_AT = now
        _LAST_BATTERY_STATUS_SAMPLE = {
            "battery_status_observed": False,
            "battery_state_source": "px4-listener:battery_status",
        }
        return dict(_LAST_BATTERY_STATUS_SAMPLE)
    output = (result.stdout + result.stderr).strip()
    _LAST_BATTERY_STATUS_SAMPLE_AT = now
    _LAST_BATTERY_STATUS_SAMPLE = _battery_status_from_listener_output(
        output,
        returncode=result.returncode,
    )
    return dict(_LAST_BATTERY_STATUS_SAMPLE)


def _append_live_pose_row(
    phase: str,
    sample: dict[str, float],
    *,
    sample_index: int | None = None,
) -> None:
    if LIVE_POSE_TRACE_PATH is None:
        return
    telemetry_dropout_mode = _telemetry_dropout_mode_request()
    if (
        telemetry_dropout_mode == "observer_sample_pause"
        and phase == "route"
        and sample_index is not None
        and sample_index > 0
        and sample_index % 5 == 0
    ):
        gap_started_at = datetime.now(timezone.utc).isoformat()
        gap_event = {
            "phase": "telemetry_gap",
            "gap_reason": "observer_sample_pause",
            "gap_started_at": gap_started_at,
            "gap_duration_seconds": 2.0,
            "missing_sample_count": 1,
            "affected_streams": ["pose_samples"],
            "sample_index": sample_index,
            "publisher_state_mutated": False,
            "mission_upload_path_mutated": False,
            "mission_progress_mutated": False,
            "publisher_transport_loss_claimed": False,
            "vehicle_recovery_behavior_claimed": False,
            "mission_failure_claimed": False,
            "delivery_completion_claimed": False,
            "observer_side_only": True,
            "observed_at": gap_started_at,
        }
        TELEMETRY_DROPOUT_EVENTS.append(gap_event)
        TELEMETRY_OBSERVER_SAMPLE_EVENTS.append(
            {
                "event": "observer_sample_pause",
                "sample_index": sample_index,
                "observed_at": gap_started_at,
                "affected_streams": ["pose_samples"],
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
            }
        )
        with LIVE_POSE_TRACE_PATH.open("a") as handle:
            handle.write(json.dumps(gap_event, sort_keys=True) + "\n")
        return
    row: dict[str, Any] = {
        "phase": phase,
        "sample": sample,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    battery_status = _battery_status_sample()
    if battery_status.get("battery_status_observed") is True:
        row["battery_status"] = battery_status
    if sample_index is not None:
        row["sample_index"] = sample_index
    if telemetry_dropout_mode == "observer_sample_pause" and phase == "route":
        TELEMETRY_OBSERVER_SAMPLE_EVENTS.append(
            {
                "event": "observer_sample_observed",
                "sample_index": sample_index,
                "observed_at": row["observed_at"],
                "affected_streams": ["pose_samples"],
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
            }
        )
    with LIVE_POSE_TRACE_PATH.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _wait_for_startup(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _logs()
        if (
            "Gazebo world is ready" in logs
            and "gz_bridge] world: default, model: x500_0" in logs
            and "Startup script returned successfully" in logs
        ):
            return
        time.sleep(1)
    raise RuntimeError("timed out waiting for PX4/Gazebo horizontal route startup")


def _wait_for_px4_home(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "home set" in _logs():
            return
        time.sleep(1)
    raise RuntimeError("timed out waiting for PX4 home set")


def _pose_sample() -> dict[str, float]:
    sample = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        timeout=10,
    ).stdout
    pose = parse_gz_sim_entity_pose(sample, entity_name="x500_0")
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _payload_pose_sample() -> dict[str, float]:
    sample = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        timeout=10,
    ).stdout
    pose = parse_gz_sim_entity_pose(sample, entity_name="delivery_payload")
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _moving_actor_pose_sample() -> dict[str, float]:
    sample = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        timeout=10,
    ).stdout
    pose = parse_gz_sim_entity_pose(
        sample,
        entity_name="mission_designer_moving_actor_marker",
    )
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _moving_actor_waypoint_motion_application_realism() -> dict[str, Any]:
    return _run_moving_actor_waypoint_motion_application(
        requested=_moving_actor_marker_requested(),
        motion_spec=_moving_actor_waypoint_motion_spec(),
        trajectory_definition_sha256=(_moving_actor_waypoint_trajectory_definition_sha256()),
        operational_realism_summary=OPERATIONAL_REALISM_SUMMARY,
        pose_sample=_moving_actor_pose_sample,
        sleep=time.sleep,
    )


def _collision_obstacle_pose_sample() -> dict[str, float]:
    sample = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        timeout=10,
    ).stdout
    pose = parse_gz_sim_entity_pose(
        sample,
        entity_name="mission_designer_collision_obstacle",
    )
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _collision_obstacle_contact_topic_observation() -> dict[str, Any]:
    topic_list = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -l",
        ],
        check=False,
        timeout=10,
    ).stdout
    selected_topic = _select_contact_topic(
        topic_list,
        configured_topic=COLLISION_OBSTACLE_CONTACT_TOPIC,
    )
    sample_result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (f"timeout 2 gz topic -e -t {shlex.quote(selected_topic)} -n 1"),
        ],
        check=False,
        timeout=5,
    )
    return _contact_topic_observation(
        topic_list=topic_list,
        sample_text=sample_result.stdout.strip(),
        sample_returncode=sample_result.returncode,
        configured_topic=COLLISION_OBSTACLE_CONTACT_TOPIC,
    )


def _moving_actor_pose_observation_realism() -> dict[str, Any]:
    return _observe_moving_actor_pose(
        requested=_moving_actor_marker_requested(),
        pose_sample=_moving_actor_pose_sample,
        sleep=time.sleep,
    )


def _collision_obstacle_evidence_realism(
    *,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
) -> dict[str, Any]:
    spawn_application = _gazebo_route_corridor_obstacle_spawn_application_realism().get(
        "gazebo_route_corridor_obstacle_spawn_application",
        {},
    )
    spawn_verified, spawn_fail_reasons = _route_corridor_obstacle_application_source_check(
        spawn_application
    )
    return _run_collision_obstacle_evidence(
        requested=_collision_obstacle_requested(),
        obstacle_profile=(OPERATIONAL_REALISM_SUMMARY or {}).get(
            "collision_obstacle_profile",
            {},
        ),
        spawn_application=spawn_application,
        spawn_application_verified=spawn_verified,
        spawn_source_fail_reasons=spawn_fail_reasons,
        fallback_motion_spec=_collision_obstacle_motion_spec(),
        route_start_xy_m=route_start_xy_m,
        route_dropoff_xy_m=route_dropoff_xy_m,
        pose_sample=_collision_obstacle_pose_sample,
        contact_observation=_collision_obstacle_contact_topic_observation,
        configured_contact_topic=COLLISION_OBSTACLE_CONTACT_TOPIC,
        sleep=time.sleep,
    )


def _route_blocking_candidate_evidence_realism() -> dict[str, Any]:
    spawn_application = _gazebo_route_corridor_obstacle_spawn_application_realism().get(
        "gazebo_route_corridor_obstacle_spawn_application",
        {},
    )
    spawn_verified, spawn_fail_reasons = _route_corridor_obstacle_application_source_check(
        spawn_application
    )
    return _project_route_blocking_candidate(
        requested=_collision_obstacle_requested(),
        collision_evidence=(COLLISION_OBSTACLE_SUMMARY or {}).get(
            "collision_obstacle_evidence",
            {},
        ),
        spawn_application=spawn_application,
        spawn_application_verified=spawn_verified,
        spawn_source_fail_reasons=spawn_fail_reasons,
    )


def _horizontal_route_contact_topic_integration_realism(
    run_dir: Path,
) -> dict[str, Any]:
    requested = _collision_obstacle_contact_topic_requested()
    sidecar_summary: dict[str, Any] = {}
    if requested:
        sidecar_summary = contact_event_smoke.run_contact_event_smoke(
            output_root=(run_dir / "contact_topic_sidecar").resolve(),
            docker_image=PX4_GAZEBO_IMAGE,
        )
    return _project_horizontal_contact_topic_integration(
        requested=requested,
        run_dir=run_dir,
        sidecar_summary=sidecar_summary,
        route_blocking_candidate_summary=ROUTE_BLOCKING_CANDIDATE_SUMMARY,
    )


def _refresh_horizontal_contact_topic_summary(run_dir: Path) -> None:
    global HORIZONTAL_CONTACT_TOPIC_SUMMARY
    if HORIZONTAL_CONTACT_TOPIC_SUMMARY is None:
        HORIZONTAL_CONTACT_TOPIC_SUMMARY = _horizontal_route_contact_topic_integration_realism(
            run_dir
        )


def _operational_incident_report_realism() -> dict[str, Any]:
    return _build_operational_incident_report(
        route_blocking_candidate_summary=ROUTE_BLOCKING_CANDIDATE_SUMMARY or {},
        collision_obstacle_summary=COLLISION_OBSTACLE_SUMMARY or {},
        requested=_collision_obstacle_requested(),
    )


def _traffic_conflict_verification_realism() -> dict[str, Any]:
    return _build_traffic_conflict_verification(
        operational_incident_report_summary=(OPERATIONAL_INCIDENT_REPORT_SUMMARY or {}),
        requested=_collision_obstacle_requested(),
    )


def _route_blocking_verification_realism() -> dict[str, Any]:
    return _build_route_blocking_verification(
        traffic_conflict_verification_summary=(TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {}),
        requested=_collision_obstacle_requested(),
    )


def _alternate_landing_candidate_evidence_realism() -> dict[str, Any]:
    return _build_alternate_landing_candidate_evidence(
        route_blocking_verification_summary=(ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}),
        operational_realism_summary=OPERATIONAL_REALISM_SUMMARY or {},
        requested=(_collision_obstacle_requested() and _alternate_landing_marker_requested()),
    )


def _collect_route_blocking_observation(
    *,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
) -> dict[str, dict[str, Any]]:
    global \
        MOVING_ACTOR_LINEAR_MOTION_SUMMARY, \
        MOVING_ACTOR_POSE_SUMMARY, \
        MOVING_ACTOR_PROXIMITY_SUMMARY, \
        COLLISION_OBSTACLE_SUMMARY, \
        ROUTE_BLOCKING_CANDIDATE_SUMMARY, \
        OPERATIONAL_INCIDENT_REPORT_SUMMARY, \
        TRAFFIC_CONFLICT_VERIFICATION_SUMMARY, \
        ROUTE_BLOCKING_VERIFICATION_SUMMARY, \
        ALTERNATE_LANDING_CANDIDATE_SUMMARY

    MOVING_ACTOR_LINEAR_MOTION_SUMMARY = _moving_actor_waypoint_motion_application_realism()
    MOVING_ACTOR_POSE_SUMMARY = _moving_actor_pose_observation_realism()
    MOVING_ACTOR_PROXIMITY_SUMMARY = _moving_actor_proximity_evidence_realism(
        route_start_xy_m=route_start_xy_m,
        route_dropoff_xy_m=route_dropoff_xy_m,
    )
    COLLISION_OBSTACLE_SUMMARY = _collision_obstacle_evidence_realism(
        route_start_xy_m=route_start_xy_m,
        route_dropoff_xy_m=route_dropoff_xy_m,
    )
    ROUTE_BLOCKING_CANDIDATE_SUMMARY = _route_blocking_candidate_evidence_realism()
    OPERATIONAL_INCIDENT_REPORT_SUMMARY = _operational_incident_report_realism()
    TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = _traffic_conflict_verification_realism()
    ROUTE_BLOCKING_VERIFICATION_SUMMARY = _route_blocking_verification_realism()
    ALTERNATE_LANDING_CANDIDATE_SUMMARY = _alternate_landing_candidate_evidence_realism()
    return {
        "moving_actor_pose": MOVING_ACTOR_POSE_SUMMARY or {},
        "moving_actor_proximity": MOVING_ACTOR_PROXIMITY_SUMMARY or {},
        "collision_obstacle": COLLISION_OBSTACLE_SUMMARY or {},
        "route_blocking_candidate": ROUTE_BLOCKING_CANDIDATE_SUMMARY or {},
        "operational_incident_report": OPERATIONAL_INCIDENT_REPORT_SUMMARY or {},
        "traffic_conflict_verification": TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {},
        "route_blocking_verification": ROUTE_BLOCKING_VERIFICATION_SUMMARY or {},
        "alternate_landing_candidate": ALTERNATE_LANDING_CANDIDATE_SUMMARY or {},
    }


def _record_route_blocking_wait_observation(attempt: int) -> None:
    _append_live_pose_row(
        "route_blocking_observation",
        _pose_sample(),
        sample_index=attempt - 1,
    )


def _alternate_landing_execution_realism(
    *,
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    completed_pose: dict[str, float] | None,
    landing_samples: list[dict[str, float]],
) -> dict[str, Any]:
    return _project_alternate_landing_outcome(
        alternate_landing_candidate_summary=(ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}),
        emergency_approval=emergency_approval,
        emergency_allowlist=emergency_allowlist,
        emergency_dispatch=emergency_dispatch,
        completed_pose=completed_pose,
        landing_samples=landing_samples,
    )


def _execute_alternate_route_rewrite(
    *,
    target_z: float,
    altitude_max_m: float,
    upload_result: dict[str, Any] | None,
    approval: Any,
    route_allowlist: Any,
) -> dict[str, Any]:
    return _run_alternate_route_rewrite(
        candidate_summary=ALTERNATE_LANDING_CANDIDATE_SUMMARY or {},
        target_z=target_z,
        altitude_max_m=altitude_max_m,
        upload_result=upload_result,
        approval=approval,
        route_allowlist=route_allowlist,
        pose_sample=_pose_sample,
        send_route_with_monitor=_send_route_with_monitor,
        append_live_pose_row=_append_live_pose_row,
    )


def _upload_alternate_landing_mission() -> dict[str, Any]:
    mission_upload_smoke.CONTAINER_NAME = CONTAINER_NAME
    mission_upload_smoke.PX4_MAVLINK_PORT = ROUTE_MAVLINK_PX4_PORT
    mission_upload_smoke.GCS_MAVLINK_PORT = ROUTE_MAVLINK_LOCAL_PORT
    return mission_upload_smoke._actual_upload(_alternate_route_mission_upload_payloads())


def _alternate_mission_upload_realism(
    *,
    upload_result: dict[str, Any] | None,
    alternate_behavior_observation: dict[str, Any],
    alternate_route_execution: dict[str, Any] | None = None,
    operator_approval_performed: bool,
) -> dict[str, Any]:
    return _project_alternate_mission_upload(
        candidate_summary=ALTERNATE_LANDING_CANDIDATE_SUMMARY or {},
        upload_result=upload_result,
        alternate_behavior_observation=alternate_behavior_observation,
        alternate_route_execution=alternate_route_execution,
        route_endpoint_port=ROUTE_MAVLINK_PX4_PORT,
        operator_approval_performed=operator_approval_performed,
    )


def _rth_behavior_execution_realism(
    *,
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    rth_state_observed: bool,
    rth_state_label: str | None,
    rth_pose: dict[str, float] | None,
    rth_samples: list[dict[str, float]],
) -> dict[str, Any]:
    return _project_rth_outcome(
        route_blocking_verification_summary=(ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}),
        rth_requested=_rth_behavior_requested(),
        emergency_approval=emergency_approval,
        emergency_allowlist=emergency_allowlist,
        emergency_dispatch=emergency_dispatch,
        rth_state_observed=rth_state_observed,
        rth_state_label=rth_state_label,
        rth_pose=rth_pose,
        rth_samples=rth_samples,
    )


def _moving_actor_proximity_evidence_realism(
    *,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
) -> dict[str, Any]:
    return _project_moving_actor_proximity(
        requested=_moving_actor_marker_requested(),
        pose_observation=(MOVING_ACTOR_POSE_SUMMARY or {}).get(
            "moving_actor_pose_observation",
            {},
        ),
        route_start_xy_m=route_start_xy_m,
        route_dropoff_xy_m=route_dropoff_xy_m,
    )


def _trigger_payload_release() -> dict[str, Any] | None:
    if os.getenv(PAYLOAD_RELEASE_MODEL_ENV) != "1":
        return None
    before = _payload_pose_sample()
    observed_at = datetime.now(timezone.utc).isoformat()
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"gz topic -t {PAYLOAD_DETACH_TOPIC} -m gz.msgs.Empty -p ''",
        ],
        timeout=10,
    )
    time.sleep(1)
    after = _payload_pose_sample()
    return {
        "payload_release_observed": True,
        "payload_release_event_source": "gazebo_detachable_joint_detach_event",
        "payload_id": "pkg-sitl-dropoff",
        "payload_detach_topic": PAYLOAD_DETACH_TOPIC,
        "payload_pose_before_release": before,
        "payload_release_position_x_m": after["x"],
        "payload_release_position_y_m": after["y"],
        "payload_release_position_z_m": after["z"],
        "payload_release_observed_at": observed_at,
        "gazebo_detachable_joint_release_performed": True,
        "gazebo_detachable_joint_release_observed": True,
        "gazebo_entity_mutation_performed": False,
    }


def _send_helper(mode: str, *args: object, timeout: int = 30) -> dict[str, Any]:
    return _execute_embedded_helper(
        mode,
        *args,
        runner=_run,
        container_name=CONTAINER_NAME,
        helper_source=MAVLINK_ROUTE_HELPER,
        timeout=timeout,
    )


def _observe_mavlink_heartbeat_gap(
    *,
    duration_seconds: float = 3.0,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    return _execute_mavlink_heartbeat_observer(
        runner=_run,
        container_name=CONTAINER_NAME,
        helper_source=MAVLINK_HEARTBEAT_OBSERVER_HELPER,
        local_port=ROUTE_MAVLINK_LOCAL_PORT,
        duration_seconds=duration_seconds,
        gap_threshold_seconds=gap_threshold_seconds,
    )


def _apply_bounded_mavlink_link_loss(
    *,
    duration_seconds: float = 2.5,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    return _execute_bounded_mavlink_link_loss(
        runner=_run,
        container_name=CONTAINER_NAME,
        helper_source=MAVLINK_LINK_LOSS_APPLICATOR_HELPER,
        route_px4_port=ROUTE_MAVLINK_PX4_PORT,
        route_local_port=ROUTE_MAVLINK_LOCAL_PORT,
        emergency_px4_port=EMERGENCY_MAVLINK_PX4_PORT,
        emergency_local_port=EMERGENCY_MAVLINK_LOCAL_PORT,
        restart_emergency=os.getenv(SKIP_EMERGENCY_MAVLINK_ENV) != "1",
        duration_seconds=duration_seconds,
        gap_threshold_seconds=gap_threshold_seconds,
    )


def _send_route_with_monitor(
    *,
    target_x: float,
    target_y: float,
    target_z: float,
    feed_forward_vx_mps: float = 0.0,
    feed_forward_vy_mps: float = 0.0,
    feed_forward_ramp_start_fraction: float = 0.65,
    feed_forward_ramp_end_fraction: float = 0.9,
    expected_target_x: float,
    expected_target_y: float,
    pickup_pose: dict[str, float],
    altitude_max_m: float,
    max_pose_deviation_xy_m: float,
    max_pose_deviation_z_m: float,
    duration_seconds: float,
    timeout: int = 45,
    on_deviation: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _execute_route_with_monitor(
        target_x=target_x,
        target_y=target_y,
        target_z=target_z,
        expected_target_x=expected_target_x,
        expected_target_y=expected_target_y,
        pickup_pose=pickup_pose,
        altitude_max_m=altitude_max_m,
        max_pose_deviation_xy_m=max_pose_deviation_xy_m,
        max_pose_deviation_z_m=max_pose_deviation_z_m,
        duration_seconds=duration_seconds,
        container_name=CONTAINER_NAME,
        helper_source=MAVLINK_ROUTE_HELPER,
        pose_sampler=_pose_sample,
        append_pose_row=_append_live_pose_row,
        distance_to_segment=_distance_to_segment_xy,
        feed_forward_vx_mps=feed_forward_vx_mps,
        feed_forward_vy_mps=feed_forward_vy_mps,
        feed_forward_ramp_start_fraction=feed_forward_ramp_start_fraction,
        feed_forward_ramp_end_fraction=feed_forward_ramp_end_fraction,
        timeout=timeout,
        on_deviation=on_deviation,
    )


def _send_command(
    command_name: str,
    *,
    approval: Any,
    coupled_allowlist: Any,
) -> None:
    command_id = {
        "arm": MAV_CMD_COMPONENT_ARM_DISARM,
        "takeoff": MAV_CMD_NAV_TAKEOFF,
        "land": MAV_CMD_NAV_LAND,
    }[command_name]
    validate_px4_gazebo_coupled_command_dispatch(
        approval=approval,
        allowlist=coupled_allowlist,
        command_id=command_id,
    )
    result = _send_helper(command_name)
    if result.get("command_ack_observed") is not True or result.get("command_ack_result_code") != 0:
        raise RuntimeError(
            f"{command_name}_command_ack_not_accepted: {json.dumps(result, sort_keys=True)}"
        )


def _dispatch_emergency_recovery(action: str) -> Any:
    emergency_approval = build_px4_gazebo_emergency_command_approval(
        operator_approval_performed=True,
        approved_recovery_actions=[action],
        now=NOW,
    )
    emergency_allowlist = build_px4_gazebo_emergency_command_allowlist(
        approval=emergency_approval,
        now=NOW,
    )
    emergency_dispatch = run_px4_gazebo_emergency_command_dispatch(
        recovery_action=action,
        approval=emergency_approval,
        allowlist=emergency_allowlist,
        endpoint_port=EMERGENCY_MAVLINK_PX4_PORT,
        local_bind_port=EMERGENCY_MAVLINK_LOCAL_PORT,
        live_mavlink_opt_in=True,
        ack_timeout_seconds=5.0,
        now=NOW,
    )
    return emergency_approval, emergency_allowlist, emergency_dispatch


MULTI_CONDITION_SUPERVISOR_SCOPE = _route_supervision.MULTI_CONDITION_SUPERVISOR_SCOPE
WIND_SUPERVISOR_SCOPE = _route_supervision.WIND_SUPERVISOR_SCOPE


def _obstacle_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    cycle1_state_label: str | None = None,
) -> dict[str, Any]:
    return _route_supervision.obstacle_supervisor_assessment_inputs(
        selected_bounded_action=selected_bounded_action,
        cycle1_state_label=cycle1_state_label,
        route_blocking_verification_summary=(ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}),
        alternate_mission_upload_summary=ALTERNATE_MISSION_UPLOAD_SUMMARY or {},
        battery_realism_summary=BATTERY_REALISM_SUMMARY or {},
        telemetry_realism_summary=TELEMETRY_REALISM_SUMMARY or {},
    )


def _build_obstacle_supervisor_loop() -> dict[str, Any]:
    return _route_supervision.build_obstacle_supervisor_loop_from_summaries(
        route_blocking_verification_summary=(ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}),
        alternate_mission_upload_summary=ALTERNATE_MISSION_UPLOAD_SUMMARY or {},
        alternate_landing_execution_summary=(ALTERNATE_LANDING_EXECUTION_SUMMARY or {}),
        battery_realism_summary=BATTERY_REALISM_SUMMARY or {},
        telemetry_realism_summary=TELEMETRY_REALISM_SUMMARY or {},
    )


def _dispatch_alternate_landing_execution() -> Any:
    return _dispatch_emergency_recovery("land")


def _dispatch_rth_behavior_execution() -> Any:
    return _dispatch_emergency_recovery("rtl")


def _send_until_z(
    command_names: list[str],
    predicate: Callable[[float, list[float]], bool],
    *,
    approval: Any,
    coupled_allowlist: Any,
    timeout: float,
    resend_interval: float = 5.0,
    phase: str = "telemetry",
) -> tuple[dict[str, float], list[dict[str, float]]]:
    deadline = time.monotonic() + timeout
    samples: list[dict[str, float]] = []
    last_sent_at = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_sent_at >= resend_interval:
            for command_name in command_names:
                _send_command(
                    command_name,
                    approval=approval,
                    coupled_allowlist=coupled_allowlist,
                )
            last_sent_at = now
        sample = _pose_sample()
        samples.append(sample)
        _append_live_pose_row(phase, sample, sample_index=len(samples) - 1)
        if predicate(sample["z"], [item["z"] for item in samples]):
            return sample, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for z predicate; samples={samples}")


def _wait_for_z(
    predicate: Callable[[float, list[float]], bool],
    *,
    timeout: float = 60.0,
    phase: str = "telemetry",
) -> tuple[dict[str, float], list[dict[str, float]]]:
    deadline = time.monotonic() + timeout
    samples: list[dict[str, float]] = []
    while time.monotonic() < deadline:
        sample = _pose_sample()
        samples.append(sample)
        _append_live_pose_row(phase, sample, sample_index=len(samples) - 1)
        if predicate(sample["z"], [item["z"] for item in samples]):
            return sample, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for z predicate; samples={samples}")


def _observe_recovery_state(
    *,
    action: str,
    pickup_pose: dict[str, float],
    dispatch_frame_sent: bool,
) -> tuple[bool, str | None, dict[str, float] | None, list[dict[str, float]]]:
    if not dispatch_frame_sent:
        return False, None, None, []
    if action == "land":
        landing_z_threshold = _landing_z_threshold(pickup_pose)
        pose, samples = _wait_for_z(
            lambda z, _samples: z <= landing_z_threshold,
            timeout=80.0,
        )
        return True, None, pose, samples
    if action == "hold":
        samples: list[dict[str, float]] = []
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            samples.append(_pose_sample())
            if "command 17 unsupported" in _logs():
                return (
                    False,
                    "hold_command_unsupported",
                    samples[-1],
                    samples,
                )
            if len(samples) >= 5:
                recent = samples[-5:]
                xy_span = max(
                    math.hypot(
                        item["x"] - recent[0]["x"],
                        item["y"] - recent[0]["y"],
                    )
                    for item in recent
                )
                z_span = max(abs(item["z"] - recent[0]["z"]) for item in recent)
                if xy_span <= 1.0 and z_span <= 0.75:
                    return True, "hold_state_observed", recent[-1], samples
            time.sleep(1)
        return False, None, samples[-1] if samples else None, samples
    if action == "rtl":
        pickup_xy = (float(pickup_pose["x"]), float(pickup_pose["y"]))
        samples = []
        deadline = time.monotonic() + 80.0
        while time.monotonic() < deadline:
            sample = _pose_sample()
            samples.append(sample)
            distance_to_pickup = math.hypot(
                float(sample["x"]) - pickup_xy[0],
                float(sample["y"]) - pickup_xy[1],
            )
            if distance_to_pickup <= 2.0:
                return True, "return_to_launch_state_observed", sample, samples
            time.sleep(1)
        return False, None, samples[-1] if samples else None, samples
    return False, None, None, []


def main() -> int:
    global \
        LIVE_POSE_TRACE_PATH, \
        PAYLOAD_RELEASE_SUMMARY, \
        WIND_REALISM_SUMMARY, \
        THERMAL_WEATHER_REALISM_SUMMARY, \
        VEHICLE_REALISM_SUMMARY, \
        BATTERY_REALISM_SUMMARY, \
        SENSOR_REALISM_SUMMARY, \
        WORLD_REALISM_SUMMARY, \
        VISIBILITY_REALISM_SUMMARY, \
        OPERATIONAL_REALISM_SUMMARY, \
        MOVING_ACTOR_LINEAR_MOTION_SUMMARY, \
        MOVING_ACTOR_POSE_SUMMARY, \
        MOVING_ACTOR_PROXIMITY_SUMMARY, \
        COLLISION_OBSTACLE_SUMMARY, \
        ROUTE_BLOCKING_CANDIDATE_SUMMARY, \
        HORIZONTAL_CONTACT_TOPIC_SUMMARY, \
        OPERATIONAL_INCIDENT_REPORT_SUMMARY, \
        TRAFFIC_CONFLICT_VERIFICATION_SUMMARY, \
        ROUTE_BLOCKING_VERIFICATION_SUMMARY, \
        ALTERNATE_LANDING_CANDIDATE_SUMMARY, \
        ALTERNATE_LANDING_EXECUTION_SUMMARY, \
        ALTERNATE_MISSION_UPLOAD_SUMMARY, \
        RTH_BEHAVIOR_SUMMARY, \
        TELEMETRY_REALISM_SUMMARY, \
        MAVLINK_LINK_REALISM_SUMMARY, \
        TERRAIN_WORLD_REALISM_SUMMARY, \
        TELEMETRY_DROPOUT_EVENTS, \
        TELEMETRY_OBSERVER_SAMPLE_EVENTS
    args = _parse_args()
    _require_opt_in()
    run_dir = _new_run_dir()
    LIVE_POSE_TRACE_PATH = run_dir / "pose_samples.jsonl"
    LIVE_POSE_TRACE_PATH.write_text("")
    PAYLOAD_RELEASE_SUMMARY = None
    WIND_REALISM_SUMMARY = None
    THERMAL_WEATHER_REALISM_SUMMARY = None
    VEHICLE_REALISM_SUMMARY = None
    BATTERY_REALISM_SUMMARY = None
    SENSOR_REALISM_SUMMARY = None
    WORLD_REALISM_SUMMARY = None
    VISIBILITY_REALISM_SUMMARY = None
    OPERATIONAL_REALISM_SUMMARY = None
    MOVING_ACTOR_LINEAR_MOTION_SUMMARY = None
    MOVING_ACTOR_POSE_SUMMARY = None
    MOVING_ACTOR_PROXIMITY_SUMMARY = None
    COLLISION_OBSTACLE_SUMMARY = None
    ROUTE_BLOCKING_CANDIDATE_SUMMARY = None
    HORIZONTAL_CONTACT_TOPIC_SUMMARY = None
    OPERATIONAL_INCIDENT_REPORT_SUMMARY = None
    TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = None
    ROUTE_BLOCKING_VERIFICATION_SUMMARY = None
    ALTERNATE_LANDING_CANDIDATE_SUMMARY = None
    ALTERNATE_LANDING_EXECUTION_SUMMARY = None
    RTH_BEHAVIOR_SUMMARY = None
    ALTERNATE_MISSION_UPLOAD_SUMMARY = None
    TELEMETRY_REALISM_SUMMARY = None
    MAVLINK_LINK_REALISM_SUMMARY = None
    TERRAIN_WORLD_REALISM_SUMMARY = None
    TELEMETRY_DROPOUT_EVENTS = []
    TELEMETRY_OBSERVER_SAMPLE_EVENTS = []
    _validate_payload_advisory_recovery_args(args)
    payload_model_root = _start_container(run_dir)
    try:
        _wait_for_px4_home()
        TERRAIN_WORLD_REALISM_SUMMARY = _terrain_world_readback(payload_model_root)
        WIND_REALISM_SUMMARY = _apply_wind_realism(payload_model_root)
        THERMAL_WEATHER_REALISM_SUMMARY = _thermal_weather_realism()
        VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
            payload_model_root=payload_model_root
        )
        BATTERY_REALISM_SUMMARY = _battery_realism()
        SENSOR_REALISM_SUMMARY = _sensor_failure_realism()
        WORLD_REALISM_SUMMARY = _landing_zone_blocked_realism(payload_model_root=payload_model_root)
        VISIBILITY_REALISM_SUMMARY = _visibility_realism(payload_model_root=payload_model_root)
        OPERATIONAL_REALISM_SUMMARY = _operational_no_fly_zone_realism(
            payload_model_root=payload_model_root
        )
        MAVLINK_LINK_REALISM_SUMMARY = _mavlink_link_degradation_realism()
        with TemporaryDirectory() as tmp:
            task_db_path = Path(tmp) / "tasks.db"
            store = TaskStore(str(task_db_path))
            bootstrap: _RouteBootstrapResult = _bootstrap_route_task(
                store=store,
                max_pose_deviation_xy_m=args.max_pose_deviation_xy_m,
                on_deviation_action=args.on_deviation_action,
                operator_approval_performed=True,
                now=NOW,
            )
            task = bootstrap.task
            route = bootstrap.route
            approval = bootstrap.approval
            coupled_allowlist = bootstrap.coupled_allowlist
            route_allowlist = bootstrap.route_allowlist

            preupload_summary = PREUPLOAD_SUMMARY

            pickup_pose = _pose_sample()
            _append_live_pose_row("pickup", pickup_pose)
            _enroute_pose, climb_samples = _send_until_z(
                ["arm", "takeoff"],
                lambda z, _samples: z >= 1.0,
                approval=approval,
                coupled_allowlist=coupled_allowlist,
                timeout=75.0,
                phase="climb",
            )
            if _payload_advisory_recovery_requested(args):
                payload_route_progress_payload = None
                payload_route_pose = None
                payload_pre_recovery_distance_to_pickup_m = None
                payload_route_progress_away_from_pickup_observed = False
                if args.mission_os_supervisor_payload_loop:
                    route_delta_x, route_delta_y, target_z = derive_px4_gazebo_route_target_ned(
                        route
                    )
                    route_origin_x, route_origin_y = _terrain_relative_xy_origin(pickup_pose)
                    target_x = route_origin_x + route_delta_x
                    target_y = route_origin_y + route_delta_y
                    _assert_planned_route_stream_budget(duration_seconds=12.0)
                    payload_route_progress_payload = _send_route_with_monitor(
                        target_x=target_x,
                        target_y=target_y,
                        target_z=target_z,
                        expected_target_x=target_x,
                        expected_target_y=target_y,
                        pickup_pose=pickup_pose,
                        altitude_max_m=route.altitude_max_m,
                        max_pose_deviation_xy_m=10.0,
                        max_pose_deviation_z_m=3.0,
                        duration_seconds=12.0,
                        timeout=25,
                    )
                    payload_route_pose = _pose_sample()
                    _append_live_pose_row("payload_pre_recovery_route", payload_route_pose)
                    payload_pre_recovery_distance_to_pickup_m = math.hypot(
                        float(payload_route_pose["x"]) - float(pickup_pose["x"]),
                        float(payload_route_pose["y"]) - float(pickup_pose["y"]),
                    )
                    payload_route_progress_away_from_pickup_observed = (
                        payload_pre_recovery_distance_to_pickup_m >= 2.5
                    )
                    if not payload_route_progress_away_from_pickup_observed:
                        raise RuntimeError(
                            "payload supervisor Form 3 requires route progress "
                            "away from pickup before bounded RTL"
                        )
                (
                    payload_recovery_approval,
                    payload_recovery_allowlist,
                    payload_recovery_dispatch,
                ) = _dispatch_emergency_recovery(args.payload_advisory_recovery_action)
                payload_recovery_cycle = _observe_dispatched_recovery(
                    action=args.payload_advisory_recovery_action,
                    approval=payload_recovery_approval,
                    dispatch=payload_recovery_dispatch,
                    pickup_pose=pickup_pose,
                    observe_state=_observe_recovery_state,
                )
                payload_recovery_outcome = payload_recovery_cycle.outcome
                payload_recovery_completed = payload_recovery_outcome.completed
                payload_recovery_pose = payload_recovery_cycle.pose
                payload_recovery_samples = list(payload_recovery_cycle.samples)
                payload_recovery_distance_to_pickup_m = (
                    None
                    if payload_recovery_pose is None
                    else math.hypot(
                        float(payload_recovery_pose["x"]) - float(pickup_pose["x"]),
                        float(payload_recovery_pose["y"]) - float(pickup_pose["y"]),
                    )
                )
                payload_recovery_outcome_ref = PAYLOAD_RECOVERY_ACTION_REF
                post_recovery_approval = None
                post_recovery_allowlist = None
                post_recovery_dispatch = None
                post_recovery_pose = None
                post_recovery_samples: list[dict[str, float]] = []
                payload_supervisor_post_recovery_action_ref = None
                payload_supervisor_post_recovery_action = None
                post_recovery_outcome = _RecoveryCycleOutcome(action=None)
                mission_os_supervisor_recovery_loop = None
                if (
                    args.mission_os_supervisor_payload_loop
                    and payload_recovery_completed
                    and args.post_recovery_action != "none"
                ):
                    (
                        post_recovery_approval,
                        post_recovery_allowlist,
                        post_recovery_dispatch,
                    ) = _dispatch_emergency_recovery(args.post_recovery_action)
                    post_recovery_cycle = _observe_dispatched_recovery(
                        action=args.post_recovery_action,
                        approval=post_recovery_approval,
                        dispatch=post_recovery_dispatch,
                        pickup_pose=pickup_pose,
                        observe_state=_observe_recovery_state,
                    )
                    post_recovery_outcome = post_recovery_cycle.outcome
                    post_recovery_pose = post_recovery_cycle.pose
                    post_recovery_samples = list(post_recovery_cycle.samples)
                    payload_supervisor_post_recovery_action_ref = (
                        "payload_supervisor_post_recovery_action:mission_designer_payload_mass"
                    )
                    payload_supervisor_post_recovery_action = _build_payload_post_recovery_action(
                        advisory_ref=args.payload_feasibility_advisory_ref,
                        source_cycle1_outcome_ref=payload_recovery_outcome_ref,
                        outcome=post_recovery_outcome,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        action_ref=payload_supervisor_post_recovery_action_ref,
                    )
                    mission_os_supervisor_recovery_loop = (
                        _route_supervision.build_payload_recovery_loop_from_outcomes(
                            payload_feasibility_advisory_ref=(
                                PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
                            ),
                            primary_outcome=payload_recovery_outcome,
                            primary_outcome_ref=payload_recovery_outcome_ref,
                            post_outcome=post_recovery_outcome,
                            post_outcome_ref=(payload_supervisor_post_recovery_action_ref),
                            vehicle_realism_summary=(VEHICLE_REALISM_SUMMARY or {}),
                            battery_realism_summary=(BATTERY_REALISM_SUMMARY or {}),
                            telemetry_realism_summary=(TELEMETRY_REALISM_SUMMARY or {}),
                        )
                    )
                final_status, task_status = _payload_recovery_terminal_status(
                    payload_action=args.payload_advisory_recovery_action,
                    payload_outcome=payload_recovery_outcome,
                    supervisor_loop_requested=(args.mission_os_supervisor_payload_loop),
                    post_recovery_outcome=post_recovery_outcome,
                )
                payload_recovery_action = _build_payload_recovery_action(
                    advisory_ref=args.payload_feasibility_advisory_ref,
                    outcome=payload_recovery_outcome,
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
                updated_payload_recovery = store.update(
                    task["task_id"],
                    status=task_status,
                    artifacts=_recovery_task_artifacts(
                        approval=payload_recovery_approval,
                        allowlist=payload_recovery_allowlist,
                        dispatch=payload_recovery_dispatch,
                        post_approval=post_recovery_approval,
                        post_allowlist=post_recovery_allowlist,
                        post_dispatch=post_recovery_dispatch,
                        payload_recovery_action=payload_recovery_action,
                        payload_post_recovery_action=(payload_supervisor_post_recovery_action),
                        supervisor_loop=mission_os_supervisor_recovery_loop,
                    ),
                )
                assert updated_payload_recovery is not None
                _refresh_battery_realism_observation_from_trace()
                TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
                VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
                    payload_model_root=payload_model_root
                )
                summary = _build_payload_recovery_summary(
                    _PayloadRecoverySummaryInputs(
                        artifact_dir=run_dir,
                        task_status=updated_payload_recovery["status"],
                        existing_artifacts_retained=(
                            updated_payload_recovery["artifacts"]["existing"]["kept"]
                        ),
                        final_status=final_status,
                        advisory_ref=args.payload_feasibility_advisory_ref,
                        payload_action_ref=PAYLOAD_RECOVERY_ACTION_REF,
                        payload_outcome=payload_recovery_outcome,
                        payload_action_artifact=payload_recovery_action,
                        payload_route_progress_payload=(payload_route_progress_payload),
                        payload_route_progress_away_from_pickup_observed=(
                            payload_route_progress_away_from_pickup_observed
                        ),
                        payload_pre_recovery_distance_to_pickup_m=(
                            payload_pre_recovery_distance_to_pickup_m
                        ),
                        payload_recovery_distance_to_pickup_m=(
                            payload_recovery_distance_to_pickup_m
                        ),
                        post_recovery_outcome=post_recovery_outcome,
                        payload_post_recovery_action_ref=(
                            payload_supervisor_post_recovery_action_ref
                        ),
                        payload_post_recovery_action_artifact=(
                            payload_supervisor_post_recovery_action
                        ),
                        supervisor_loop=mission_os_supervisor_recovery_loop,
                        wind_realism_artifacts=(
                            _wind_realism_summary_artifacts(
                                cleanup_status=("teardown_required_after_summary")
                            )
                        ),
                        vehicle_realism_artifacts=(_vehicle_realism_summary_artifacts()),
                    )
                )
                recovery_rows = _recovery_pose_rows(
                    pre_phase="payload_pre_recovery_route",
                    pre_pose=payload_route_pose,
                    primary_phase=(f"payload_recovery_{args.payload_advisory_recovery_action}"),
                    primary_samples=payload_recovery_samples,
                    primary_pose=payload_recovery_pose,
                    primary_completed_phase="payload_recovery_completed",
                    post_phase=(f"payload_post_recovery_{args.post_recovery_action}"),
                    post_samples=post_recovery_samples,
                    post_pose=post_recovery_pose,
                    post_completed_phase="payload_post_recovery_completed",
                )
                _write_recovery_run_artifacts(
                    run_dir=run_dir,
                    summary=summary,
                    task_artifacts=updated_payload_recovery["artifacts"],
                    pose_rows=recovery_rows,
                    log_text=_all_logs(),
                    task_db_path=task_db_path,
                )
                print(json.dumps(summary, indent=2, sort_keys=True))
                _audit_payload_recovery_summary(
                    summary,
                    expectations=_PayloadRecoveryAuditExpectations(
                        advisory_ref_prefix=(PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX),
                        payload_action_ref=PAYLOAD_RECOVERY_ACTION_REF,
                        payload_action=args.payload_advisory_recovery_action,
                        landing_z_threshold_m=(_landing_z_threshold(pickup_pose)),
                        supervisor_loop_requested=(args.mission_os_supervisor_payload_loop),
                    ),
                )
                return 0

            route_delta_x, route_delta_y, target_z = derive_px4_gazebo_route_target_ned(route)
            route_origin_x, route_origin_y = _terrain_relative_xy_origin(pickup_pose)
            target_x = route_origin_x + route_delta_x
            target_y = route_origin_y + route_delta_y
            form2a_wind_compensation = _form2a_wind_compensation_request()
            compensation_offset_x, compensation_offset_y = _form2a_wind_compensation_xy_offset(
                form2a_wind_compensation
            )
            feed_forward_vx_mps, feed_forward_vy_mps = _form2a_wind_feed_forward_xy_mps(
                form2a_wind_compensation
            )
            sent_target_x = target_x + float(args.inject_target_offset_m) + compensation_offset_x
            sent_target_y = target_y + float(args.inject_target_offset_m) + compensation_offset_y
            route_duration_seconds = 25.0
            _assert_planned_route_stream_budget(duration_seconds=route_duration_seconds)
            recovery_approval = None
            recovery_allowlist = None
            recovery_dispatch = None

            def _on_deviation() -> dict[str, Any]:
                nonlocal recovery_approval, recovery_allowlist, recovery_dispatch
                if route.on_deviation_action == "abort_only":
                    return {"recovery_action_taken": None}
                (
                    recovery_approval,
                    recovery_allowlist,
                    recovery_dispatch,
                ) = _dispatch_emergency_recovery(route.on_deviation_action)
                return {
                    "recovery_action_taken": route.on_deviation_action,
                    "recovery_dispatch_status": recovery_dispatch.dispatch_status,
                    "recovery_command_ack_observed": (recovery_dispatch.command_ack_observed),
                    "recovery_command_ack_result_name": (recovery_dispatch.command_ack_result_name),
                }

            route_send = _send_route_with_monitor(
                target_x=sent_target_x,
                target_y=sent_target_y,
                target_z=target_z,
                feed_forward_vx_mps=feed_forward_vx_mps,
                feed_forward_vy_mps=feed_forward_vy_mps,
                feed_forward_ramp_start_fraction=float(
                    form2a_wind_compensation["feed_forward_ramp_start_fraction"]
                ),
                feed_forward_ramp_end_fraction=float(
                    form2a_wind_compensation["feed_forward_ramp_end_fraction"]
                ),
                expected_target_x=target_x,
                expected_target_y=target_y,
                pickup_pose=pickup_pose,
                altitude_max_m=route.altitude_max_m,
                max_pose_deviation_xy_m=route.max_pose_deviation_xy_m,
                max_pose_deviation_z_m=route.max_pose_deviation_z_m,
                duration_seconds=route_duration_seconds,
                timeout=40,
                on_deviation=_on_deviation,
            )
            if route_send.get("pose_deviation_aborted") is True:
                abort = build_px4_gazebo_route_deviation_abort(
                    route_plan=route,
                    route_allowlist=route_allowlist,
                    deviation_samples=route_send["deviation_samples"],
                    route_monitor_sample_count=int(route_send["route_monitor_sample_count"]),
                    now=NOW,
                )
                post_recovery_approval = None
                post_recovery_allowlist = None
                post_recovery_dispatch = None
                mission_os_supervisor_recovery_loop = None
                recovery_workflow = _assemble_route_deviation_recovery()
                if recovery_dispatch is not None:
                    recovery_cycle = _observe_dispatched_recovery(
                        action=route.on_deviation_action,
                        approval=recovery_approval,
                        dispatch=recovery_dispatch,
                        pickup_pose=pickup_pose,
                        observe_state=_observe_recovery_state,
                        deviation_abort=abort,
                        build_completion=(build_px4_gazebo_route_recovery_completion),
                        observed_at=NOW,
                    )
                    recovery_workflow = _assemble_route_deviation_recovery(primary=recovery_cycle)
                    if (
                        recovery_workflow.primary.outcome.completed
                        and args.post_recovery_action != "none"
                    ):
                        (
                            post_recovery_approval,
                            post_recovery_allowlist,
                            post_recovery_dispatch,
                        ) = _dispatch_emergency_recovery(args.post_recovery_action)
                        post_recovery_cycle = _observe_dispatched_recovery(
                            action=args.post_recovery_action,
                            approval=post_recovery_approval,
                            dispatch=post_recovery_dispatch,
                            pickup_pose=pickup_pose,
                            observe_state=_observe_recovery_state,
                            deviation_abort=abort,
                            build_completion=(build_px4_gazebo_route_recovery_completion),
                            observed_at=NOW,
                        )
                        recovery_workflow = _assemble_route_deviation_recovery(
                            primary=recovery_workflow.primary,
                            post=post_recovery_cycle,
                        )
                updated_abort = _persist_route_deviation_recovery(
                    _RouteDeviationRecoveryPersistenceInputs(
                        store=store,
                        task_id=task["task_id"],
                        workflow=recovery_workflow,
                        deviation_abort=abort,
                        approval=recovery_approval,
                        allowlist=recovery_allowlist,
                        dispatch=recovery_dispatch,
                        post_approval=post_recovery_approval,
                        post_allowlist=post_recovery_allowlist,
                        post_dispatch=post_recovery_dispatch,
                    )
                )
                _refresh_battery_realism_observation_from_trace()
                MOVING_ACTOR_LINEAR_MOTION_SUMMARY = (
                    _moving_actor_waypoint_motion_application_realism()
                )
                MOVING_ACTOR_POSE_SUMMARY = _moving_actor_pose_observation_realism()
                MOVING_ACTOR_PROXIMITY_SUMMARY = _moving_actor_proximity_evidence_realism(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
                COLLISION_OBSTACLE_SUMMARY = _collision_obstacle_evidence_realism(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = _route_blocking_candidate_evidence_realism()
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = _operational_incident_report_realism()
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = _traffic_conflict_verification_realism()
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = _route_blocking_verification_realism()
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = (
                    _alternate_landing_candidate_evidence_realism()
                )
                TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
                _refresh_horizontal_contact_topic_summary(run_dir)
                if recovery_dispatch is not None and (
                    args.mission_os_supervisor_recovery_loop
                    or args.mission_os_supervisor_multi_condition_loop
                ):
                    mission_os_supervisor_recovery_loop = (
                        _route_supervision.build_wind_recovery_loop_from_outcomes(
                            deviation_samples=route_send["deviation_samples"],
                            primary_outcome=recovery_workflow.primary.outcome,
                            post_outcome=recovery_workflow.post.outcome,
                            wind_requested_profile=_wind_requested_profile(),
                            route_blocking_verification_summary=(
                                ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}
                            ),
                            vehicle_realism_summary=(VEHICLE_REALISM_SUMMARY or {}),
                            battery_realism_summary=(BATTERY_REALISM_SUMMARY or {}),
                            telemetry_realism_summary=(TELEMETRY_REALISM_SUMMARY or {}),
                            supervisor_scope=(
                                MULTI_CONDITION_SUPERVISOR_SCOPE
                                if args.mission_os_supervisor_multi_condition_loop
                                else WIND_SUPERVISOR_SCOPE
                            ),
                        )
                    )
                summary = _build_route_deviation_recovery_summary(
                    _RouteDeviationRecoverySummaryInputs(
                        artifact_dir=run_dir,
                        task_status=updated_abort["status"],
                        existing_artifacts_retained=(
                            updated_abort["artifacts"]["existing"]["kept"]
                        ),
                        final_status=recovery_workflow.final_status,
                        deviation_abort=abort,
                        route=route,
                        route_stream=route_send,
                        recovery_outcome=recovery_workflow.primary.outcome,
                        recovery_completion=(recovery_workflow.primary.completion),
                        post_recovery_outcome=recovery_workflow.post.outcome,
                        supervisor_loop=mission_os_supervisor_recovery_loop,
                        wind_realism_artifacts=(
                            _wind_realism_summary_artifacts(
                                cleanup_status=("teardown_required_after_summary")
                            )
                        ),
                        vehicle_realism_artifacts=(_vehicle_realism_summary_artifacts()),
                    )
                )
                recovery_rows = _recovery_pose_rows(
                    primary_phase=f"recovery_{route.on_deviation_action}",
                    primary_samples=recovery_workflow.primary.samples,
                    primary_pose=recovery_workflow.primary.pose,
                    primary_completed_phase="recovery_completed",
                    post_phase=f"post_recovery_{args.post_recovery_action}",
                    post_samples=recovery_workflow.post.samples,
                    post_pose=recovery_workflow.post.pose,
                    post_completed_phase="post_recovery_completed",
                )
                _write_recovery_run_artifacts(
                    run_dir=run_dir,
                    summary=summary,
                    task_artifacts=updated_abort["artifacts"],
                    pose_rows=recovery_rows,
                    log_text=_all_logs(),
                    task_db_path=task_db_path,
                )
                print(json.dumps(summary, indent=2, sort_keys=True))
                _audit_route_deviation_recovery_summary(
                    summary,
                    expectations=_RouteDeviationRecoveryAuditExpectations(
                        on_deviation_action=route.on_deviation_action,
                        post_recovery_action=args.post_recovery_action,
                        landing_z_threshold_m=(_landing_z_threshold(pickup_pose)),
                    ),
                )
                return 0
            assert route_send["offboard_mode_switch_allowed"] is True
            assert route_send["offboard_mode_switch_command_id"] == 176
            assert route_send["offboard_mode_switch_frame_sent"] is True
            assert route_send["offboard_mode_switch_ack_required"] is True
            assert route_send["offboard_mode_switch_ack_command_id"] == 176
            assert route_send["offboard_mode_switch_ack_observed"] is True
            assert route_send["offboard_mode_switch_ack_result_code"] == 0
            route_pose = _pose_sample()
            _append_live_pose_row("route", route_pose)
            observation_attempts = 8 if _collision_obstacle_requested() else 1
            route_blocking_decision = _observe_route_blocking_decision(
                observation_attempts=observation_attempts,
                rth_requested=_rth_behavior_requested(),
                observe_once=lambda _attempt: _collect_route_blocking_observation(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                ),
                record_wait_observation=_record_route_blocking_wait_observation,
            )
            alternate_landing_requested = route_blocking_decision.alternate_landing_requested
            rth_behavior_requested = route_blocking_decision.rth_behavior_requested
            route_blocking_decision_summaries = route_blocking_decision.decision_summaries

            def _wait_for_terminal_landing(
                phase: str,
            ) -> tuple[dict[str, float], list[dict[str, float]]]:
                landing_z_threshold = _landing_z_threshold(pickup_pose)
                return _wait_for_z(
                    lambda z, _samples: z <= landing_z_threshold,
                    timeout=80.0,
                    phase=phase,
                )

            terminal_action = _orchestrate_route_terminal_action(
                rth_behavior_requested=rth_behavior_requested,
                alternate_landing_requested=alternate_landing_requested,
                pickup_pose=pickup_pose,
                target_z=target_z,
                altitude_max_m=route.altitude_max_m,
                route_approval=approval,
                route_allowlist=route_allowlist,
                dispatch_rth=_dispatch_rth_behavior_execution,
                observe_recovery_state=_observe_recovery_state,
                current_pose=_pose_sample,
                upload_alternate_mission=_upload_alternate_landing_mission,
                execute_alternate_route=_execute_alternate_route_rewrite,
                dispatch_alternate_landing=_dispatch_alternate_landing_execution,
                send_standard_land=lambda: _send_command(
                    "land",
                    approval=approval,
                    coupled_allowlist=coupled_allowlist,
                ),
                wait_for_landing=_wait_for_terminal_landing,
            )
            alternate_approval = terminal_action.alternate_approval
            alternate_allowlist = terminal_action.alternate_allowlist
            alternate_dispatch = terminal_action.alternate_dispatch
            alternate_mission_upload_result = terminal_action.alternate_mission_upload_result
            alternate_route_execution_result = terminal_action.alternate_route_execution_result
            rth_approval = terminal_action.rth_approval
            rth_allowlist = terminal_action.rth_allowlist
            rth_dispatch = terminal_action.rth_dispatch
            rth_state_observed = terminal_action.rth_state_observed
            rth_state_label = terminal_action.rth_state_label
            rth_pose = terminal_action.rth_pose
            rth_samples = list(terminal_action.rth_samples)
            completed_pose = terminal_action.completed_pose
            landing_samples = list(terminal_action.landing_samples)
            _append_live_pose_row(
                "rth_completed" if rth_behavior_requested else "completed",
                completed_pose,
            )
            PAYLOAD_RELEASE_SUMMARY = None if rth_behavior_requested else _trigger_payload_release()
            VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
                payload_model_root=payload_model_root,
                payload_release_summary=PAYLOAD_RELEASE_SUMMARY,
            )
            _refresh_battery_realism_observation_from_trace()
            if route_blocking_decision_summaries:
                MOVING_ACTOR_POSE_SUMMARY = route_blocking_decision_summaries.get(
                    "moving_actor_pose", {}
                )
                MOVING_ACTOR_PROXIMITY_SUMMARY = route_blocking_decision_summaries.get(
                    "moving_actor_proximity", {}
                )
                COLLISION_OBSTACLE_SUMMARY = route_blocking_decision_summaries.get(
                    "collision_obstacle", {}
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = route_blocking_decision_summaries.get(
                    "route_blocking_candidate", {}
                )
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = route_blocking_decision_summaries.get(
                    "operational_incident_report", {}
                )
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = route_blocking_decision_summaries.get(
                    "traffic_conflict_verification", {}
                )
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = route_blocking_decision_summaries.get(
                    "route_blocking_verification", {}
                )
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = route_blocking_decision_summaries.get(
                    "alternate_landing_candidate", {}
                )
            else:
                _collect_route_blocking_observation(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
            ALTERNATE_LANDING_EXECUTION_SUMMARY = _alternate_landing_execution_realism(
                emergency_approval=alternate_approval,
                emergency_allowlist=alternate_allowlist,
                emergency_dispatch=alternate_dispatch,
                completed_pose=completed_pose,
                landing_samples=landing_samples,
            )
            ALTERNATE_MISSION_UPLOAD_SUMMARY = _alternate_mission_upload_realism(
                upload_result=alternate_mission_upload_result,
                alternate_behavior_observation=(
                    ALTERNATE_LANDING_EXECUTION_SUMMARY.get(
                        "alternate_landing_behavior_observation", {}
                    )
                ),
                alternate_route_execution=alternate_route_execution_result,
                operator_approval_performed=(approval.operator_approval_performed is True),
            )
            TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
            obstacle_supervisor_recovery_loop = (
                _build_obstacle_supervisor_loop()
                if args.mission_os_supervisor_obstacle_loop
                else None
            )
            RTH_BEHAVIOR_SUMMARY = _rth_behavior_execution_realism(
                emergency_approval=rth_approval,
                emergency_allowlist=rth_allowlist,
                emergency_dispatch=rth_dispatch,
                rth_state_observed=rth_state_observed,
                rth_state_label=rth_state_label,
                rth_pose=rth_pose,
                rth_samples=rth_samples,
            )
            _refresh_horizontal_contact_topic_summary(run_dir)
            finalization: _RouteFinalizationResult = _finalize_route_observation(
                _RouteFinalizationInputs(
                    store=store,
                    task_id=task["task_id"],
                    route=route,
                    route_allowlist=route_allowlist,
                    approval=approval,
                    endpoint_port=ROUTE_MAVLINK_PX4_PORT,
                    target_x_m=route_delta_x,
                    target_y_m=route_delta_y,
                    target_z_m=target_z,
                    route_stream=route_send,
                    pickup_pose_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    observed_pose_xy_m=(completed_pose["x"], completed_pose["y"]),
                    horizontal_route_motion_observed=True,
                    px4_telemetry_correlated=True,
                    gazebo_pose_correlated=True,
                    actual_px4_gazebo_horizontal_smoke_observed=True,
                    now=NOW,
                )
            )
            dispatch = finalization.dispatch
            progress = finalization.progress
            gate = finalization.gate
            updated = finalization.updated_task
            _snapshot_task_database_evidence(
                task_db_path=task_db_path,
                run_dir=run_dir,
            )

        runner = updated["artifacts"]["px4_gazebo_route_delivery_runner_result"]
        recorded_at_value = datetime.now(timezone.utc)
        recorded_at = recorded_at_value.isoformat()
        delivery_completion_claimed = (
            runner["final_status"] == "completed"
            and gate.dropoff_region_reached
            and not gate.blocked_reasons
        )
        terminal_pose_fields = _terminal_pose_summary_fields(
            route_pose=route_pose,
            completed_pose=completed_pose,
            landing_samples=landing_samples,
            route_terminal_progress_m=gate.horizontal_progress_m,
        )
        summary = _build_route_summary(
            _RouteSummaryInputs(
                artifact_dir=run_dir,
                recorded_at=recorded_at,
                task_status=updated["status"],
                existing_artifacts_retained=(updated["artifacts"]["existing"]["kept"]),
                route_plan_schema_version=route.schema_version,
                route_allowlist_schema_version=route_allowlist.schema_version,
                dispatch=dispatch,
                progress=progress,
                gate=gate,
                runner=runner,
                pickup_pose=pickup_pose,
                route_pose=route_pose,
                completed_pose=completed_pose,
                delivery_completion_claimed=delivery_completion_claimed,
                terminal_pose_fields=terminal_pose_fields,
                route_send=route_send,
                sent_target_x_m=sent_target_x,
                sent_target_y_m=sent_target_y,
                uncompensated_target_x_m=target_x,
                uncompensated_target_y_m=target_y,
                form2a_wind_compensation=form2a_wind_compensation,
                compensation_offset_x_m=compensation_offset_x,
                compensation_offset_y_m=compensation_offset_y,
                climb_sample_count=len(climb_samples),
                landing_sample_count=len(landing_samples),
                preupload_summary=preupload_summary,
                payload_release_summary=PAYLOAD_RELEASE_SUMMARY,
                obstacle_supervisor_recovery_loop=(obstacle_supervisor_recovery_loop),
                wind_realism_artifacts=_wind_realism_summary_artifacts(
                    cleanup_status="teardown_required_after_summary"
                ),
                vehicle_realism_artifacts=_vehicle_realism_summary_artifacts(),
            )
        )
        pose_rows = None
        if LIVE_POSE_TRACE_PATH is None or not LIVE_POSE_TRACE_PATH.read_text().strip():
            pose_rows = _pose_rows(
                pickup_pose=pickup_pose,
                climb_samples=climb_samples,
                route_pose=route_pose,
                completed_pose=completed_pose,
                landing_samples=landing_samples,
            )
        _write_run_artifacts(
            run_dir=run_dir,
            summary=summary,
            task_artifacts=updated["artifacts"],
            pose_rows=pose_rows,
            log_text=_all_logs(),
            task_db_path=None,
            recorded_at=recorded_at_value,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        _audit_route_summary(
            summary,
            expectations=_RouteAuditExpectations(
                route_target_x_m=route_delta_x,
                route_target_y_m=route_delta_y,
                route_target_z_m=target_z,
                landing_z_threshold_m=_landing_z_threshold(pickup_pose),
                preupload_requested=os.getenv(PREUPLOAD_MISSION_ENV) == "1",
                payload_release_requested=(os.getenv(PAYLOAD_RELEASE_MODEL_ENV) == "1"),
                contact_topic_requested=(_collision_obstacle_contact_topic_requested()),
            ),
        )
        return 0
    finally:
        _stop_container()
        _mark_cleanup_observed(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
