from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.nav2_action_feasibility_corpus import (
    seal_nav2_corpus_case,
    verify_nav2_corpus,
    verify_nav2_corpus_case,
    verify_nav2_corpus_through_core,
)
from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    verify_action_feasibility_corpus_through_core,
)


pytestmark = pytest.mark.contract
GOLDEN_ROOT = Path(__file__).parents[1] / "golden" / "action_feasibility"
NAV2_ROOT = GOLDEN_ROOT / "nav2_v1"
NAV2_MANIFEST = NAV2_ROOT / "manifest.json"
PX4_MANIFEST = GOLDEN_ROOT / "px4_v1" / "manifest.json"


def _case(case_id: str) -> dict:
    return json.loads(
        (NAV2_ROOT / "cases" / f"{case_id}.json").read_text(
            encoding="utf-8"
        )
    )


def test_nav2_manifest_replays_positive_and_refusal_cases_offline() -> None:
    verdict = verify_nav2_corpus(NAV2_MANIFEST)

    assert verdict["status"] == "verified"
    assert verdict["case_count"] == 5
    assert verdict["positive_case_count"] == 1
    assert verdict["refusal_case_count"] == 4
    assert verdict["reasons"] == []
    assert verdict["approval_created"] is False
    assert verdict["dispatch_authority_created"] is False
    assert verdict["execution_invoked"] is False
    assert verdict["progress_claimed"] is False
    assert verdict["completion_claimed"] is False


@pytest.mark.parametrize(
    ("manifest", "runner", "expected_count"),
    [
        (
            PX4_MANIFEST,
            verify_action_feasibility_corpus_through_core,
            8,
        ),
        (NAV2_MANIFEST, verify_nav2_corpus_through_core, 5),
    ],
    ids=("px4", "nav2"),
)
def test_px4_and_nav2_use_the_same_core_conformance_runner(
    manifest: Path,
    runner,
    expected_count: int,
) -> None:
    verdict = runner(manifest)

    assert verdict["schema_version"] == (
        "missionos_core_conformance_run.v1"
    )
    assert verdict["status"] == "verified"
    assert verdict["case_count"] == expected_count
    assert verdict["reasons"] == []
    assert all(item["passed"] for item in verdict["case_verdicts"])


@pytest.mark.parametrize(
    ("case_id", "expected_status", "required_reason"),
    [
        ("nav2-positive-verified-bypass", "verified_feasible", ""),
        (
            "nav2-refusal-collision-path",
            "blocked",
            "nav2_candidate_collision_envelope_intersection",
        ),
        (
            "nav2-refusal-missing-obstacle-geometry",
            "unverified",
            "nav2_obstacle_geometry_unverified",
        ),
        (
            "nav2-refusal-frame-mismatch",
            "unverified",
            "nav2_path_obstacle_frame_mismatch",
        ),
        (
            "nav2-refusal-stale-dynamic-observation",
            "unverified",
            "nav2_dynamic_observation_stale",
        ),
    ],
)
def test_nav2_cases_preserve_exact_tri_state_semantics(
    case_id: str,
    expected_status: str,
    required_reason: str,
) -> None:
    case = _case(case_id)

    verdict = verify_nav2_corpus_case(case)

    assert verdict["passed"] is True
    assert verdict["status"] == expected_status
    assert verdict["reasons"] == []
    if required_reason:
        assert case["expected"]["required_reason"] == required_reason
    assert verdict["source_runtime_reexecuted"] is False
    assert all(flag is False for flag in verdict["output_flags"].values())


@pytest.mark.parametrize(
    "private_value",
    [
        {"task_id": "task_deadbeefcafebabe"},
        {"artifact_path": "/Users/operator/private/evidence.json"},
        {"credential": "sk-private-example-not-for-publication"},
        {"link_endpoint": "/dev/ttyACM0"},
        {"autopilot_uid": "000200000000383832343437511800230026"},
        {"approval_actor": "an-operator-real-name"},
    ],
)
def test_nav2_publication_sanitizer_rejects_private_material(
    private_value: dict,
) -> None:
    unsafe = _case("nav2-positive-verified-bypass")
    unsafe["unsafe"] = private_value
    unsafe = seal_nav2_corpus_case(unsafe)

    verdict = verify_nav2_corpus_case(unsafe)

    assert verdict["passed"] is False
    assert "nav2_corpus_publication_boundary_violated" in verdict["reasons"]
