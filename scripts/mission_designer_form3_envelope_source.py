"""Shared operational-envelope source fields for Mission Designer Form 3 audits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from scripts.smoke_px4_gazebo_horizontal_route_delivery import PX4_GAZEBO_IMAGE


MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF = (
    "mission_contract:mission_designer_horizontal_route_form3"
)
MISSION_DESIGNER_FORM3_TASK_GRAPH_REF = (
    "task_graph:mission_designer_horizontal_route_form3"
)
MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE = "px4_gazebo"
MISSION_DESIGNER_FORM3_SIM_VERSION = "gz-sim-horizontal-route-smoke"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def world_sdf_hash_from_summary(summary: Mapping[str, Any]) -> str:
    """Extract the source-bound SDF hash already recorded by the route smoke."""

    observed_environment = _as_mapping(
        _as_mapping(summary.get("observed_environment_evidence")).get("observed")
    )
    gazebo_world_application = _as_mapping(summary.get("gazebo_world_application"))
    gazebo_world_applied = _as_mapping(gazebo_world_application.get("applied"))
    obstacle_spawn = _as_mapping(summary.get("gazebo_route_corridor_obstacle_spawn_application"))
    obstacle_observed = _as_mapping(obstacle_spawn.get("observed"))
    obstacle_applied = _as_mapping(obstacle_spawn.get("applied"))
    return _first_string(
        observed_environment.get("world_sdf_sha256"),
        gazebo_world_applied.get("world_sdf_sha256"),
        obstacle_observed.get("world_sdf_sha256"),
        obstacle_applied.get("world_sdf_sha256"),
    )


def build_form3_backend_context(
    summary: Mapping[str, Any],
    *,
    applicator_chain_refs: Iterable[str],
    verifier_version: str,
    audit_script_version: str,
) -> dict[str, Any]:
    """Build the backend context required before envelope ingestion can be ready."""

    refs = [ref for ref in dict.fromkeys(str(ref) for ref in applicator_chain_refs) if ref]
    return {
        "backend_type": MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE,
        "image_version": PX4_GAZEBO_IMAGE,
        "sim_version": MISSION_DESIGNER_FORM3_SIM_VERSION,
        "sdf_hash": world_sdf_hash_from_summary(summary),
        "applicator_chain_refs": refs,
        "verifier_version": verifier_version,
        "audit_script_version": audit_script_version,
    }


def parameter_observation(
    *,
    parameter: str,
    value: float,
    unit: str,
    source_ref: str,
) -> dict[str, Any]:
    return {
        "parameter": parameter,
        "value": float(value),
        "unit": unit,
        "source_ref": source_ref,
    }


def build_wind_parameterized_sdf_delta_proof(
    summary: Mapping[str, Any],
    *,
    wind_speed_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    source_ref: str,
) -> dict[str, Any]:
    """Build source-bound proof for wind parameterized SDF normalization."""

    application = _as_mapping(summary.get("simulator_condition_application"))
    applied = _as_mapping(application.get("applied"))
    raw_sdf_hash = world_sdf_hash_from_summary(summary)
    applied_file_sha256 = _first_string(applied.get("applied_file_sha256"))
    applied_fields = applied.get("applied_fields")
    applied_fields = applied_fields if isinstance(applied_fields, list) else []
    applied_mps = applied.get("applied_mps", applied.get("wind_vector_x_mps"))
    applied_direction = applied.get("applied_direction_deg")
    checks = {
        "source_condition_application_ref_matches": application.get("application_id")
        == source_ref,
        "application_status_applied": application.get("application_status") == "applied",
        "applied_file_hash_matches_backend_context": bool(raw_sdf_hash)
        and applied_file_sha256 == raw_sdf_hash,
        "applied_fields_present": bool(applied_fields),
        "applied_fields_are_wind_only": set(str(item) for item in applied_fields)
        <= {"wind_mean_mps", "wind_direction_deg"},
        "wind_speed_matches_parameter": float(applied_mps or 0.0)
        == float(wind_speed_mps),
        "wind_direction_matches_parameter": float(applied_direction or 0.0)
        == float(wind_direction_deg),
        "wind_world_linear_velocity_matches_requested": applied.get(
            "wind_world_linear_velocity_matches_requested"
        )
        is True,
        "wind_effects_plugin_materialized": applied.get(
            "wind_effects_plugin_materialized"
        )
        is True,
        "wind_enabled_on_vehicle_links": applied.get("wind_enabled_on_vehicle_links")
        is True,
        "hardware_target_not_allowed": application.get("hardware_target_allowed")
        is False,
        "physical_execution_not_invoked": application.get(
            "physical_execution_invoked"
        )
        is False,
        "delivery_completion_not_claimed": application.get(
            "delivery_completion_claimed"
        )
        is False,
    }
    unsupported = [name for name, passed in checks.items() if passed is not True]
    proof_supported = not unsupported
    return {
        "schema_version": "parameterized_sdf_delta_proof.v1",
        "proof_id": "parameterized_sdf_delta_proof:wind_form3_sdf_delta",
        "proof_status": "source_bound" if proof_supported else "unsupported",
        "proof_supported": proof_supported,
        "condition_kind": "source_bound_wind_drift_form3_closed_loop",
        "source_condition_application_ref": source_ref,
        "raw_sdf_hash": raw_sdf_hash,
        "applied_file_sha256": applied_file_sha256,
        "applied_file_path": str(applied.get("applied_file_path") or ""),
        "applied_message_sha256": str(applied.get("applied_message_sha256") or ""),
        "applied_fields": [str(item) for item in applied_fields],
        "parameter_values": {
            "wind_speed_mps": {"value": float(wind_speed_mps), "unit": "m/s"},
            "wind_direction_deg": {"value": float(wind_direction_deg), "unit": "deg"},
            "wind_drift_threshold_m": {
                "value": float(drift_threshold_m),
                "unit": "m",
            },
        },
        "checks": checks,
        "unsupported_reasons": unsupported,
    }
