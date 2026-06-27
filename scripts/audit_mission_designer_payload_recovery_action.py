#!/usr/bin/env python3
"""Audit payload advisory -> operator-approved bounded recovery action.

This consumes a source-bound Form 2b payload feasibility advisory and records
whether a bounded operator-approved payload recovery action was dispatched and
observed. It does not add verifier, gate, task-status authority, hardware
execution, physical execution, or delivery-completion authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


SCHEMA_VERSION = "mission_designer_payload_recovery_action_audit.v1"
PAYLOAD_ADVISORY_SCHEMA_VERSION = "payload_feasibility_advisory.v1"
PAYLOAD_ADVISORY_REF = "payload_feasibility_advisory:mission_designer_payload_mass"
PAYLOAD_RECOVERY_ACTION_REF = "payload_recovery_action:mission_designer_payload_mass"
DEFAULT_PAYLOAD_MASS_KG = 1.25
DEFAULT_RECOVERY_ACTION = "land"
UNSAFE_AUTHORITY_KEYS = (
    "auto_gate",
    "task_status_mutated",
    "gate_status_mutated",
    "dropoff_verified",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "approval_free_dispatch_allowed",
    "approval_free_recovery_dispatch_allowed",
    "actuator_execution_performed",
    "physical_actuator_execution_allowed",
    "real_world_authority_granted",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _nested_true_keys(payload: Any, keys: set[str]) -> list[str]:
    observed: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and value is True:
                observed.append(key)
            observed.extend(_nested_true_keys(value, keys))
    elif isinstance(payload, list):
        for value in payload:
            observed.extend(_nested_true_keys(value, keys))
    return observed


def _summary_path(run_dir: Path) -> Path:
    path = run_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found under {run_dir}")
    return path


def _load_advisory(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    advisory = payload.get("payload_feasibility_advisory") if isinstance(payload, dict) else None
    if isinstance(advisory, dict):
        return advisory
    return payload


def _advisory_source_bound(advisory: dict[str, Any]) -> bool:
    refs = advisory.get("advisory_source_refs") or {}
    same_session = advisory.get("same_session_evidence") or {}
    return (
        advisory.get("schema_version") == PAYLOAD_ADVISORY_SCHEMA_VERSION
        and advisory.get("advisory_ref") == PAYLOAD_ADVISORY_REF
        and advisory.get("causal_form") == "Form 2b"
        and advisory.get("form2_subtype") == "Form 2b"
        and advisory.get("trigger_level") == "level_2_inferred"
        and advisory.get("mission_response_kind") == "advisory"
        and advisory.get("advisory_status") == "operator_review_required"
        and advisory.get("operator_review_required") is True
        and advisory.get("automatic_dispatch_suppressed") is True
        and advisory.get("eligible_for_direct_trigger") is False
        and advisory.get("eligible_for_advisory_only") is True
        and advisory.get("advisory_consumed_by_ref") is None
        and refs.get("climb_delay_audit_ref") == "drone_behavior_delta_under_payload_mass:mission_designer_payload_mass"
        and same_session.get("audit_artifact_id") == refs.get("climb_delay_audit_ref")
    )


def _payload_application_source_bound(summary: dict[str, Any], *, expected_payload_kg: float) -> bool:
    application = summary.get("payload_simulator_condition_application") or {}
    profile = summary.get("vehicle_condition_profile") or {}
    evidence = summary.get("observed_vehicle_condition_evidence") or {}
    observed = evidence.get("observed") or {}
    try:
        applied = application.get("applied") or {}
        requested = profile.get("requested") or {}
        applied_mass = float(applied.get("applied_mass_kg", applied.get("payload_mass_kg")))
        requested_mass = float(
            applied.get(
                "requested_mass_kg",
                requested.get("payload_mass_kg", observed.get("requested_payload_mass_kg")),
            )
        )
    except (TypeError, ValueError):
        return False
    return (
        application.get("schema_version") == "simulator_condition_application.v1"
        and application.get("application_id") == "simulator_condition_application:mission_designer_payload_mass"
        and application.get("condition_kind") == "payload_mass"
        and application.get("application_status") == "applied"
        and abs(applied_mass - expected_payload_kg) <= 1e-6
        and abs(requested_mass - expected_payload_kg) <= 1e-6
        and evidence.get("schema_version") == "observed_vehicle_condition_evidence.v1"
        and evidence.get("application_ref") == application.get("application_id")
        and observed.get("payload_mass_materialized") is True
        and observed.get("world_sdf_hash_match") is True
    )


def _payload_recovery_action_observed(
    summary: dict[str, Any],
    *,
    advisory_ref: str,
    expected_action: str,
) -> bool:
    action = summary.get("payload_recovery_action_artifact") or {}
    try:
        pose_z_m = float(summary.get("payload_recovery_pose_z_m"))
    except (TypeError, ValueError):
        pose_z_m = 999.0
    return (
        action.get("schema_version") == "payload_recovery_action.v1"
        and action.get("action_ref") == PAYLOAD_RECOVERY_ACTION_REF
        and action.get("causal_form") == "Form 2a"
        and action.get("form2_subtype") == "Form 2a"
        and action.get("trigger_level") == "level_2_inferred"
        and action.get("mission_response_kind") == "action"
        and action.get("payload_feasibility_advisory_ref") == advisory_ref
        and action.get("advisory_ref") == advisory_ref
        and action.get("advisory_consumed_by_ref") == PAYLOAD_RECOVERY_ACTION_REF
        and action.get("operator_approval_required") is True
        and action.get("operator_approval_performed") is True
        and str(action.get("approval_ref", "")).startswith("px4_gazebo_emergency_command_approval:")
        and str(action.get("dispatch_ref", "")).startswith("px4_gazebo_emergency_command_dispatch_result:")
        and action.get("bounded_action_kind") == expected_action
        and action.get("dispatch_status") in ("accepted", "timeout")
        and action.get("recovery_state_observed") is True
        and action.get("recovery_completed") is True
        and action.get("automatic_dispatch_suppressed") is False
        and action.get("approval_free_recovery_dispatch_allowed") is False
        and action.get("auto_gate") is False
        and action.get("task_status_mutated") is False
        and action.get("gate_status_mutated") is False
        and action.get("dropoff_verified") is False
        and action.get("delivery_completion_claimed") is False
        and action.get("hardware_target_allowed") is False
        and action.get("physical_execution_invoked") is False
        and summary.get("payload_feasibility_advisory_ref") == advisory_ref
        and summary.get("payload_advisory_consumed_by_ref") == PAYLOAD_RECOVERY_ACTION_REF
        and summary.get("payload_recovery_dispatch_status") in ("accepted", "timeout")
        and summary.get("payload_recovery_state_observed") is True
        and summary.get("payload_recovery_completed") is True
        and pose_z_m <= 0.15
    )


def _summarize_payload_recovery_action(
    *,
    advisory: dict[str, Any],
    run_dir: Path,
    expected_payload_kg: float,
    expected_action: str,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    advisory_ref = str(advisory.get("advisory_ref") or "")
    checks = {
        "advisory_source_bound": _advisory_source_bound(advisory),
        "horizontal_route_smoke_observed": summary.get("actual_px4_gazebo_horizontal_smoke_observed") is True,
        "payload_application_source_bound": _payload_application_source_bound(summary, expected_payload_kg=expected_payload_kg),
        "operator_approved_payload_recovery_action_observed": _payload_recovery_action_observed(summary, advisory_ref=advisory_ref, expected_action=expected_action),
        "task_completed_by_recovery_not_delivery": summary.get("task_status") == "completed" and str(summary.get("final_status", "")).startswith("payload_advisory_recovered_"),
        "dropoff_not_claimed": summary.get("dropoff_region_reached") is False and summary.get("dropoff_verified") is False and summary.get("delivery_completion_claimed") is False,
        "top_level_hardware_physical_false": summary.get("hardware_target_allowed") is False and summary.get("physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
    }
    missing = [name for name, passed in checks.items() if not passed]
    observed = not missing
    action = summary.get("payload_recovery_action_artifact") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": "mission_designer_payload_recovery_action_audit:mission_designer_payload_mass",
        "condition_kind": "source_bound_payload_recovery_action",
        "audit_status": "payload_recovery_action_observed" if observed else "unsupported",
        "closed_loop_observed": observed,
        "form2a_action_supported": observed,
        "artifact_dir": str(run_dir),
        "requested": {
            "payload_feasibility_advisory_ref": advisory_ref,
            "payload_mass_kg": expected_payload_kg,
            "operator_approved_recovery_action": expected_action,
            "recovery_action_is_not_delivery_completion": True,
        },
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed" for name in missing if name != "unsafe_authority_flags_absent"
        ] + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "observed": {
            "task_status": summary.get("task_status"),
            "final_status": summary.get("final_status"),
            "dropoff_region_reached": summary.get("dropoff_region_reached"),
            "payload_application_status": (summary.get("payload_simulator_condition_application") or {}).get("application_status"),
            "payload_recovery_action_ref": summary.get("payload_recovery_action_ref"),
            "payload_feasibility_advisory_ref": summary.get("payload_feasibility_advisory_ref"),
            "advisory_consumed_by_ref": summary.get("payload_advisory_consumed_by_ref"),
            "form2_subtype": action.get("form2_subtype"),
            "trigger_level": action.get("trigger_level"),
            "mission_response_kind": action.get("mission_response_kind"),
            "approval_ref": action.get("approval_ref"),
            "dispatch_ref": action.get("dispatch_ref"),
            "bounded_action_kind": action.get("bounded_action_kind"),
            "dispatch_status": action.get("dispatch_status"),
            "command_ack_observed": action.get("command_ack_observed"),
            "recovery_state_observed": action.get("recovery_state_observed"),
            "recovery_completed": action.get("recovery_completed"),
            "recovery_pose_z_m": action.get("recovery_pose_z_m"),
            "automatic_dispatch_suppressed": action.get("automatic_dispatch_suppressed"),
            "approval_free_recovery_dispatch_allowed": action.get("approval_free_recovery_dispatch_allowed"),
            "dropoff_verified": action.get("dropoff_verified"),
            "delivery_completion_claimed": action.get("delivery_completion_claimed"),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": {
            "payload_feasibility_advisory": advisory_ref,
            "payload_recovery_action": PAYLOAD_RECOVERY_ACTION_REF,
            "approval": action.get("approval_ref"),
            "dispatch": action.get("dispatch_ref"),
        },
        "adds_verifier": False,
        "adds_candidate": False,
        "adds_approval_chain": False,
        "adds_gate": False,
        "uses_existing_operator_approved_recovery": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_payload_recovery_smoke(
    *,
    advisory_ref: str,
    payload_mass_kg: float,
    recovery_action: str,
    artifact_root: Path,
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": str(payload_mass_kg),
        }
    )
    for key in (
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC",
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
            "--payload-advisory-recovery-action",
            recovery_action,
            "--payload-feasibility-advisory-ref",
            advisory_ref,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=360,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route payload advisory recovery smoke failed: "
            f"rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "horizontal route smoke did not emit JSON summary: "
            f"{result.stdout[-2000:]}"
        ) from exc
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit payload advisory -> bounded recovery action behavior."
    )
    parser.add_argument("--advisory-artifact", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--payload-mass-kg", type=float, default=DEFAULT_PAYLOAD_MASS_KG)
    parser.add_argument(
        "--recovery-action",
        choices=("land", "rtl", "hold"),
        default=DEFAULT_RECOVERY_ACTION,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    advisory = _load_advisory(args.advisory_artifact)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"payload_recovery_action_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_dir = args.run_dir or _run_payload_recovery_smoke(
        advisory_ref=str(advisory.get("advisory_ref") or ""),
        payload_mass_kg=args.payload_mass_kg,
        recovery_action=args.recovery_action,
        artifact_root=audit_dir / "runs" / "payload_recovery_action",
    )
    artifact = _summarize_payload_recovery_action(
        advisory=advisory,
        run_dir=run_dir,
        expected_payload_kg=args.payload_mass_kg,
        expected_action=args.recovery_action,
    )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = "existing_run" if args.run_dir else "executed_run"
    output_path = audit_dir / "mission_designer_payload_recovery_action.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
