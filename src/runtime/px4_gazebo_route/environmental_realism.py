"""Environment and operational realism projections for PX4/Gazebo.

The entrypoint supplies explicit scenario requests, already-patched world files,
and transport callbacks. These functions may observe or apply bounded simulator
configuration through those callbacks, but they do not mint approval, dispatch
flight commands, mutate task/gate state, or claim delivery completion or
physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
from typing import Any
import xml.etree.ElementTree as ET

from src.runtime.px4_gazebo_route.scenario import (
    thermal_battery_drain_factor_from_temperature,
    thermal_motor_derate_factor_from_temperature,
)
from src.runtime.px4_gazebo_route.verification import (
    px4_param_set_applied,
    px4_param_value_matches,
)
from src.runtime.px4_gazebo_route.world import (
    VISIBILITY_FOG_RENDER_COLOR,
    VISIBILITY_FOG_RENDER_DENSITY,
    VISIBILITY_FOG_RENDER_END_M,
    VISIBILITY_FOG_RENDER_MARKER_ID,
    VISIBILITY_FOG_RENDER_START_M,
    VISIBILITY_FOG_RENDER_TYPE,
    visibility_marker_fog_element,
)


def _observed_at_text(value: datetime | None) -> str:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat()


def run_thermal_weather_realism(
    *,
    profile: Mapping[str, Any],
    param_show: Callable[[str], Mapping[str, Any]],
    param_set: Callable[[str, float], Mapping[str, Any]],
    reset_battery_status_cache: Callable[[], None],
    battery_status_sample: Callable[[], Mapping[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at_text = _observed_at_text(observed_at)
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    thermal_capability_status = "not_requested"
    battery_drain_status = "not_requested"
    motor_derate_status = "not_requested"
    if requested_present:
        temperature_c = requested.get("temperature_c")
        pressure_hpa = requested.get("pressure_hpa")
        explicit_battery_factor = requested.get("thermal_battery_drain_factor")
        explicit_motor_factor = requested.get("thermal_motor_derate_factor")
        thermal_effect_requested = any(
            value is not None
            for value in (
                temperature_c,
                explicit_battery_factor,
                explicit_motor_factor,
            )
        )
        if pressure_hpa is not None:
            approximation_reasons.append("pressure_hpa_recorded_for_context_not_air_physics")
        if not thermal_effect_requested:
            application_status = "unsupported"
            observation_status = "unsupported"
            thermal_capability_status = "unsupported"
            unsupported_reasons.append("thermal_battery_or_motor_condition_not_requested")
            if pressure_hpa is not None:
                unsupported_reasons.append("pressure_physics_not_supported_by_bounded_sitl_model")
            capability = {
                "schema_version": "simulator_capability_matrix.v1",
                "capability_id": ("simulator_capability_matrix:mission_designer_thermal_weather"),
                "thermal_weather": thermal_capability_status,
                "battery_drain_temperature_effect": battery_drain_status,
                "motor_derate_temperature_effect": motor_derate_status,
                "air_temperature_physics": "not_claimed",
                "pressure_physics": "not_claimed",
                "support_detection_method": ("px4_param_set_readback_and_battery_status_listener"),
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": approximation_reasons,
            }
            application = {
                "schema_version": "simulator_condition_application.v1",
                "application_id": (
                    "simulator_condition_application:mission_designer_thermal_weather"
                ),
                "condition_kind": "thermal_weather",
                "application_status": application_status,
                "requested_condition_ref": profile["condition_id"],
                "applied": applied,
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": approximation_reasons,
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            }
            evidence = {
                "schema_version": "observed_environment_evidence.v1",
                "evidence_id": ("observed_environment_evidence:mission_designer_thermal_weather"),
                "condition_kind": "thermal_weather",
                "observation_status": observation_status,
                "requested_condition_ref": profile["condition_id"],
                "application_ref": application["application_id"],
                "observed": observed,
                "observed_at": observed_at_text,
                "delivery_completion_claimed": False,
            }
            return {
                "thermal_weather_condition_profile": profile,
                "thermal_weather_simulator_capability_matrix": capability,
                "thermal_weather_simulator_condition_application": application,
                "observed_thermal_weather_evidence": evidence,
            }
        battery_factor = (
            float(explicit_battery_factor)
            if explicit_battery_factor is not None
            else thermal_battery_drain_factor_from_temperature(temperature_c)
        )
        motor_factor = (
            float(explicit_motor_factor)
            if explicit_motor_factor is not None
            else thermal_motor_derate_factor_from_temperature(temperature_c)
        )
        if battery_factor is None:
            battery_factor = 1.0
        if motor_factor is None:
            motor_factor = 1.0
        if explicit_battery_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_battery_drain_factor_uses_bounded_sitl_model"
            )
        if explicit_motor_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_motor_derate_factor_uses_bounded_sitl_model"
            )
        before_params = {
            "SIM_BAT_MIN_PCT": param_show("SIM_BAT_MIN_PCT"),
            "SIM_BAT_DRAIN": param_show("SIM_BAT_DRAIN"),
            "MPC_THR_MAX": param_show("MPC_THR_MAX"),
        }
        before_drain = before_params["SIM_BAT_DRAIN"].get("value")
        try:
            before_drain_seconds = float(before_drain)
        except (TypeError, ValueError):
            before_drain_seconds = 1800.0
        effective_drain_seconds = max(
            60.0,
            round(before_drain_seconds / max(float(battery_factor), 0.1), 3),
        )
        effective_motor_derate = max(0.1, min(1.0, float(motor_factor)))
        set_results = [
            param_set("SIM_BAT_MIN_PCT", 5.0),
            param_set("SIM_BAT_DRAIN", effective_drain_seconds),
        ]
        if effective_motor_derate < 0.999:
            set_results.append(param_set("MPC_THR_MAX", effective_motor_derate))
        applied_params = {item["param"]: item["requested_value"] for item in set_results}
        after_params = {
            "SIM_BAT_MIN_PCT": param_show("SIM_BAT_MIN_PCT"),
            "SIM_BAT_DRAIN": param_show("SIM_BAT_DRAIN"),
            "MPC_THR_MAX": param_show("MPC_THR_MAX"),
        }
        param_readback = {
            name: px4_param_value_matches(after_params.get(name) or {}, value)
            for name, value in applied_params.items()
        }
        params_set = all(px4_param_set_applied(item) for item in set_results)
        params_read_back = bool(param_readback) and all(param_readback.values())
        reset_battery_status_cache()
        sleep(1)
        battery_sample = battery_status_sample()
        if params_set and params_read_back:
            application_status = "applied_with_approximations"
            observation_status = "thermal_condition_param_readback_observed"
            thermal_capability_status = "supported"
            battery_drain_status = "supported"
            motor_derate_status = (
                "supported" if effective_motor_derate < 0.999 else "not_materialized"
            )
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            thermal_capability_status = "unsupported"
            battery_drain_status = "unsupported"
            motor_derate_status = (
                "unsupported" if effective_motor_derate < 0.999 else "not_materialized"
            )
            if not params_set:
                unsupported_reasons.append("px4_thermal_param_set_failed")
            if not params_read_back:
                unsupported_reasons.append("px4_thermal_param_readback_mismatch")
        applied = {
            "method": "px4_runtime_param_thermal_battery_motor_model",
            "target": "px4_runtime_params",
            "temperature_c": temperature_c,
            "pressure_hpa": pressure_hpa,
            "thermal_battery_drain_factor": battery_factor,
            "thermal_motor_derate_factor": effective_motor_derate,
            "baseline_sim_bat_drain_seconds": before_drain_seconds,
            "effective_sim_bat_drain_seconds": effective_drain_seconds,
            "applied_params": applied_params,
            "before_params": before_params,
            "after_params": after_params,
            "param_readback_matches_requested": param_readback,
            "thermal_air_physics_claimed": False,
            "motor_derate_param_materialized": effective_motor_derate < 0.999,
            "applied_at": observed_at_text,
        }
        observed = {
            "source": "px4-param-readback-and-battery-status-listener",
            "observed": params_set and params_read_back,
            "temperature_c": temperature_c,
            "pressure_hpa": pressure_hpa,
            "thermal_battery_drain_factor": battery_factor,
            "thermal_motor_derate_factor": effective_motor_derate,
            "thermal_air_physics_claimed": False,
            "battery_status": battery_sample,
            "battery_status_observed": battery_sample.get("battery_status_observed") is True,
            "param_readback_matches_requested": param_readback,
        }
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_thermal_weather",
        "thermal_weather": thermal_capability_status,
        "battery_drain_temperature_effect": battery_drain_status,
        "motor_derate_temperature_effect": motor_derate_status,
        "air_temperature_physics": "not_claimed",
        "pressure_physics": "not_claimed",
        "support_detection_method": (
            "px4_param_set_readback_and_battery_status_listener"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_thermal_weather",
        "condition_kind": "thermal_weather",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:mission_designer_thermal_weather",
        "condition_kind": "thermal_weather",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": observed_at_text,
        "delivery_completion_claimed": False,
    }
    return {
        "thermal_weather_condition_profile": profile,
        "thermal_weather_simulator_capability_matrix": capability,
        "thermal_weather_simulator_condition_application": application,
        "observed_thermal_weather_evidence": evidence,
    }


def run_sensor_failure_realism(
    *,
    profile: Mapping[str, Any],
    param_set: Callable[[str, float], Mapping[str, Any]],
    sensor_gps_sample: Callable[[], Mapping[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at_text = _observed_at_text(observed_at)
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    validation_reasons = list(profile.get("validation_reasons") or [])
    unsupported_reasons: list[str] = list(validation_reasons)
    component = requested.get("sensor_component")
    failure_type = requested.get("failure_type")
    capability_status = "not_requested"
    application_status = "not_requested"
    observation_status = "not_requested"
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    reset: dict[str, Any] = {}
    if requested_present and not validation_reasons and component and failure_type:
        before_sample = sensor_gps_sample()
        block_result = param_set("SIM_GZ_EN_GPS", 0.0)
        applied = {
            "method": "px4_sim_gz_en_gps_param",
            "block_param_result": block_result,
            "component": component,
            "failure_type": failure_type,
            "applied_at": observed_at_text,
        }
        block_param_applied = px4_param_set_applied(block_result)
        if block_param_applied:
            sleep(1)
            after_sample = sensor_gps_sample()
            baseline_observed = before_sample["sensor_gps_observed"] is True
            gps_stopped = (
                baseline_observed
                and failure_type == "off"
                and after_sample["sensor_gps_observed"] is False
            )
            capability_status = "supported" if gps_stopped else "unsupported"
            application_status = "applied" if gps_stopped else "unsupported"
            observation_status = (
                "sensor_failure_effect_observed"
                if gps_stopped
                else "sensor_failure_command_observed_effect_unconfirmed"
            )
            if not baseline_observed:
                unsupported_reasons.append("sensor_gps_baseline_not_observed")
                observation_status = "sensor_failure_baseline_not_observed"
            elif not gps_stopped:
                unsupported_reasons.append("sensor_failure_effect_not_observed")
            observed = {
                "source": "px4-listener:sensor_gps",
                "requested_sensor_component": component,
                "requested_failure_type": failure_type,
                "before_sensor_sample": before_sample,
                "after_sensor_sample": after_sample,
                "block_param_observed": block_param_applied,
                "baseline_sensor_gps_observed": baseline_observed,
                "sensor_failure_effect_observed": gps_stopped,
                "gps_sample_lost_after_injection": gps_stopped,
                "estimator_degradation_observed": False,
                "sensor_failure_does_not_verify_failsafe": True,
            }
        else:
            capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("sim_gz_en_gps_param_failed_or_unsupported")
            observed = {
                "source": "px4-param:SIM_GZ_EN_GPS",
                "requested_sensor_component": component,
                "requested_failure_type": failure_type,
                "before_sensor_sample": before_sample,
                "block_param_observed": False,
                "sensor_failure_effect_observed": False,
                "gps_sample_lost_after_injection": False,
                "estimator_degradation_observed": False,
            }
        reset = param_set("SIM_GZ_EN_GPS", 1.0)
        reset_applied = px4_param_set_applied(reset)
        reset["reset_observed"] = reset_applied
        if not reset_applied:
            capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "sensor_failure_cleanup_failed"
            unsupported_reasons.append("sensor_failure_cleanup_failed")
            observed["sensor_failure_effect_observed"] = False
            observed["gps_sample_lost_after_injection"] = False
            observed["cleanup_reset_observed"] = False
        else:
            observed["cleanup_reset_observed"] = True
    elif requested_present:
        capability_status = "unsupported"
        application_status = "unsupported"
        observation_status = "unsupported"
    capability = {
        "schema_version": "sensor_simulator_capability_matrix.v1",
        "capability_id": "sensor_simulator_capability_matrix:mission_designer_sensor_failure",
        "sensor_failure": capability_status,
        "support_detection_method": (
            "px4_param_set_sim_gz_en_gps_and_sensor_gps_readback"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "sensor_failure_injection_application.v1",
        "application_id": "sensor_failure_injection_application:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "reset": reset,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_sensor_condition_evidence.v1",
        "evidence_id": "observed_sensor_condition_evidence:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": observed_at_text,
        "delivery_completion_claimed": False,
    }
    return {
        "sensor_condition_profile": profile,
        "sensor_simulator_capability_matrix": capability,
        "sensor_failure_injection_application": application,
        "observed_sensor_condition_evidence": evidence,
    }


def project_landing_zone_blocked_realism(
    *,
    requested: bool,
    payload_model_root: Path | None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at_text = _observed_at_text(observed_at)
    profile = {
        "schema_version": "gazebo_world_condition_profile.v1",
        "condition_id": "gazebo_world_condition_profile:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "requested": {
            "landing_zone_blocked": requested,
            "dropoff_frame": "gazebo_world_local",
            "marker_pose_xyz_m": [5.0, 5.0, 0.025],
            "collision_enabled": False,
            "visual_only": True,
        },
        "requested_present": requested,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if requested:
        world_path = (
            None if payload_model_root is None else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            marker_present = False
            marker_visual_present = False
            marker_collision_present = False
            for model in ET.fromstring(world_text).iter("model"):
                if model.attrib.get("name") != "mission_designer_landing_zone_blocked_marker":
                    continue
                marker_present = True
                marker_visual_present = model.find(".//visual") is not None
                marker_collision_present = model.find(".//collision") is not None
                break
            if marker_present and marker_visual_present and not marker_collision_present:
                application_status = "applied"
                observation_status = "world_sdf_marker_observed"
                approximation_reasons.append(
                    "visual_only_marker_not_collision_or_landing_zone_verifier"
                )
                applied = {
                    "method": "gazebo_world_sdf_visual_marker",
                    "world_sdf_path": str(world_path),
                    "model_name": "mission_designer_landing_zone_blocked_marker",
                    "marker_pose_xyz_m": [5.0, 5.0, 0.025],
                    "collision_enabled": False,
                    "world_sdf_sha256": world_sha256,
                    "applied_at": observed_at_text,
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "model_name": "mission_designer_landing_zone_blocked_marker",
                    "visual_present": marker_visual_present,
                    "collision_present": marker_collision_present,
                    "world_sdf_sha256": world_sha256,
                    "landing_zone_blocked_does_not_verify_dropoff": True,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append("landing_zone_blocked_marker_not_materialized")
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "world_sdf_sha256": world_sha256,
                }
    capability_status = (
        "supported_visual_only"
        if application_status == "applied"
        else "unsupported"
        if unsupported_reasons
        else "not_requested"
    )
    capability = {
        "schema_version": "gazebo_world_capability_matrix.v1",
        "capability_id": "gazebo_world_capability_matrix:mission_designer_landing_zone_blocked",
        "landing_zone_blocked_marker": capability_status,
        "collision_enabled": False,
        "support_detection_method": (
            "gazebo_world_sdf_marker_presence" if requested else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "gazebo_world_application.v1",
        "application_id": "gazebo_world_application:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    obstacle_manifest = {
        "schema_version": "obstacle_manifest.v1",
        "manifest_id": "obstacle_manifest:mission_designer_landing_zone_blocked",
        "obstacles": (
            [
                {
                    "obstacle_id": "mission_designer_landing_zone_blocked_marker",
                    "kind": "landing_zone_blocked_marker",
                    "source": "gazebo_world_sdf",
                    "frame": "gazebo_world_local",
                    "visual_only": True,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                }
            ]
            if requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    evidence = {
        "schema_version": "observed_world_condition_evidence.v1",
        "evidence_id": "observed_world_condition_evidence:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": observed_at_text,
        "delivery_completion_claimed": False,
    }
    return {
        "gazebo_world_condition_profile": profile,
        "gazebo_world_capability_matrix": capability,
        "gazebo_world_application": application,
        "obstacle_manifest": obstacle_manifest,
        "observed_world_condition_evidence": evidence,
    }


def project_visibility_realism(
    *,
    mode: str | None,
    payload_model_root: Path | None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at_text = _observed_at_text(observed_at)
    requested_present = bool(mode)
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    if mode and mode not in ("fog", "smoke"):
        unsupported_reasons.append("visibility_mode_not_supported")
    if mode == "smoke":
        unsupported_reasons.append("smoke_visibility_mode_deferred_to_particle_slice")
    profile = {
        "schema_version": "visibility_condition_profile.v1",
        "condition_id": "visibility_condition_profile:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
        "requested": {
            "visibility_mode": mode if mode in ("fog", "smoke") else None,
            "fog_mode_requested": mode == "fog",
            "smoke_mode_requested": mode == "smoke",
            "render_only_marker_requested": mode == "fog",
            "smoke_deferred_to_followup_pr": mode == "smoke",
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if mode == "fog":
        world_path = (
            None if payload_model_root is None else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            marker_comment_present = VISIBILITY_FOG_RENDER_MARKER_ID in world_text
            fog_element_present = False
            fog_type_matches = False
            fog_density_matches = False
            fog_color_matches = False
            fog_start_matches = False
            fog_end_matches = False
            try:
                ET.fromstring(world_text)
                fog = visibility_marker_fog_element(world_text)
                if fog is not None:
                    fog_element_present = True
                    type_text = (fog.findtext("type") or "").strip()
                    density_text = (fog.findtext("density") or "").strip()
                    color_text = (fog.findtext("color") or "").strip()
                    start_text = (fog.findtext("start") or "").strip()
                    end_text = (fog.findtext("end") or "").strip()
                    fog_type_matches = type_text == VISIBILITY_FOG_RENDER_TYPE
                    fog_density_matches = density_text == VISIBILITY_FOG_RENDER_DENSITY
                    fog_color_matches = color_text == VISIBILITY_FOG_RENDER_COLOR
                    fog_start_matches = start_text == VISIBILITY_FOG_RENDER_START_M
                    fog_end_matches = end_text == VISIBILITY_FOG_RENDER_END_M
            except ET.ParseError:
                unsupported_reasons.append("visibility_world_sdf_parse_failed")
            fog_render_marker_materialized = bool(
                marker_comment_present
                and fog_element_present
                and fog_type_matches
                and fog_density_matches
                and fog_color_matches
                and fog_start_matches
                and fog_end_matches
            )
            if fog_render_marker_materialized:
                application_status = "applied_with_approximations"
                observation_status = "world_sdf_fog_render_marker_observed"
                approximation_reasons.append(
                    "scene_fog_render_marker_not_visibility_meters_or_sensor_effect"
                )
                applied = {
                    "method": "gazebo_world_sdf_scene_fog_render_marker",
                    "world_sdf_path": str(world_path),
                    "visibility_mode": mode,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "fog_type_requested": VISIBILITY_FOG_RENDER_TYPE,
                    "fog_density_requested": VISIBILITY_FOG_RENDER_DENSITY,
                    "fog_color_requested": VISIBILITY_FOG_RENDER_COLOR,
                    "fog_start_m_requested": VISIBILITY_FOG_RENDER_START_M,
                    "fog_end_m_requested": VISIBILITY_FOG_RENDER_END_M,
                    "visibility_fog_render_marker_materialized": True,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": True,
                    "gazebo_world_sdf_mutated": True,
                    "publisher_state_mutated": False,
                    "mission_upload_path_mutated": False,
                    "mission_progress_mutated": False,
                    "world_sdf_sha256": world_sha256,
                    "applied_at": observed_at_text,
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "fog_element_present": fog_element_present,
                    "fog_type_matches_requested": fog_type_matches,
                    "fog_density_matches_requested": fog_density_matches,
                    "fog_color_matches_requested": fog_color_matches,
                    "fog_start_matches_requested": fog_start_matches,
                    "fog_end_matches_requested": fog_end_matches,
                    "visibility_fog_render_marker_materialized": True,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": True,
                    "traffic_conflict_verified": False,
                    "route_blocking_verified": False,
                    "incident_verified": False,
                    "world_sdf_sha256": world_sha256,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append("visibility_fog_render_marker_not_materialized")
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "marker_comment_present": marker_comment_present,
                    "fog_element_present": fog_element_present,
                    "fog_type_matches_requested": fog_type_matches,
                    "fog_density_matches_requested": fog_density_matches,
                    "fog_color_matches_requested": fog_color_matches,
                    "fog_start_matches_requested": fog_start_matches,
                    "fog_end_matches_requested": fog_end_matches,
                    "visibility_fog_render_marker_materialized": False,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": False,
                    "world_sdf_sha256": world_sha256,
                }
    elif requested_present:
        application_status = "unsupported"
        observation_status = "unsupported"
    capability_status = (
        "supported_render_only"
        if application_status == "applied_with_approximations"
        else "unsupported"
        if unsupported_reasons
        else "not_requested"
    )
    capability = {
        "schema_version": "visibility_capability_matrix.v1",
        "capability_id": "visibility_capability_matrix:mission_designer_visibility",
        "fog_render_marker": capability_status,
        "smoke_render_marker": "deferred_to_followup_pr",
        "visibility_meters_target": "not_materialized",
        "support_detection_method": (
            "gazebo_world_sdf_scene_fog_presence" if requested_present else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "visibility_application.v1",
        "application_id": "visibility_application:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
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
        "schema_version": "observed_visibility_condition_evidence.v1",
        "evidence_id": "observed_visibility_condition_evidence:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": observed_at_text,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
    }
    return {
        "visibility_condition_profile": profile,
        "visibility_capability_matrix": capability,
        "visibility_application": application,
        "observed_visibility_condition_evidence": evidence,
    }


def project_operational_markers_realism(
    *,
    payload_model_root: Path | None,
    no_fly_zone_requested: bool,
    traffic_conflict_requested: bool,
    alternate_landing_requested: bool,
    rth_behavior_requested: bool,
    moving_actor_requested: bool,
    collision_obstacle_requested: bool,
    collision_contact_topic_requested: bool,
    multi_drone_conflict_probe_requested: bool,
    moving_actor_motion_spec: Mapping[str, Any],
    moving_actor_trajectory_definition_sha256: str,
    collision_obstacle_motion_spec: Mapping[str, Any],
    collision_obstacle_contact_topic: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at_text = _observed_at_text(observed_at)
    requested = (
        no_fly_zone_requested
        or traffic_conflict_requested
        or alternate_landing_requested
        or rth_behavior_requested
        or moving_actor_requested
        or collision_obstacle_requested
        or multi_drone_conflict_probe_requested
    )
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    visual_marker_requested = (
        no_fly_zone_requested
        or traffic_conflict_requested
        or alternate_landing_requested
        or moving_actor_requested
        or collision_obstacle_requested
    )
    profile = {
        "schema_version": "operational_condition_profile.v1",
        "condition_id": "operational_condition_profile:mission_designer_operational_markers",
        "condition_kind": (
            "moving_visual_actor_marker"
            if moving_actor_requested
            and not no_fly_zone_requested
            and not traffic_conflict_requested
            and not alternate_landing_requested
            and not collision_obstacle_requested
            else (
                "collision_enabled_moving_obstacle"
                if collision_obstacle_requested
                and not no_fly_zone_requested
                and not traffic_conflict_requested
                and not alternate_landing_requested
                and not moving_actor_requested
                and not multi_drone_conflict_probe_requested
                else (
                    "alternate_landing_visual_marker"
                    if alternate_landing_requested
                    and not no_fly_zone_requested
                    and not traffic_conflict_requested
                    and not moving_actor_requested
                    and not collision_obstacle_requested
                    and not rth_behavior_requested
                    else (
                        "traffic_conflict_visual_marker"
                        if traffic_conflict_requested
                        and not no_fly_zone_requested
                        and not alternate_landing_requested
                        and not moving_actor_requested
                        and not collision_obstacle_requested
                        and not multi_drone_conflict_probe_requested
                        else (
                            "multi_drone_conflict_support_detection"
                            if multi_drone_conflict_probe_requested
                            and not no_fly_zone_requested
                            and not traffic_conflict_requested
                            and not alternate_landing_requested
                            and not moving_actor_requested
                            and not collision_obstacle_requested
                            else (
                                "operational_visual_markers"
                                if (
                                    (traffic_conflict_requested and no_fly_zone_requested)
                                    or (
                                        alternate_landing_requested
                                        and (no_fly_zone_requested or traffic_conflict_requested)
                                    )
                                    or rth_behavior_requested
                                    or (
                                        moving_actor_requested
                                        and (
                                            no_fly_zone_requested
                                            or traffic_conflict_requested
                                            or alternate_landing_requested
                                        )
                                    )
                                    or collision_obstacle_requested
                                    or (
                                        multi_drone_conflict_probe_requested
                                        and (
                                            no_fly_zone_requested
                                            or traffic_conflict_requested
                                            or alternate_landing_requested
                                            or moving_actor_requested
                                            or collision_obstacle_requested
                                        )
                                    )
                                )
                                else "no_fly_zone_visual_marker"
                            )
                        )
                    )
                )
            )
        ),
        "requested": {
            "no_fly_zone_marker": no_fly_zone_requested,
            "traffic_conflict_marker": traffic_conflict_requested,
            "alternate_landing_marker": alternate_landing_requested,
            "return_to_home_behavior": rth_behavior_requested,
            "moving_actor_marker": moving_actor_requested,
            "collision_obstacle": collision_obstacle_requested,
            "multi_drone_conflict_probe": multi_drone_conflict_probe_requested,
            "frame": "gazebo_world_local",
            "no_fly_zone_center_xy_m": [2.5, 2.5],
            "no_fly_zone_radius_m": 1.25,
            "traffic_conflict_xy_m": [3.6, 2.9],
            "alternate_landing_xy_m": [-2.0, 3.5],
            "moving_actor_start_xy_m": [1.2, -0.7],
            "moving_actor_end_xy_m": [4.2, 3.2],
            "moving_actor_loop_seconds": 6.0,
            "moving_actor_mode": "linear_waypoint_motion",
            "moving_actor_nominal_profile_velocity_mps": (
                moving_actor_motion_spec["nominal_profile_velocity_mps"]
            ),
            "collision_obstacle_start_xy_m": collision_obstacle_motion_spec["start_xy_m"],
            "collision_obstacle_end_xy_m": collision_obstacle_motion_spec["end_xy_m"],
            "collision_obstacle_loop_seconds": collision_obstacle_motion_spec["loop_seconds"],
            "enforcement_enabled": False,
            "traffic_motion_enabled": False,
            "moving_actor_sdf_scripted_motion_enabled": (True if moving_actor_requested else False),
            "collision_enabled": False,
            "collision_obstacle_collision_enabled": collision_obstacle_requested,
            "collision_obstacle_contact_topic_enabled": (
                collision_obstacle_requested and collision_contact_topic_requested
            ),
            "sensor_visible_claimed": False,
            "incident_claimed": False,
            "route_blocking_enabled": False,
            "alternate_landing_behavior_enabled": False,
            "return_to_home_behavior_enabled": rth_behavior_requested,
            "multi_vehicle_enabled": False,
            "multi_drone_conflict_verifier_enabled": False,
            "explicit_vehicle_ids": [],
            "visual_only": visual_marker_requested,
        },
        "requested_present": requested,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if requested:
        if multi_drone_conflict_probe_requested:
            unsupported_reasons.extend(
                [
                    "multi_drone_support_not_implemented",
                    "multi_drone_probe_not_traffic_conflict_verifier",
                ]
            )
        world_path = (
            None if payload_model_root is None else payload_model_root / "worlds" / "default.sdf"
        )
        if multi_drone_conflict_probe_requested and not visual_marker_requested:
            application_status = "unsupported"
            observation_status = "unsupported"
            observed = {
                "source": "operational_support_detection",
                "observed": False,
                "multi_drone_conflict_probe_requested": True,
                "multi_vehicle_enabled": False,
                "explicit_vehicle_ids_observed": [],
                "multi_drone_conflict_verified": False,
                "route_blocking_observed": False,
                "incident_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        elif world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            no_fly_marker_present = False
            no_fly_marker_visual_present = False
            no_fly_marker_collision_present = False
            traffic_marker_present = False
            traffic_marker_visual_present = False
            traffic_marker_collision_present = False
            alternate_marker_present = False
            alternate_marker_visual_present = False
            alternate_marker_collision_present = False
            moving_actor_present = False
            moving_actor_script_present = False
            moving_actor_visual_present = False
            moving_actor_collision_present = False
            collision_obstacle_present = False
            collision_obstacle_visual_present = False
            collision_obstacle_collision_present = False
            collision_obstacle_script_present = False
            collision_obstacle_contact_sensor_present = False
            for model in ET.fromstring(world_text).iter("model"):
                model_name = model.attrib.get("name")
                if model_name == "mission_designer_no_fly_zone_marker":
                    no_fly_marker_present = True
                    no_fly_marker_visual_present = model.find(".//visual") is not None
                    no_fly_marker_collision_present = model.find(".//collision") is not None
                if model_name == "mission_designer_traffic_conflict_marker":
                    traffic_marker_present = True
                    traffic_marker_visual_present = model.find(".//visual") is not None
                    traffic_marker_collision_present = model.find(".//collision") is not None
                if model_name == "mission_designer_alternate_landing_marker":
                    alternate_marker_present = True
                    alternate_marker_visual_present = model.find(".//visual") is not None
                    alternate_marker_collision_present = model.find(".//collision") is not None
                if model_name == "mission_designer_moving_actor_marker":
                    moving_actor_present = True
                    moving_actor_visual_present = model.find(".//visual") is not None
                    moving_actor_script_present = (
                        model.find(".//plugin[@name='gz::sim::systems::TrajectoryFollower']")
                        is not None
                    )
                    moving_actor_collision_present = model.find(".//collision") is not None
                if model_name == "mission_designer_collision_obstacle":
                    collision_obstacle_present = True
                    collision_obstacle_visual_present = model.find(".//visual") is not None
                    collision_obstacle_collision_present = model.find(".//collision") is not None
                    collision_obstacle_contact_sensor_present = (
                        model.find(".//sensor[@type='contact']") is not None
                    )
                    collision_obstacle_script_present = (
                        model.find(".//plugin[@name='gz::sim::systems::TrajectoryFollower']")
                        is not None
                    )
            for actor in ET.fromstring(world_text).iter("actor"):
                actor_name = actor.attrib.get("name")
                if actor_name == "mission_designer_moving_actor_marker":
                    moving_actor_present = True
                    moving_actor_script_present = actor.find(".//script/trajectory") is not None
                    moving_actor_collision_present = actor.find(".//collision") is not None
            no_fly_ok = not no_fly_zone_requested or (
                no_fly_marker_present
                and no_fly_marker_visual_present
                and not no_fly_marker_collision_present
            )
            traffic_ok = not traffic_conflict_requested or (
                traffic_marker_present
                and traffic_marker_visual_present
                and not traffic_marker_collision_present
            )
            alternate_ok = not alternate_landing_requested or (
                alternate_marker_present
                and alternate_marker_visual_present
                and not alternate_marker_collision_present
            )
            moving_actor_ok = not moving_actor_requested or (
                moving_actor_present
                and moving_actor_visual_present
                and moving_actor_script_present
                and not moving_actor_collision_present
            )
            collision_obstacle_ok = not collision_obstacle_requested or (
                collision_obstacle_present
                and collision_obstacle_visual_present
                and collision_obstacle_collision_present
                and (
                    collision_obstacle_contact_sensor_present
                    if collision_contact_topic_requested
                    else True
                )
                and collision_obstacle_script_present
            )
            if (
                no_fly_ok
                and traffic_ok
                and alternate_ok
                and moving_actor_ok
                and collision_obstacle_ok
            ):
                application_status = "applied_with_approximations"
                observation_status = "world_sdf_operational_markers_observed"
                if no_fly_zone_requested:
                    approximation_reasons.append("visual_only_marker_not_geofence_enforcement")
                if traffic_conflict_requested:
                    approximation_reasons.append(
                        "visual_only_marker_not_dynamic_traffic_or_collision"
                    )
                if alternate_landing_requested:
                    approximation_reasons.append(
                        "visual_only_marker_not_alternate_landing_or_rth_behavior"
                    )
                if moving_actor_requested:
                    approximation_reasons.append(
                        "visual_only_actor_not_collision_sensor_visible_incident_or_route_blocking"
                    )
                if collision_obstacle_requested:
                    approximation_reasons.append(
                        "collision_obstacle_not_traffic_conflict_route_blocking_incident_or_gate"
                    )
                applied = {
                    "method": "gazebo_world_sdf_operational_visual_markers",
                    "world_sdf_path": str(world_path),
                    "model_names": [
                        *(["mission_designer_no_fly_zone_marker"] if no_fly_zone_requested else []),
                        *(
                            ["mission_designer_traffic_conflict_marker"]
                            if traffic_conflict_requested
                            else []
                        ),
                        *(
                            ["mission_designer_alternate_landing_marker"]
                            if alternate_landing_requested
                            else []
                        ),
                        *(
                            ["mission_designer_moving_actor_marker"]
                            if moving_actor_requested
                            else []
                        ),
                        *(
                            ["mission_designer_collision_obstacle"]
                            if collision_obstacle_requested
                            else []
                        ),
                    ],
                    "no_fly_zone_center_xy_m": [2.5, 2.5],
                    "no_fly_zone_radius_m": 1.25,
                    "traffic_conflict_xy_m": [3.6, 2.9],
                    "alternate_landing_xy_m": [-2.0, 3.5],
                    "moving_actor_start_xy_m": [1.2, -0.7],
                    "moving_actor_end_xy_m": [4.2, 3.2],
                    "moving_actor_loop_seconds": 6.0,
                    "moving_actor_mode": "linear_waypoint_motion",
                    "moving_actor_nominal_profile_velocity_mps": (
                        moving_actor_motion_spec["nominal_profile_velocity_mps"]
                    ),
                    "moving_actor_trajectory_definition_sha256": (
                        moving_actor_trajectory_definition_sha256
                    ),
                    "collision_obstacle_start_xy_m": (collision_obstacle_motion_spec["start_xy_m"]),
                    "collision_obstacle_end_xy_m": (collision_obstacle_motion_spec["end_xy_m"]),
                    "collision_obstacle_loop_seconds": (
                        collision_obstacle_motion_spec["loop_seconds"]
                    ),
                    "enforcement_enabled": False,
                    "traffic_motion_enabled": False,
                    "moving_actor_scripted_motion_enabled": moving_actor_requested,
                    "collision_enabled": collision_obstacle_requested,
                    "collision_obstacle_enabled": collision_obstacle_requested,
                    "collision_obstacle_contact_sensor_enabled": (collision_obstacle_requested),
                    "collision_obstacle_contact_topic": (
                        collision_obstacle_contact_topic if collision_obstacle_requested else ""
                    ),
                    "sensor_visible_claimed": False,
                    "incident_claimed": False,
                    "route_blocking_enabled": False,
                    "alternate_landing_behavior_enabled": False,
                    "return_to_home_behavior_enabled": False,
                    "multi_vehicle_enabled": False,
                    "multi_drone_conflict_verifier_enabled": False,
                    "explicit_vehicle_ids": [],
                    "world_sdf_sha256": world_sha256,
                    "applied_at": observed_at_text,
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "no_fly_zone_model_name": (
                        "mission_designer_no_fly_zone_marker" if no_fly_zone_requested else ""
                    ),
                    "traffic_conflict_model_name": (
                        "mission_designer_traffic_conflict_marker"
                        if traffic_conflict_requested
                        else ""
                    ),
                    "alternate_landing_model_name": (
                        "mission_designer_alternate_landing_marker"
                        if alternate_landing_requested
                        else ""
                    ),
                    "moving_actor_name": (
                        "mission_designer_moving_actor_marker" if moving_actor_requested else ""
                    ),
                    "collision_obstacle_name": (
                        "mission_designer_collision_obstacle"
                        if collision_obstacle_requested
                        else ""
                    ),
                    "no_fly_zone_visual_present": no_fly_marker_visual_present,
                    "traffic_conflict_visual_present": traffic_marker_visual_present,
                    "alternate_landing_visual_present": alternate_marker_visual_present,
                    "moving_actor_script_present": False,
                    "moving_actor_sdf_motion_present": moving_actor_script_present,
                    "moving_actor_trajectory_follower_present": moving_actor_script_present,
                    "moving_actor_visual_present": moving_actor_visual_present,
                    "collision_present": (
                        no_fly_marker_collision_present
                        or traffic_marker_collision_present
                        or alternate_marker_collision_present
                        or moving_actor_collision_present
                        or collision_obstacle_collision_present
                    ),
                    "collision_obstacle_visual_present": collision_obstacle_visual_present,
                    "collision_obstacle_collision_present": collision_obstacle_collision_present,
                    "collision_obstacle_contact_sensor_present": (
                        collision_obstacle_contact_sensor_present
                    ),
                    "collision_obstacle_contact_topic": (
                        collision_obstacle_contact_topic
                        if collision_obstacle_contact_sensor_present
                        else ""
                    ),
                    "collision_obstacle_trajectory_follower_present": collision_obstacle_script_present,
                    "geofence_enforcement_observed": False,
                    "dynamic_traffic_observed": False,
                    "moving_actor_sdf_script_observed": False,
                    "moving_actor_sdf_motion_observed": moving_actor_script_present,
                    "moving_actor_trajectory_follower_observed": moving_actor_script_present,
                    "moving_actor_collision_observed": False,
                    "moving_actor_sensor_evidence_observed": False,
                    "moving_actor_incident_observed": False,
                    "moving_actor_route_blocking_observed": False,
                    "collision_obstacle_route_blocking_observed": False,
                    "collision_obstacle_incident_observed": False,
                    "collision_obstacle_contact_observed": False,
                    "traffic_conflict_sensor_evidence_observed": False,
                    "alternate_landing_behavior_observed": False,
                    "return_to_home_behavior_observed": False,
                    "multi_drone_conflict_probe_observed": False,
                    "multi_vehicle_observed": False,
                    "explicit_vehicle_ids_observed": [],
                    "multi_drone_conflict_verified": False,
                    "world_sdf_sha256": world_sha256,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                if not no_fly_ok:
                    unsupported_reasons.append("no_fly_zone_marker_not_materialized")
                if not traffic_ok:
                    unsupported_reasons.append("traffic_conflict_marker_not_materialized")
                if not alternate_ok:
                    unsupported_reasons.append("alternate_landing_marker_not_materialized")
                if not moving_actor_ok:
                    unsupported_reasons.append("moving_actor_marker_not_materialized")
                if not collision_obstacle_ok:
                    unsupported_reasons.append("collision_obstacle_not_materialized")
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "world_sdf_sha256": world_sha256,
                }
    capability_status = (
        "supported_visual_only"
        if application_status == "applied_with_approximations"
        else "unsupported"
        if unsupported_reasons
        else "not_requested"
    )
    geofence = {
        "schema_version": "geofence_condition_profile.v1",
        "geofence_id": "geofence_condition_profile:mission_designer_no_fly_zone",
        "geofences": (
            [
                {
                    "geofence_id": "mission_designer_no_fly_zone_marker",
                    "frame": "gazebo_world_local",
                    "center_xy_m": [2.5, 2.5],
                    "radius_m": 1.25,
                    "visual_only": True,
                    "enforcement_enabled": False,
                }
            ]
            if no_fly_zone_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    traffic_conflict = {
        "schema_version": "traffic_conflict_profile.v1",
        "traffic_conflict_id": "traffic_conflict_profile:mission_designer_visual_marker",
        "conflicts": (
            [
                {
                    "conflict_id": "mission_designer_traffic_conflict_marker",
                    "frame": "gazebo_world_local",
                    "position_xy_m": [3.6, 2.9],
                    "visual_only": True,
                    "dynamic_motion_enabled": False,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                }
            ]
            if traffic_conflict_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    alternate_landing = {
        "schema_version": "alternate_landing_profile.v1",
        "alternate_landing_id": "alternate_landing_profile:mission_designer_visual_marker",
        "candidates": (
            [
                {
                    "candidate_id": "mission_designer_alternate_landing_marker",
                    "frame": "gazebo_world_local",
                    "position_xy_m": [-2.0, 3.5],
                    "visual_only": True,
                    "alternate_landing_behavior_enabled": False,
                    "return_to_home_behavior_enabled": False,
                    "landing_zone_verified": False,
                    "collision_enabled": False,
                }
            ]
            if alternate_landing_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    dynamic_actor = {
        "schema_version": "dynamic_actor_profile.v1",
        "dynamic_actor_id": "dynamic_actor_profile:mission_designer_moving_visual_marker",
        "actors": (
            [
                {
                    "actor_id": "mission_designer_moving_actor_marker",
                    "sdf_entity_type": "model",
                    "frame": "gazebo_world_local",
                    "start_xy_m": [1.2, -0.7],
                    "end_xy_m": [4.2, 3.2],
                    "loop_seconds": 6.0,
                    "mode": "linear_waypoint_motion",
                    "nominal_profile_velocity_mps": (
                        moving_actor_motion_spec["nominal_profile_velocity_mps"]
                    ),
                    "trajectory_definition_sha256": (moving_actor_trajectory_definition_sha256),
                    "visual_only": True,
                    "sdf_scripted_motion_enabled": True,
                    "trajectory_follower_plugin_enabled": True,
                    "gravity_enabled": False,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                    "route_blocking_enabled": False,
                    "incident_claimed": False,
                }
            ]
            if moving_actor_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    collision_motion = collision_obstacle_motion_spec
    collision_obstacle = {
        "schema_version": "collision_obstacle_profile.v1",
        "obstacle_id": "collision_obstacle_profile:mission_designer_collision_obstacle",
        "condition_kind": "collision_enabled_moving_obstacle",
        "requested_present": collision_obstacle_requested,
        "obstacles": (
            [
                {
                    "obstacle_id": "mission_designer_collision_obstacle",
                    "sdf_entity_type": "model",
                    "frame": "gazebo_world_local",
                    "start_xy_m": collision_motion["start_xy_m"],
                    "end_xy_m": collision_motion["end_xy_m"],
                    "loop_seconds": collision_motion["loop_seconds"],
                    "mode": collision_motion["mode"],
                    "visual_only": False,
                    "collision_enabled": True,
                    "contact_sensor_enabled": collision_contact_topic_requested,
                    "contact_topic": (
                        collision_obstacle_contact_topic
                        if collision_contact_topic_requested
                        else ""
                    ),
                    "trajectory_follower_plugin_enabled": True,
                    "sensor_visible_claimed": False,
                    "route_blocking_enabled": False,
                    "incident_claimed": False,
                    "traffic_conflict_verifier": False,
                }
            ]
            if collision_obstacle_requested
            else []
        ),
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    multi_vehicle_frame_contract = {
        "schema_version": "multi_vehicle_frame_contract.v1",
        "contract_id": "multi_vehicle_frame_contract:mission_designer_primary_vehicle_only",
        "condition_kind": "multi_drone_conflict_support_detection",
        "requested_present": multi_drone_conflict_probe_requested,
        "frame": "gazebo_world_local",
        "primary_vehicle_id": "x500_0",
        "primary_vehicle_frame": "gazebo_world_local",
        "additional_vehicle_ids": [],
        "additional_vehicle_frames": [],
        "multi_vehicle_enabled": False,
        "conflict_verifier_enabled": False,
        "traffic_conflict_verified": False,
        "route_blocking_enabled": False,
        "incident_claimed": False,
        "unsupported_until_explicit_vehicle_ids_and_observer": True,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    capability = {
        "schema_version": "operational_capability_matrix.v1",
        "capability_id": "operational_capability_matrix:mission_designer_operational_markers",
        "no_fly_zone_marker": (capability_status if no_fly_zone_requested else "not_requested"),
        "traffic_conflict_marker": (
            capability_status if traffic_conflict_requested else "not_requested"
        ),
        "alternate_landing_marker": (
            capability_status if alternate_landing_requested else "not_requested"
        ),
        "moving_actor_marker": (capability_status if moving_actor_requested else "not_requested"),
        "collision_obstacle": (
            "supported_collision_geometry"
            if collision_obstacle_requested and application_status == "applied_with_approximations"
            else "unsupported"
            if collision_obstacle_requested
            else "not_requested"
        ),
        "geofence_enforcement": "unsupported",
        "dynamic_traffic_motion": "unsupported",
        "moving_actor_collision": "unsupported",
        "moving_actor_sensor_visibility": "unsupported",
        "moving_actor_incident_evidence": "unsupported",
        "moving_actor_route_blocking": "unsupported",
        "traffic_collision": "unsupported",
        "traffic_sensor_visibility": "unsupported",
        "collision_obstacle_contact_evidence": (
            "supported_contact_topic_observer"
            if collision_obstacle_requested
            and collision_contact_topic_requested
            and application_status == "applied_with_approximations"
            else (
                "unsupported"
                if collision_obstacle_requested and collision_contact_topic_requested
                else ("not_requested" if collision_obstacle_requested else "not_requested")
            )
        ),
        "collision_obstacle_route_blocking": "unsupported",
        "collision_obstacle_incident_evidence": "unsupported",
        "multi_drone_conflict_probe": (
            "unsupported" if multi_drone_conflict_probe_requested else "not_requested"
        ),
        "multi_vehicle_simulation": "unsupported",
        "multi_drone_conflict_verifier": "unsupported",
        "explicit_vehicle_frame_binding": "unsupported",
        "alternate_landing_behavior": "unsupported",
        "return_to_home_behavior": (
            "supported_sitl_only" if rth_behavior_requested else "not_requested"
        ),
        "alternate_landing_zone_verification": "unsupported",
        "support_detection_method": (
            "gazebo_world_sdf_marker_presence" if requested else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "operational_application.v1",
        "application_id": "operational_application:mission_designer_operational_markers",
        "condition_kind": profile["condition_kind"],
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_operational_condition_evidence.v1",
        "evidence_id": "observed_operational_condition_evidence:mission_designer_operational_markers",
        "condition_kind": profile["condition_kind"],
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": observed_at_text,
        "delivery_completion_claimed": False,
    }
    return {
        "operational_condition_profile": profile,
        "geofence_condition_profile": geofence,
        "traffic_conflict_profile": traffic_conflict,
        "alternate_landing_profile": alternate_landing,
        "dynamic_actor_profile": dynamic_actor,
        "collision_obstacle_profile": collision_obstacle,
        "multi_vehicle_frame_contract": multi_vehicle_frame_contract,
        "operational_capability_matrix": capability,
        "operational_application": application,
        "observed_operational_condition_evidence": evidence,
    }


__all__ = [
    "project_landing_zone_blocked_realism",
    "project_operational_markers_realism",
    "project_visibility_realism",
    "run_sensor_failure_realism",
    "run_thermal_weather_realism",
]
