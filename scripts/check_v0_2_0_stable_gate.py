#!/usr/bin/env python3
"""Fail-closed, version-aware release gate for MissionOS v0.2.0.

The gate runs GPU-free fixtures and checks publication-safe representative
evidence against current semantic material digests. It does not invoke an LLM,
NVIDIA GPU, live simulator, approval, dispatch, or physical execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.runtime.groot_policy_client import build_groot_sim_freshness_policy
from src.runtime.libero_panda_official_runner_instrumentation import (
    LIBERO_PANDA_INSTRUMENTATION_MATERIAL_SHA256,
    LIBERO_PANDA_INSTRUMENTATION_VERSION,
)
from src.runtime.libero_panda_predicate_package import (
    LIBERO_PANDA_PREDICATE_PACKAGE_SHA256,
    LIBERO_PANDA_PREDICATE_PACKAGE_VERSION,
)
from src.runtime.nav2_action_feasibility_corpus import (
    verify_nav2_corpus_through_core,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256,
    NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
)
from src.runtime.parent_mission_coordinator import (
    PARENT_MISSION_COORDINATOR_MATERIAL_SHA256,
    PARENT_MISSION_COORDINATOR_VERSION,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
    PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
)
from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    verify_action_feasibility_corpus_through_core,
)

from scripts.check_v0_1_0_stable_gate import _publication_reasons


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT
    / "docs"
    / "agents"
    / "evidence"
    / "v0.2.0-live-evidence-manifest.json"
)
READINESS_MATRIX_PATH = (
    ROOT
    / "docs"
    / "agents"
    / "evidence"
    / "20260802-v0.2.0-stable-readiness-matrix.json"
)
CORPUS_ROOT = ROOT / "tests" / "golden" / "action_feasibility"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RAW_TASK_ID_PATTERN = re.compile(r"\btask_[0-9a-f]{8,}\b", re.IGNORECASE)
_VAR_FOLDERS_PREFIX = "/var/" + "folders/"
PERSONAL_PATH_PATTERN = re.compile(
    rf"(?:/Users/[^/]+/|{re.escape(_VAR_FOLDERS_PREFIX)}|/home/[^/]+/)"
)
CREDENTIAL_PATTERN = re.compile(
    r"\b(?:sk-[0-9a-f]{24,}|gh[opusr]_[A-Za-z0-9]{20,})\b",
    re.IGNORECASE,
)
REPRESENTATIVE_SCHEMA_VERSION = "missionos_public_representative_evidence.v1"
MANIFEST_SCHEMA_VERSION = "missionos_versioned_live_evidence_manifest.v2"
PUBLICATION_FALSE_FIELDS = {
    "raw_task_id_included",
    "local_path_included",
    "credential_included",
    "raw_log_included",
    "raw_action_included",
    "raw_observation_included",
    "vendor_source_vendored",
    "checkpoint_vendored",
}

FixtureCheck = Callable[[], bool]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_components() -> dict[str, dict[str, Any]]:
    freshness = build_groot_sim_freshness_policy()
    return {
        "px4_gazebo_delivery_predicate_package": {
            "version": PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
            "material_sha256": PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
            "required_live_evidence": True,
        },
        "nav2_turtlebot3_predicate_package": {
            "version": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
            "material_sha256": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256,
            "required_live_evidence": True,
        },
        "libero_panda_predicate_package": {
            "version": LIBERO_PANDA_PREDICATE_PACKAGE_VERSION,
            "material_sha256": LIBERO_PANDA_PREDICATE_PACKAGE_SHA256,
            "required_live_evidence": True,
        },
        "parent_mission_coordinator": {
            "version": PARENT_MISSION_COORDINATOR_VERSION,
            "material_sha256": PARENT_MISSION_COORDINATOR_MATERIAL_SHA256,
            "required_live_evidence": True,
        },
        "libero_panda_official_runner_instrumentation": {
            "version": LIBERO_PANDA_INSTRUMENTATION_VERSION,
            "material_sha256": LIBERO_PANDA_INSTRUMENTATION_MATERIAL_SHA256,
            "required_live_evidence": True,
        },
        "groot_sim_freshness_policy": {
            "version": freshness.policy_version,
            "material_sha256": freshness.policy_sha256,
            "required_live_evidence": False,
        },
    }


def _run_command(path: str) -> bool:
    environment = dict(os.environ)
    pythonpath = [
        str(ROOT),
        str(ROOT / "packages" / "missionos-core" / "src"),
        str(ROOT / "packages" / "missionos-cli" / "src"),
    ]
    existing = environment.get("PYTHONPATH")
    if existing:
        pythonpath.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    result = subprocess.run(
        [sys.executable, path],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _px4_conformance() -> bool:
    verdict = verify_action_feasibility_corpus_through_core(
        CORPUS_ROOT / "px4_v1" / "manifest.json"
    )
    return verdict.get("status") == "verified"


def _nav2_conformance() -> bool:
    verdict = verify_nav2_corpus_through_core(
        CORPUS_ROOT / "nav2_v1" / "manifest.json"
    )
    return verdict.get("status") == "verified"


def _fixture_checks() -> dict[str, FixtureCheck]:
    return {
        "px4_core_conformance": _px4_conformance,
        "nav2_core_conformance": _nav2_conformance,
        "parent_three_stage_fixture": lambda: _run_command(
            "scripts/smoke_parent_mission_px4_nav2_libero_fixture.py"
        ),
        "libero_predicate_fixture": lambda: _run_command(
            "scripts/smoke_libero_panda_predicate_package.py"
        ),
        "libero_instrumentation_fixture": lambda: _run_command(
            "scripts/smoke_libero_panda_official_runner_instrumentation.py"
        ),
    }


def _component_bindings(
    evidence: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    raw = evidence.get("component_bindings")
    if not isinstance(raw, list):
        return bindings
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        component_id = str(item.get("component_id") or "")
        if component_id and component_id not in bindings:
            bindings[component_id] = dict(item)
    return bindings


def _publication_boundary_reasons(
    evidence_id: str,
    evidence: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    boundary = evidence.get("publication_boundary")
    boundary = dict(boundary) if isinstance(boundary, Mapping) else {}
    for field in sorted(PUBLICATION_FALSE_FIELDS):
        if boundary.get(field) is not False:
            reasons.append(f"representative_publication_boundary_invalid:{evidence_id}:{field}")
    serialized = json.dumps(evidence, ensure_ascii=True, sort_keys=True)
    if RAW_TASK_ID_PATTERN.search(serialized):
        reasons.append(f"representative_contains_raw_task_id:{evidence_id}")
    if PERSONAL_PATH_PATTERN.search(serialized):
        reasons.append(f"representative_contains_personal_path:{evidence_id}")
    if CREDENTIAL_PATTERN.search(serialized):
        reasons.append(f"representative_contains_credential_shape:{evidence_id}")
    return reasons


def _representative_reasons(
    *,
    record: Mapping[str, Any],
    current_components: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any] | None]:
    reasons: list[str] = []
    evidence_id = str(record.get("evidence_id") or "")
    relative = str(record.get("path") or "")
    path = ROOT / relative
    if not evidence_id:
        return ["manifest_evidence_id_missing"], None
    if not path.is_file():
        return [f"representative_missing:{evidence_id}"], None
    expected_digest = str(record.get("sha256") or "")
    if not SHA256_PATTERN.fullmatch(expected_digest):
        reasons.append(f"representative_digest_invalid:{evidence_id}")
    elif _sha256_path(path) != expected_digest:
        reasons.append(f"representative_digest_mismatch:{evidence_id}")
    try:
        evidence = _load(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return [*reasons, f"representative_schema_unreadable:{evidence_id}"], None
    if evidence.get("schema_version") != REPRESENTATIVE_SCHEMA_VERSION:
        reasons.append(f"representative_schema_invalid:{evidence_id}")
    if evidence.get("evidence_id") != evidence_id:
        reasons.append(f"representative_id_mismatch:{evidence_id}")
    if evidence.get("version_status") != record.get("version_status"):
        reasons.append(f"representative_version_status_mismatch:{evidence_id}")
    current_record = record.get("version_status") == "current"
    satisfies_current = record.get("satisfies_current_live_evidence_requirement")
    if current_record and satisfies_current is not True:
        reasons.append(f"current_representative_not_eligible:{evidence_id}")
    if not current_record and satisfies_current is not False:
        reasons.append(f"historical_representative_promoted:{evidence_id}")
    reasons.extend(_publication_boundary_reasons(evidence_id, evidence))

    source = evidence.get("source_record")
    source = dict(source) if isinstance(source, Mapping) else {}
    source_digest = str(source.get("sha256") or "")
    if not SHA256_PATTERN.fullmatch(source_digest):
        reasons.append(f"representative_source_digest_invalid:{evidence_id}")
    if source.get("source_record_included") is not False:
        reasons.append(f"representative_source_boundary_invalid:{evidence_id}")
    if not str(source.get("internal_evidence_id") or ""):
        reasons.append(f"representative_source_id_missing:{evidence_id}")
    if "path" in source:
        reasons.append(f"representative_source_path_published:{evidence_id}")

    bindings = _component_bindings(evidence)
    if not bindings:
        reasons.append(f"representative_component_bindings_missing:{evidence_id}")
    if current_record:
        for component_id, binding in bindings.items():
            current = current_components.get(component_id)
            if not isinstance(current, Mapping):
                reasons.append(f"representative_component_unknown:{evidence_id}:{component_id}")
                continue
            if binding.get("version") != current.get("version"):
                reasons.append(f"representative_component_version_mismatch:{evidence_id}:{component_id}")
            if binding.get("material_sha256") != current.get("material_sha256"):
                reasons.append(f"representative_component_digest_mismatch:{evidence_id}:{component_id}")
    else:
        supersession = evidence.get("supersession")
        supersession = dict(supersession) if isinstance(supersession, Mapping) else {}
        if supersession.get("historical_record_rewritten") is not False:
            reasons.append(f"historical_record_rewritten:{evidence_id}")
        if supersession.get("satisfies_current_live_evidence_requirement") is not False:
            reasons.append(f"historical_record_promoted:{evidence_id}")
        freshness = current_components["groot_sim_freshness_policy"]
        if supersession.get("superseded_by_version") != freshness.get("version"):
            reasons.append(f"historical_supersession_version_mismatch:{evidence_id}")
        if supersession.get("superseded_by_material_sha256") != freshness.get("material_sha256"):
            reasons.append(f"historical_supersession_digest_mismatch:{evidence_id}")
    return reasons, evidence


def _readiness_matrix_reasons(
    *,
    path: Path,
    manifest_path: Path,
    computed_matrix: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    if not path.is_file():
        return ["v0_2_0_readiness_matrix_missing"]
    try:
        artifact = _load(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ["v0_2_0_readiness_matrix_unreadable"]
    reasons: list[str] = []
    if artifact.get("schema_version") != (
        "missionos_v0_2_0_stable_readiness_matrix.v1"
    ):
        reasons.append("v0_2_0_readiness_matrix_schema_invalid")
    if artifact.get("release") != "v0.2.0":
        reasons.append("v0_2_0_readiness_matrix_release_invalid")
    if artifact.get("manifest_sha256") != _sha256_path(manifest_path):
        reasons.append("v0_2_0_readiness_matrix_manifest_digest_mismatch")
    recorded = artifact.get("verification_matrix")
    recorded = dict(recorded) if isinstance(recorded, Mapping) else {}
    if set(recorded) != set(computed_matrix):
        reasons.append("v0_2_0_readiness_matrix_row_set_mismatch")
    for row_id, computed in computed_matrix.items():
        row = recorded.get(row_id)
        row = dict(row) if isinstance(row, Mapping) else {}
        for field in ("fixture", "live_evidence", "implementation_bound"):
            if row.get(field) != computed.get(field):
                reasons.append(
                    f"v0_2_0_readiness_matrix_{field}_mismatch:{row_id}"
                )
    boundary = artifact.get("claim_boundary")
    boundary = dict(boundary) if isinstance(boundary, Mapping) else {}
    for field in (
        "manual_live_evidence_reexecuted_by_ci",
        "nvidia_gpu_invoked_by_ci",
        "live_simulator_invoked_by_ci",
        "physical_execution_invoked",
        "single_health_score_created",
    ):
        if boundary.get(field) is not False:
            reasons.append(f"v0_2_0_readiness_matrix_claim_invalid:{field}")
    return reasons


def evaluate_v0_2_0_stable_gate(
    *,
    manifest_path: Path = MANIFEST_PATH,
    fixture_checks: Mapping[str, FixtureCheck] | None = None,
    readiness_matrix_path: Path | None = READINESS_MATRIX_PATH,
) -> dict[str, Any]:
    """Return independent release dimensions and typed blocking reasons."""

    reasons: list[str] = []
    if not manifest_path.is_file():
        return {
            "schema_version": "missionos_v0_2_0_stable_gate_result.v1",
            "release": "v0.2.0",
            "verification_matrix": {},
            "blocking_reasons": ["v0_2_0_evidence_manifest_missing"],
            "gpu_invoked": False,
            "live_simulator_invoked": False,
            "physical_execution_invoked": False,
        }
    manifest = _load(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        reasons.append("v0_2_0_evidence_manifest_schema_invalid")
    if manifest.get("release") != "v0.2.0":
        reasons.append("v0_2_0_evidence_manifest_release_invalid")

    current_components = _current_components()
    declared = manifest.get("current_components")
    declared = dict(declared) if isinstance(declared, Mapping) else {}
    if set(declared) != set(current_components):
        reasons.append("v0_2_0_current_component_set_mismatch")
    for component_id, current in current_components.items():
        item = declared.get(component_id)
        item = dict(item) if isinstance(item, Mapping) else {}
        for field in ("version", "material_sha256", "required_live_evidence"):
            if item.get(field) != current.get(field):
                reasons.append(f"current_component_{field}_mismatch:{component_id}")

    records_value = manifest.get("evidence_records")
    records_value = records_value if isinstance(records_value, list) else []
    records: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for raw_record in records_value:
        if not isinstance(raw_record, Mapping):
            reasons.append("manifest_evidence_record_invalid")
            continue
        record = dict(raw_record)
        evidence_id = str(record.get("evidence_id") or "")
        if evidence_id in records:
            reasons.append(f"manifest_evidence_id_duplicate:{evidence_id}")
            continue
        records[evidence_id] = record
        record_reasons, loaded = _representative_reasons(
            record=record,
            current_components=current_components,
        )
        reasons.extend(record_reasons)
        if loaded is not None:
            evidence[evidence_id] = loaded

    checks = dict(fixture_checks or _fixture_checks())
    fixture_results: dict[str, bool] = {}
    matrix_value = manifest.get("verification_matrix")
    matrix_value = dict(matrix_value) if isinstance(matrix_value, Mapping) else {}
    matrix: dict[str, dict[str, Any]] = {}
    for row_id, raw_row in matrix_value.items():
        row = dict(raw_row) if isinstance(raw_row, Mapping) else {}
        required = row.get("required") is True
        check_ids = [str(value) for value in row.get("fixture_checks") or []]
        row_check_results: dict[str, bool] = {}
        for check_id in check_ids:
            if check_id not in fixture_results:
                check = checks.get(check_id)
                fixture_results[check_id] = bool(check and check())
            row_check_results[check_id] = fixture_results[check_id]
        fixture_status = (
            "not_applicable"
            if not check_ids
            else "pass"
            if all(row_check_results.values())
            else "fail"
        )

        evidence_ids = [
            str(value) for value in row.get("current_live_evidence_ids") or []
        ]
        current_evidence = [
            evidence[item]
            for item in evidence_ids
            if item in evidence
            and records[item].get("version_status") == "current"
            and records[item].get("satisfies_current_live_evidence_requirement") is True
        ]
        live_status = (
            "absent"
            if not evidence_ids
            else "present"
            if len(current_evidence) == len(evidence_ids)
            else "incompatible"
        )
        required_components = [
            str(value) for value in row.get("required_component_ids") or []
        ]
        observed_bindings: dict[str, dict[str, Any]] = {}
        for item in current_evidence:
            observed_bindings.update(_component_bindings(item))
        missing_components = [
            component_id
            for component_id in required_components
            if component_id not in observed_bindings
        ]
        unmatched_components = [
            component_id
            for component_id in required_components
            if component_id in observed_bindings
            and (
                observed_bindings[component_id].get("version")
                != current_components.get(component_id, {}).get("version")
                or observed_bindings[component_id].get("material_sha256")
                != current_components.get(component_id, {}).get("material_sha256")
            )
        ]
        audited_after_run = [
            component_id
            for component_id in required_components
            if component_id in observed_bindings
            and observed_bindings[component_id].get("binding_observed_at_runtime") is False
        ]
        implementation_status = (
            "not_applicable"
            if not required_components
            else "unmatched"
            if missing_components or unmatched_components
            else "matched_post_run_source_audit"
            if audited_after_run
            else "matched"
        )
        matrix[str(row_id)] = {
            "required": required,
            "fixture": fixture_status,
            "fixture_checks": row_check_results,
            "live_evidence": live_status,
            "evidence_ids": evidence_ids,
            "implementation_bound": implementation_status,
            "post_run_source_audit_component_ids": audited_after_run,
            "missing_component_ids": missing_components,
            "unmatched_component_ids": unmatched_components,
        }
        if required:
            if fixture_status != "pass":
                reasons.append(f"v0_2_0_fixture_not_passed:{row_id}")
            if live_status != "present":
                reasons.append(f"v0_2_0_live_evidence_not_present:{row_id}")
            if not implementation_status.startswith("matched"):
                reasons.append(f"v0_2_0_implementation_not_bound:{row_id}")

    required_components_with_evidence: set[str] = set()
    for evidence_id, item in evidence.items():
        record = records[evidence_id]
        if (
            record.get("version_status") == "current"
            and record.get("satisfies_current_live_evidence_requirement")
            is True
        ):
            required_components_with_evidence.update(
                _component_bindings(item)
            )
    for component_id, current in current_components.items():
        if current["required_live_evidence"] and component_id not in required_components_with_evidence:
            reasons.append(f"live_evidence_missing_for_current_version:{component_id}")

    if readiness_matrix_path is not None:
        reasons.extend(
            _readiness_matrix_reasons(
                path=readiness_matrix_path,
                manifest_path=manifest_path,
                computed_matrix=matrix,
            )
        )

    claim_boundary = manifest.get("claim_boundary")
    claim_boundary = dict(claim_boundary) if isinstance(claim_boundary, Mapping) else {}
    for field in (
        "ci_invokes_nvidia_gpu",
        "ci_invokes_live_simulator",
        "fixture_success_is_live_runtime_success",
        "evidence_compatibility_is_runtime_reexecution",
        "physical_execution_invoked",
    ):
        if claim_boundary.get(field) is not False:
            reasons.append(f"v0_2_0_claim_boundary_invalid:{field}")
    reasons.extend(_publication_reasons())
    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": "missionos_v0_2_0_stable_gate_result.v1",
        "release": "v0.2.0",
        "verification_matrix": matrix,
        "historical_evidence_ids": [
            evidence_id
            for evidence_id, record in records.items()
            if record.get("version_status") == "superseded"
        ],
        "blocking_reasons": reasons,
        "gpu_invoked": False,
        "live_simulator_invoked": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    result = evaluate_v0_2_0_stable_gate(manifest_path=args.manifest)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if not result["blocking_reasons"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
