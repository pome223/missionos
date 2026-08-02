from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.check_v0_2_0_stable_gate import (
    MANIFEST_PATH,
    _current_components,
    evaluate_v0_2_0_stable_gate,
)


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _passing_checks(manifest: dict) -> dict[str, object]:
    check_ids = {
        str(check_id)
        for row in manifest["verification_matrix"].values()
        for check_id in row["fixture_checks"]
    }
    return {check_id: lambda: True for check_id in check_ids}


def test_v0_2_0_gate_passes_real_gpu_free_fixtures() -> None:
    result = evaluate_v0_2_0_stable_gate()

    assert result["blocking_reasons"] == []
    assert result["gpu_invoked"] is False
    assert result["live_simulator_invoked"] is False
    assert result["physical_execution_invoked"] is False


def test_v0_2_0_gate_keeps_verification_dimensions_separate() -> None:
    result = evaluate_v0_2_0_stable_gate()
    matrix = result["verification_matrix"]

    assert matrix["px4_nav2"]["fixture"] == "pass"
    assert matrix["px4_nav2"]["live_evidence"] == "present"
    assert matrix["px4_nav2"]["implementation_bound"] == "matched"
    assert matrix["parent_three_stage"]["implementation_bound"] == (
        "matched_post_run_source_audit"
    )
    assert matrix["vla_groot_libero"]["implementation_bound"] == (
        "matched_post_run_source_audit"
    )
    assert matrix["physical"] == {
        "required": False,
        "fixture": "not_applicable",
        "fixture_checks": {},
        "live_evidence": "absent",
        "evidence_ids": [],
        "implementation_bound": "not_applicable",
        "post_run_source_audit_component_ids": [],
        "missing_component_ids": [],
        "unmatched_component_ids": [],
    }


def test_current_component_digest_drift_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["current_components"][
        "libero_panda_predicate_package"
    ]["material_sha256"] = "0" * 64

    result = evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=_passing_checks(manifest),
        readiness_matrix_path=None,
    )

    assert (
        "current_component_material_sha256_mismatch:"
        "libero_panda_predicate_package"
    ) in result["blocking_reasons"]


def test_representative_digest_mutation_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["evidence_records"][0]["sha256"] = "f" * 64

    result = evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=_passing_checks(manifest),
        readiness_matrix_path=None,
    )

    assert (
        "representative_digest_mismatch:"
        "v0.2.0-parent-three-stage-live-positive"
    ) in result["blocking_reasons"]


def test_historical_evidence_cannot_be_promoted(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["evidence_records"][2][
        "satisfies_current_live_evidence_requirement"
    ] = True

    result = evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=_passing_checks(manifest),
        readiness_matrix_path=None,
    )

    assert (
        "historical_representative_promoted:"
        "v0.2.0-historical-groot-freshness-negative"
    ) in result["blocking_reasons"]


def test_missing_current_vla_record_is_visible_in_matrix(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest["evidence_records"] = [
        record
        for record in manifest["evidence_records"]
        if record["evidence_id"]
        != "v0.2.0-vla-groot-libero-live-positive"
    ]

    result = evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=_passing_checks(manifest),
        readiness_matrix_path=None,
    )

    assert result["verification_matrix"]["vla_groot_libero"][
        "live_evidence"
    ] == "incompatible"
    assert (
        "v0_2_0_live_evidence_not_present:vla_groot_libero"
        in result["blocking_reasons"]
    )


def test_fixture_failure_does_not_change_live_evidence_status(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    checks = _passing_checks(manifest)
    checks["libero_instrumentation_fixture"] = lambda: False

    result = evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=checks,
        readiness_matrix_path=None,
    )

    row = result["verification_matrix"]["vla_groot_libero"]
    assert row["fixture"] == "fail"
    assert row["live_evidence"] == "present"
    assert row["implementation_bound"] == "matched_post_run_source_audit"


def test_current_materials_are_semantic_digests_not_file_digests() -> None:
    current = _current_components()
    expected = {
        "px4_gazebo_delivery_predicate_package": (
            "1",
            "ac6bf52b91c662147303a3ab3141cf531f8c4576da91f31695713b58961b9e05",
        ),
        "nav2_turtlebot3_predicate_package": (
            "1",
            "dd7b4eb8305cd9265c757f1889ecca128298665a41f13cca39f9aaa3b9624d2d",
        ),
        "libero_panda_predicate_package": (
            "2",
            "7949e9f2db452a1e1f751ff186aa0accc3b4915a4c79dda8a5b30a9c225e186d",
        ),
    }
    for component_id, (version, digest) in expected.items():
        assert current[component_id]["version"] == version
        assert current[component_id]["material_sha256"] == digest


def test_manifest_fixture_override_is_not_mutated(tmp_path: Path) -> None:
    manifest = _manifest()
    before = copy.deepcopy(manifest)
    evaluate_v0_2_0_stable_gate(
        manifest_path=_write_manifest(tmp_path, manifest),
        fixture_checks=_passing_checks(manifest),
        readiness_matrix_path=None,
    )
    assert manifest == before
