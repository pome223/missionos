#!/usr/bin/env python3
"""Build a scoped Mission OS supervisor cohort from existing Form 3 audits.

This widens the observed supervisor scope from single-condition slices to a
multi-condition SITL supervisor cohort. It does not create full Gateway runtime
authority and does not count new Form 3 capability progress.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "mission_os_supervisor_scope_cohort.v1"
WIND_OBSTACLE_SUPERVISOR_SCOPE = "wind_obstacle_form3_sitl"
WIND_OBSTACLE_PAYLOAD_SUPERVISOR_SCOPE = "wind_obstacle_payload_form3_sitl"
TARGET_SUPERVISOR_SCOPE = WIND_OBSTACLE_PAYLOAD_SUPERVISOR_SCOPE
EXPECTED_SOURCE_SCOPES = {
    "wind": "wind_form3_sitl_only",
    "obstacle": "obstacle_form3_sitl_only",
    "payload": "payload_form3_sitl_only",
}
EXPECTED_SOURCE_SCHEMAS = {
    "wind": "mission_designer_wind_drift_form3_closed_loop_audit.v1",
    "obstacle": "mission_designer_obstacle_true_form3_closed_loop_audit.v1",
    "payload": "mission_designer_payload_supervisor_form3_closed_loop_audit.v1",
}
EXPECTED_LOOP_SCHEMAS = {
    "wind": "mission_os_recovery_runtime_bridge.v1",
    "obstacle": "mission_os_supervisor_recovery_loop.v1",
    "payload": "mission_os_supervisor_recovery_loop.v1",
}
NESTED_AUTHORITY_KEYS_REQUIRED_FALSE = {
    "ai_judgment_is_gate_verdict",
    "ai_judgment_created_dispatch_authority",
    "llm_gate_judge_used",
    "dispatch_authority_created",
    "created_dispatch_authority",
    "automatic_dispatch_allowed",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "physical_form1_claimed",
    "full_gateway_runtime_loop",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _runtime_loop(audit: dict[str, Any]) -> dict[str, Any]:
    loop = audit.get("mission_os_runtime_decision_loop")
    if isinstance(loop, dict):
        return loop
    loop = audit.get("mission_os_supervisor_recovery_loop")
    return loop if isinstance(loop, dict) else {}


def _supervisor_loop_observed(checks: dict[str, Any]) -> bool:
    return (
        checks.get("mission_os_supervisor_loop_observed_if_requested") is True
        or checks.get("mission_os_supervisor_loop_observed") is True
    )


def _conflicting_risks_absent(
    *,
    checks: dict[str, Any],
    loop: dict[str, Any],
) -> bool:
    if checks.get("mission_os_supervisor_conflicting_risks_absent") is True:
        return True
    risks = loop.get("conflicting_risks")
    return isinstance(risks, list) and not risks


def _dict_nonempty(value: Any) -> bool:
    return isinstance(value, dict) and bool(value)


def _list_nonempty(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def _source_ref_present(source_refs: dict[str, Any], key: str) -> bool:
    return isinstance(source_refs.get(key), str) and bool(source_refs.get(key))


def _parameter_source_refs_match(
    audit: dict[str, Any],
    *,
    parameter_names: set[str],
    expected_source_ref: str,
) -> bool:
    observations = audit.get("parameter_observations")
    if not isinstance(observations, list):
        return False
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        parameter = observation.get("parameter")
        if parameter in parameter_names:
            seen.add(str(parameter))
            if observation.get("source_ref") != expected_source_ref:
                return False
    return seen == parameter_names


def _source_bound_checks(
    audit: dict[str, Any],
    *,
    condition: str,
    checks: dict[str, Any],
) -> dict[str, bool]:
    source_refs = audit.get("source_refs")
    source_refs = source_refs if isinstance(source_refs, dict) else {}
    common = {
        "source_bound_true": audit.get("source_bound") is True,
        "source_refs_present": _dict_nonempty(source_refs),
    }
    if condition == "wind":
        wind_application_ref = str(source_refs.get("wind_application") or "")
        return {
            **common,
            "wind_application_ref_present": _source_ref_present(
                source_refs, "wind_application"
            ),
            "source_refs_observed": checks.get("source_refs_observed") is True,
            "wind_application_source_bound": checks.get(
                "wind_application_source_bound"
            )
            is True,
            "parameter_observations_present": _list_nonempty(
                audit.get("parameter_observations")
            ),
            "wind_parameter_source_refs_match": _parameter_source_refs_match(
                audit,
                parameter_names={"wind_speed_mps", "wind_direction_deg"},
                expected_source_ref=wind_application_ref,
            ),
        }
    if condition == "obstacle":
        obstacle_application_ref = str(source_refs.get("obstacle_application") or "")
        return {
            **common,
            "obstacle_application_ref_present": _source_ref_present(
                source_refs, "obstacle_application"
            ),
            "route_blocking_verification_ref_present": _source_ref_present(
                source_refs, "route_blocking_verification"
            ),
            "parameter_observations_present": _list_nonempty(
                audit.get("parameter_observations")
            ),
            "obstacle_parameter_source_refs_match": _parameter_source_refs_match(
                audit,
                parameter_names={
                    "obstacle_start_x_m",
                    "obstacle_start_y_m",
                    "obstacle_end_x_m",
                    "obstacle_end_y_m",
                },
                expected_source_ref=obstacle_application_ref,
            ),
        }
    if condition == "payload":
        return {
            **common,
            "payload_feasibility_advisory_ref_present": _source_ref_present(
                source_refs, "payload_feasibility_advisory"
            ),
            "cycle1_payload_recovery_action_ref_present": _source_ref_present(
                source_refs, "cycle1_payload_recovery_action"
            ),
            "advisory_source_bound": checks.get("advisory_source_bound") is True,
            "payload_application_source_bound": checks.get(
                "payload_application_source_bound"
            )
            is True,
        }
    return common


def _nested_authority_reasons(payload: Any, *, path: str = "loop") -> list[str]:
    reasons: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            current_path = f"{path}.{key}"
            if key in NESTED_AUTHORITY_KEYS_REQUIRED_FALSE and value is not False:
                reasons.append(f"nested_authority_{current_path}_not_false")
            if isinstance(value, (dict, list)):
                reasons.extend(_nested_authority_reasons(value, path=current_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            reasons.extend(_nested_authority_reasons(value, path=f"{path}[{index}]"))
    return reasons


def _audit_supported(
    audit: dict[str, Any],
    *,
    condition: str,
    expected_schema: str,
    expected_scope: str,
    expected_loop_schema: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    loop = _runtime_loop(audit)
    checks = audit.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    observed = audit.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    source_bound_checks = _source_bound_checks(
        audit,
        condition=condition,
        checks=checks,
    )

    required = {
        "expected_source_schema": audit.get("schema_version") == expected_schema,
        "audit_status_form3_observed": audit.get("audit_status") == "form3_observed",
        "causal_form_form3": audit.get("causal_form") == "Form 3",
        "form3_claim_supported": audit.get("form3_claim_supported") is True,
        "source_progress_counted": audit.get("progress_counted") is True,
        "supervisor_loop_check_observed": _supervisor_loop_observed(checks),
        "supervisor_conflicting_risks_absent": _conflicting_risks_absent(
            checks=checks,
            loop=loop,
        ),
        "decision_loop_driver_supervisor": loop.get("decision_loop_driver")
        == "mission_os_supervisor",
        "expected_loop_schema": loop.get("schema_version") == expected_loop_schema,
        "expected_source_supervisor_scope": loop.get("supervisor_scope")
        == expected_scope,
        "source_full_gateway_runtime_false": loop.get("full_gateway_runtime_loop")
        is False,
        "supervisor_loop_claim_supported": loop.get("supervisor_loop_claim_supported")
        is True,
        "source_delivery_completion_not_claimed": audit.get(
            "delivery_completion_claimed"
        )
        is False,
        "source_hardware_target_disallowed": audit.get("hardware_target_allowed")
        is False,
        "source_physical_execution_not_invoked": audit.get(
            "physical_execution_invoked"
        )
        is False,
        "source_unsafe_authority_flags_absent": not observed.get(
            "unsafe_authority_flags_observed"
        ),
        "source_dispatch_authority_not_created": audit.get(
            "dispatch_authority_created", False
        )
        is False,
        "source_physical_form1_not_claimed": audit.get(
            "physical_form1_claimed", False
        )
        is False,
    }
    for name, passed in source_bound_checks.items():
        required[f"source_bound_{name}"] = passed
    for name, passed in required.items():
        if not passed:
            reasons.append(f"{name}_not_observed")
    reasons.extend(_nested_authority_reasons(audit, path="audit"))
    return not reasons, reasons


def build_mission_os_supervisor_scope_cohort(
    *,
    wind_audit_path: Path,
    obstacle_audit_path: Path,
    payload_audit_path: Path | None = None,
) -> dict[str, Any]:
    wind_audit = _read_json(wind_audit_path)
    obstacle_audit = _read_json(obstacle_audit_path)
    payload_audit = _read_json(payload_audit_path) if payload_audit_path else None

    source_specs = {
        "wind": {
            "path": wind_audit_path,
            "audit": wind_audit,
            "expected_scope": EXPECTED_SOURCE_SCOPES["wind"],
            "expected_schema": EXPECTED_SOURCE_SCHEMAS["wind"],
            "expected_loop_schema": EXPECTED_LOOP_SCHEMAS["wind"],
        },
        "obstacle": {
            "path": obstacle_audit_path,
            "audit": obstacle_audit,
            "expected_scope": EXPECTED_SOURCE_SCOPES["obstacle"],
            "expected_schema": EXPECTED_SOURCE_SCHEMAS["obstacle"],
            "expected_loop_schema": EXPECTED_LOOP_SCHEMAS["obstacle"],
        },
    }
    if payload_audit_path is not None and payload_audit is not None:
        source_specs["payload"] = {
            "path": payload_audit_path,
            "audit": payload_audit,
            "expected_scope": EXPECTED_SOURCE_SCOPES["payload"],
            "expected_schema": EXPECTED_SOURCE_SCHEMAS["payload"],
            "expected_loop_schema": EXPECTED_LOOP_SCHEMAS["payload"],
        }

    target_supervisor_scope = (
        WIND_OBSTACLE_PAYLOAD_SUPERVISOR_SCOPE
        if "payload" in source_specs
        else WIND_OBSTACLE_SUPERVISOR_SCOPE
    )
    required_condition_count = 3 if "payload" in source_specs else 2
    source_entries: list[dict[str, Any]] = []
    condition_checks: dict[str, bool] = {}
    unsupported_reasons: list[str] = []
    for condition, spec in source_specs.items():
        audit = spec["audit"]
        loop = _runtime_loop(audit)
        supported, reasons = _audit_supported(
            audit,
            condition=condition,
            expected_schema=str(spec["expected_schema"]),
            expected_scope=str(spec["expected_scope"]),
            expected_loop_schema=str(spec["expected_loop_schema"]),
        )
        condition_checks[f"{condition}_supervisor_scope_observed"] = supported
        unsupported_reasons.extend(f"{condition}_{reason}" for reason in reasons)
        source_entries.append(
            {
                "condition": condition,
                "artifact_path": str(spec["path"]),
                "schema_version": audit.get("schema_version"),
                "audit_status": audit.get("audit_status"),
                "causal_form": audit.get("causal_form"),
                "loop_schema_version": loop.get("schema_version"),
                "source_supervisor_scope": loop.get("supervisor_scope"),
                "decision_loop_driver": loop.get("decision_loop_driver"),
                "full_gateway_runtime_loop": loop.get("full_gateway_runtime_loop"),
                "supervisor_loop_claim_supported": loop.get(
                    "supervisor_loop_claim_supported"
                ),
                "supported_for_scope_cohort": supported,
                "unsupported_reasons": reasons,
            }
        )

    scope_observed = all(condition_checks.values())
    checks = {
        **condition_checks,
        "condition_count_minimum_observed": len(source_entries)
        >= required_condition_count,
        "target_scope_is_not_full_gateway_runtime": True,
        "target_scope_has_no_dispatch_authority": True,
        "target_scope_has_no_physical_authority": True,
    }
    if not checks["condition_count_minimum_observed"]:
        unsupported_reasons.append("condition_count_minimum_observed_not_observed")

    payload_for_id = {
        "target_scope": target_supervisor_scope,
        "source_artifacts": [
            {
                "condition": entry["condition"],
                "artifact_path": entry["artifact_path"],
                "source_supervisor_scope": entry["source_supervisor_scope"],
            }
            for entry in source_entries
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "cohort_id": _stable_id("mission_os_supervisor_scope_cohort", payload_for_id),
        "supervisor_scope": target_supervisor_scope,
        "scope_status": "supervisor_scope_observed" if scope_observed else "unsupported",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "form3_capability_progress_counted": False,
        "decision_loop_driver": "mission_os_supervisor",
        "full_gateway_runtime_loop": False,
        "source_condition_count": len(source_entries),
        "accepted_condition_count": sum(
            1 for entry in source_entries if entry["supported_for_scope_cohort"]
        ),
        "required_condition_count": required_condition_count,
        "required_source_scopes": {
            condition: EXPECTED_SOURCE_SCOPES[condition]
            for condition in source_specs
        },
        "required_source_schemas": {
            condition: EXPECTED_SOURCE_SCHEMAS[condition]
            for condition in source_specs
        },
        "required_loop_schemas": {
            condition: EXPECTED_LOOP_SCHEMAS[condition] for condition in source_specs
        },
        "source_artifacts": source_entries,
        "checks": checks,
        "unsupported_reasons": unsupported_reasons,
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "physical_form1_claimed": False,
        },
        "scope_boundary_notes": [
            f"{target_supervisor_scope}_is_a_scoped_supervisor_cohort",
            "full_gateway_runtime_loop_remains_false",
            "source_form3_capability_is_reused_but_not_recounted",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wind-audit", type=Path, required=True)
    parser.add_argument("--obstacle-audit", type=Path, required=True)
    parser.add_argument("--payload-audit", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    stamp = _utc_stamp()
    audit_dir = args.output_dir / f"mission_os_supervisor_scope_cohort_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    artifact = build_mission_os_supervisor_scope_cohort(
        wind_audit_path=args.wind_audit,
        obstacle_audit_path=args.obstacle_audit,
        payload_audit_path=args.payload_audit,
    )
    artifact["audit_dir"] = str(audit_dir)
    output_path = audit_dir / "mission_os_supervisor_scope_cohort.json"
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
