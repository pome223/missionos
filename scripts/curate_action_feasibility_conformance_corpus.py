#!/usr/bin/env python3
"""Curate deterministic, publication-safe PX4 feasibility replay cases."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    CORPUS_CASE_SCHEMA_VERSION,
    CORPUS_MANIFEST_SCHEMA_VERSION,
    seal_action_feasibility_corpus_case,
    verify_action_feasibility_corpus,
)
from src.runtime.px4_gazebo_route.core_action_feasibility_adapter import (
    build_runtime_recovery_hazard_state,
    verify_runtime_recovery_action_feasibility,
)


DEFAULT_OUTPUT = Path("tests/golden/action_feasibility/px4_v1")
OBSERVED_AT = "2026-07-23T12:00:00+00:00"
SOURCE_RUNTIME_EVIDENCE_REFS = [
    "maintainer_evidence:px4_action_feasibility_composite_deepseek_e2e",
    "release_validation:public_v0.1.0-rc.4",
]


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _policy() -> dict[str, Any]:
    return {
        "policy_ref": "px4_action_feasibility_conformance_policy.v1",
        "battery_return_threshold_percent": 20.0,
        "min_terrain_clearance_m": 30.0,
        "max_recovery_duration_s": 75.0,
        "max_obstacle_avoidance_duration_s": 120.0,
        "max_recovery_horizontal_speed_mps": 10.0,
        "max_recovery_vertical_speed_mps": 3.0,
        "max_wind_speed_mps": 6.0,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5.0,
        "minimum_motor_thrust_factor": 0.6,
        "obstacle_minimum_clearance_m": 20.0,
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
        "temperature_derating_model": {
            "model_id": "px4_sitl_temperature_derating.v1",
            "model_version": "1.0.0",
            "source_refs": ["policy.temperature_derating_model"],
            "uncertainty_percent": 5.0,
            "battery_capacity_factor": 0.95,
            "motor_thrust_factor": 0.9,
        },
        "battery_action_energy_model": {
            "model_id": "px4_sitl_action_energy.v1",
            "model_version": "1.0.0",
            "source_refs": ["policy.action_energy_model"],
            "uncertainty_percent": 5.0,
            "percent_per_meter_horizontal": 0.02,
            "percent_per_meter_climb": 0.08,
            "headwind_multiplier_per_mps": 0.01,
            "payload_energy_multiplier_per_kg": 0.05,
        },
    }


def _telemetry() -> dict[str, Any]:
    return {
        "source": "anonymized_px4_sitl_observation",
        "sample_index": 140,
        "elapsed_seconds": 163.207,
        "telemetry": {"stale": False, "dropout": False},
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
            "distance_to_home_m": 128.0,
            "frame_id": "local_ned_xy_altitude_up",
            "source_refs": ["runtime_readback.local_position"],
        },
        "battery": {
            "remaining_percent": 91.8,
            "source_refs": ["runtime_readback.battery_status"],
        },
        "wind": {
            "speed_mps": 4.0,
            "gust_mps": 4.8,
            "source_refs": ["runtime_readback.wind_window"],
        },
        "terrain": {
            "terrain_clearance_m": 50.0,
            "terrain_clearance_target_m": 30.0,
            "frame_id": "amsl",
            "source_refs": ["runtime_readback.terrain_clearance"],
        },
        "obstacle": {
            "frame_id": "local_ned_xy_altitude_up",
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "route_obstacle",
                        "bounds_local_xyz_m": {
                            "min_x_m": 28.0,
                            "max_x_m": 32.0,
                            "min_y_m": -9.0,
                            "max_y_m": 9.0,
                            "min_z_m": 0.0,
                            "max_z_m": 20.0,
                        },
                        "source_refs": [
                            "runtime_readback.obstacle_pose_and_bounds"
                        ],
                    }
                ]
            },
            "conflict_assessment": {
                "local_avoidance_required": True,
                "source_refs": ["runtime_readback.local_conflict"],
                "nearest_obstacle": {
                    "obstacle_name": "route_obstacle",
                    "time_to_conflict_s": 60.0,
                    "source_refs": [
                        "runtime_readback.obstacle_pose_and_bounds"
                    ],
                },
            },
        },
        "temperature": {
            "temperature_c": 38.0,
            "battery_capacity_factor": 0.95,
            "motor_thrust_factor": 0.9,
            "model": {
                "model_id": "px4_sitl_temperature_derating.v1",
                "model_version": "1.0.0",
                "source_refs": [
                    "runtime_readback.px4_param_get",
                    "runtime_readback.route_temperature",
                ],
                "uncertainty_percent": 5.0,
            },
            "source_refs": [
                "runtime_readback.px4_param_get",
                "runtime_readback.route_temperature",
            ],
        },
        "payload": {
            "requested_mass_kg": 0.5,
            "mass_kg": 0.5,
            "observation_status": "configured_applied",
            "source_refs": ["runtime_readback.payload_sdf_mass"],
        },
        "landing_zone": {
            "safe": True,
            "source_refs": ["runtime_readback.landing_zone"],
        },
        "recovery": {
            "request_observed": True,
            "action": "safety_hold",
            "command_ack_observed": True,
            "assist_status": "safety_hold_observed",
            "resume_status": "held_awaiting_operator_recovery_approval",
            "source_refs": ["runtime_readback.safety_hold"],
            "performance_observation": {
                "action": "avoid_obstacle",
                "sample_count": 5,
                "duration_seconds": 4.386,
                "horizontal_distance_m": 13.237,
                "observed_horizontal_speed_mps": 3.017663,
                "source_refs": [
                    "runtime_readback.same_task_offboard_calibration"
                ],
            },
        },
    }


def _candidate() -> dict[str, Any]:
    return {
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": {
            "target_x_m": 60.0,
            "target_y_m": 120.0,
            "target_altitude_m": 45.0,
            "source_obstacle_name": "route_obstacle",
        },
        "intent_constraints": {"maximum_duration_s": 120.0},
        "source_refs": ["candidate_compiler.local_avoidance"],
        "recovery_path": {
            "frame_id": "local_ned_xy_altitude_up",
            "waypoints": [{"x_m": 60.0, "y_m": 120.0, "z_m": 45.0}],
            "source_refs": ["planner.bounded_avoidance_path"],
        },
    }


def _artifact(ref: str, **values: Any) -> dict[str, Any]:
    return {"artifact_ref": ref, **values}


def _positive_authority_chain() -> dict[str, Any]:
    return {
        "proposal": _artifact(
            "proposal:positive-avoidance",
            status="created",
            origin="hosted_llm_judgment",
            llm_judgment_observed=True,
            approval_created=False,
            dispatch_authority_created=False,
            physical_execution_invoked=False,
        ),
        "human_approval": _artifact(
            "approval:positive-avoidance",
            status="approved",
            human_approval_performed=True,
        ),
        "dispatch_revalidation": _artifact(
            "revalidation:positive-avoidance",
            status="valid",
            latest_telemetry_selected=True,
            policy_digest_matched=True,
            feasibility_status="verified_feasible",
        ),
        "dispatch_authority": _artifact(
            "dispatch:positive-avoidance",
            created=True,
            action="avoid_obstacle",
        ),
        "runner_ack": _artifact(
            "runner_ack:positive-avoidance",
            observed=True,
            accepted=True,
            ack_is_execution_effect=False,
        ),
        "observed_effect": _artifact(
            "effect:positive-avoidance",
            target_reached=True,
            resume_status="resumed_auto_mission",
        ),
        "completion": _artifact(
            "completion:positive-avoidance",
            landed=True,
            disarmed=True,
            mission_completion_claimed=True,
            delivery_completion_claimed=False,
            physical_execution_invoked=False,
        ),
    }


def _refusal_authority_chain(case_id: str) -> dict[str, Any]:
    return {
        "proposal": _artifact(
            f"proposal:{case_id}",
            status="rejected_before_proposal",
            llm_judgment_observed=True,
            approval_created=False,
            dispatch_authority_created=False,
            physical_execution_invoked=False,
        ),
        "human_approval": _artifact(
            f"approval:{case_id}",
            status="not_created",
            human_approval_performed=False,
        ),
        "dispatch_revalidation": _artifact(
            f"revalidation:{case_id}",
            status="not_authorized",
        ),
        "dispatch_authority": _artifact(
            f"dispatch:{case_id}",
            created=False,
        ),
        "runner_ack": _artifact(
            f"runner_ack:{case_id}",
            observed=False,
            ack_is_execution_effect=False,
        ),
        "observed_effect": _artifact(
            f"effect:{case_id}",
            target_reached=False,
            resume_status="not_attempted",
        ),
        "completion": _artifact(
            f"completion:{case_id}",
            landed=False,
            disarmed=False,
            mission_completion_claimed=False,
            delivery_completion_claimed=False,
            physical_execution_invoked=False,
        ),
    }


def _case(
    *,
    case_id: str,
    scenario_class: str,
    telemetry: dict[str, Any],
    policy_for_state: dict[str, Any],
    policy_for_replay: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    prior_cursor: dict[str, Any] | None = None,
    expected_policy_sha256: str = "",
    extracted_observations: list[str],
    source_runtime_evidence_available: bool,
) -> dict[str, Any]:
    replay_policy = policy_for_replay or policy_for_state
    hazard_state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry,
        recovery_policy=policy_for_state,
        observed_at=OBSERVED_AT,
        prior_telemetry_cursor=prior_cursor,
        expected_policy_sha256=expected_policy_sha256,
    )
    selected_candidate = candidate or _candidate()
    evaluation = verify_runtime_recovery_action_feasibility(
        candidate=selected_candidate,
        hazard_state=hazard_state,
        recovery_policy=replay_policy,
    )
    payload = {
        "schema_version": CORPUS_CASE_SCHEMA_VERSION,
        "case_id": case_id,
        "scenario_class": scenario_class,
        "source_evidence_refs": SOURCE_RUNTIME_EVIDENCE_REFS,
        "truth_boundary": {
            "artifact_truth": {
                "case_is_replay_fixture": True,
                "source_values_are_anonymized_semantic_extracts": True,
                "extracted_observations": extracted_observations,
            },
            "runtime_truth": {
                "source_runtime_evidence_available": (
                    source_runtime_evidence_available
                ),
                "source_runtime_evidence_refs": (
                    SOURCE_RUNTIME_EVIDENCE_REFS
                    if source_runtime_evidence_available
                    else []
                ),
                "source_contract_evidence_available": True,
                "source_contract_evidence_refs": [
                    "contract_evidence:action_feasibility_fail_closed",
                    "gateway_smoke:action_feasibility_fail_closed",
                ],
                "runtime_invoked_by_this_replay": False,
                "llm_invoked_by_this_replay": False,
                "simulator_invoked_by_this_replay": False,
            },
        },
        "recovery_policy": replay_policy,
        "hazard_state": hazard_state,
        "candidate": selected_candidate,
        "expected": {
            "feasibility_status": evaluation["feasibility_status"],
            "blocking_reasons": evaluation["blocking_reasons"],
            "unverified_reasons": evaluation["unverified_reasons"],
            "required_assumptions": evaluation["assumptions"],
        },
        "authority_chain": (
            _positive_authority_chain()
            if scenario_class == "positive"
            else _refusal_authority_chain(case_id)
        ),
        "replay_boundary": {
            "read_only": True,
            "network_required": False,
            "llm_required": False,
            "px4_required": False,
            "gazebo_required": False,
            "approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "completion_claimed": False,
            "progress_counted": False,
        },
    }
    return seal_action_feasibility_corpus_case(payload)


def _cases() -> list[dict[str, Any]]:
    base_policy = _policy()
    base_telemetry = _telemetry()
    positive_observations = [
        "thermal_parameter_set_get_agreement",
        "payload_requested_and_sdf_mass_equal_0.5kg",
        "same_task_offboard_calibration_5_samples",
        "source_backed_obstacle_geometry",
        "verified_feasible_before_and_at_dispatch",
        "explicit_human_approval",
        "runner_ack_then_target_reached",
        "auto_resumed_then_landed_and_disarmed",
    ]
    cases = [
        _case(
            case_id="px4-positive-verified-avoidance",
            scenario_class="positive",
            telemetry=copy.deepcopy(base_telemetry),
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=positive_observations,
            source_runtime_evidence_available=True,
        )
    ]

    missing_geometry = copy.deepcopy(base_telemetry)
    missing_geometry["obstacle"]["obstacle_manifest"]["obstacles"] = []
    cases.append(
        _case(
            case_id="px4-refusal-missing-obstacle-geometry",
            scenario_class="refusal",
            telemetry=missing_geometry,
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=[
                "missing_geometry_remains_unverified",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=False,
        )
    )

    missing_thermal = copy.deepcopy(base_telemetry)
    missing_thermal["temperature"] = {
        "temperature_c": 38.0,
        "model": {
            "model_id": "px4_sitl_temperature_derating.v1",
            "model_version": "1.0.0",
            "source_refs": ["runtime_readback.route_temperature"],
            "uncertainty_percent": 5.0,
        },
        "source_refs": ["runtime_readback.route_temperature"],
    }
    cases.append(
        _case(
            case_id="px4-refusal-missing-thermal-readback",
            scenario_class="refusal",
            telemetry=missing_thermal,
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=[
                "temperature_without_parameter_readback_remains_unverified",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=False,
        )
    )

    missing_performance = copy.deepcopy(base_telemetry)
    missing_performance["recovery"].pop("performance_observation")
    cases.append(
        _case(
            case_id="px4-refusal-missing-performance-envelope",
            scenario_class="refusal",
            telemetry=missing_performance,
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=[
                "missing_same_task_offboard_performance_remains_unverified",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=True,
        )
    )

    stale = copy.deepcopy(base_telemetry)
    stale["telemetry"]["stale"] = True
    cases.append(
        _case(
            case_id="px4-refusal-stale-telemetry",
            scenario_class="refusal",
            telemetry=stale,
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=[
                "stale_dispatch_telemetry_fails_closed",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=True,
        )
    )

    cases.append(
        _case(
            case_id="px4-refusal-cursor-regression",
            scenario_class="refusal",
            telemetry=copy.deepcopy(base_telemetry),
            policy_for_state=copy.deepcopy(base_policy),
            prior_cursor={
                "cursor_status": "complete",
                "sample_index": 141,
                "elapsed_seconds": 164.0,
            },
            extracted_observations=[
                "telemetry_cursor_regression_fails_closed",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=False,
        )
    )

    drifted_policy = copy.deepcopy(base_policy)
    drifted_policy["battery_return_threshold_percent"] = 30.0
    cases.append(
        _case(
            case_id="px4-refusal-policy-drift",
            scenario_class="refusal",
            telemetry=copy.deepcopy(base_telemetry),
            policy_for_state=copy.deepcopy(base_policy),
            policy_for_replay=drifted_policy,
            extracted_observations=[
                "active_policy_digest_drift_fails_closed",
                "new_proposal_and_approval_required",
            ],
            source_runtime_evidence_available=False,
        )
    )

    high_wind = copy.deepcopy(base_telemetry)
    high_wind["wind"]["speed_mps"] = 10.5
    high_wind["wind"]["gust_mps"] = 12.0
    cases.append(
        _case(
            case_id="px4-refusal-negative-wind-control-margin",
            scenario_class="refusal",
            telemetry=high_wind,
            policy_for_state=copy.deepcopy(base_policy),
            extracted_observations=[
                "negative_wind_control_margin_is_blocked",
                "no_dispatch_authority_created",
            ],
            source_runtime_evidence_available=True,
        )
    )
    return cases


def curate(output: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for case in _cases():
        relative_path = Path("cases") / f"{case['case_id']}.json"
        _write_json(output / relative_path, case)
        entries.append(
            {
                "case_id": case["case_id"],
                "path": relative_path.as_posix(),
                "sha256": _canonical_sha256(case),
                "scenario_class": case["scenario_class"],
                "expected_feasibility_status": case["expected"][
                    "feasibility_status"
                ],
            }
        )
    manifest = {
        "schema_version": CORPUS_MANIFEST_SCHEMA_VERSION,
        "corpus_id": "px4-action-feasibility-v1",
        "source_evidence_refs": SOURCE_RUNTIME_EVIDENCE_REFS,
        "case_count": len(entries),
        "cases": entries,
        "truth_boundary": {
            "artifact_truth": "deterministic_anonymized_replay_fixture",
            "runtime_truth": "referenced_source_evidence_not_reexecuted",
        },
        "publication_boundary": {
            "private_task_ids_included": False,
            "private_task_store_included": False,
            "credentials_included": False,
            "absolute_local_paths_included": False,
        },
    }
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    _write_json(output / "manifest.json", manifest)
    return verify_action_feasibility_corpus(output / "manifest.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    verdict = curate(args.output)
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["verification_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
