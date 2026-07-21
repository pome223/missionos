"""Contract tests for perception claims and the corroboration guard (issue #31).

The support rule under test is asymmetric: an uncorroborated camera-only claim
may support conservative (fail-safe) recovery actions but never progressive
ones; corroborated claims may support either. The guard blocks, it never
rewrites the LLM's selected action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from src.intelligence.turtlebot3_recovery_planner import (
    guard_turtlebot3_recovery_planner_output,
)
from src.runtime.perception_claim import (
    CONSERVATIVE_RECOVERY_ACTIONS,
    PERCEPTION_CLAIM_SCHEMA_VERSION,
    PerceptionClaimError,
    build_perception_claim,
    guard_perception_claim_support,
)
from src.runtime.perception_corroboration_binding import (
    build_perception_corroboration_binding,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
FRAME_REF = "sha256:" + "a" * 64


def _camera_only_claim():
    return build_perception_claim(
        claim_kind="corridor_blocked_by_object",
        source_frame_ref=FRAME_REF,
        confidence=0.8,
        observed_at=NOW,
    )


def _corroborated_claim():
    return build_perception_claim(
        claim_kind="corridor_blocked_by_object",
        source_frame_ref=FRAME_REF,
        confidence=0.8,
        corroborated_by=["lidar_costmap:occupied_cells_ahead"],
        observed_at=NOW,
    )


def _bound_claim():
    stdout = "{}"
    stderr = ""
    binding = build_perception_corroboration_binding(
        source_frame_ref=FRAME_REF,
        claim_kind="corridor_blocked_by_object",
        camera_horizontal_sector="center",
        target_center_x_normalized=0.5,
        runtime_context={
            "decision_epoch_ref": "test:perception",
            "capture": {
                "camera_frame_sha256": "a" * 64,
                "camera_lidar_observation": {
                    "camera_observed_at": NOW.isoformat(),
                    "camera_received_at": (
                        NOW - timedelta(milliseconds=50)
                    ).isoformat(),
                    "camera_width": 640,
                    "camera_fx": 554.25,
                    "camera_cx": 320.0,
                    "lidar_observed_at": (
                        NOW + timedelta(milliseconds=100)
                    ).isoformat(),
                    "lidar_obstacle_observed": True,
                    "lidar_horizontal_sector": "center",
                    "lidar_candidate_bearing_rad": 0.0,
                    "target_candidate_id": "lidar_candidate:test",
                    "lidar_evidence_ref": "laser_scan:test",
                },
            },
            "llm_invocation_evidence": {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "llm_api",
                "invocation_target": "google_adk:gemini-test",
                "provider": "google_adk",
                "model_id": "gemini-test",
                "input_image_sha256": "a" * 64,
                "invocation_started_at": NOW.isoformat(),
                "invocation_completed_at": (
                    NOW + timedelta(milliseconds=100)
                ).isoformat(),
                "invocation_stdout_sha256": sha256(stdout.encode()).hexdigest(),
                "invocation_stderr_sha256": sha256(stderr.encode()).hexdigest(),
                "invocation_stdout_preimage": stdout,
                "invocation_stderr_preimage": stderr,
                "invocation_exit_code": 0,
                "invocation_ref": "vlm_invocation:test",
                "physical_execution_invoked": False,
            },
        },
    )
    return build_perception_claim(
        claim_kind="corridor_blocked_by_object",
        source_frame_ref=FRAME_REF,
        confidence=0.8,
        corroborated_by=["lidar_costmap:occupied_cells_ahead"],
        corroboration_binding=binding,
        observed_at=NOW,
    )


def test_claim_builds_as_evidence_only_artifact() -> None:
    claim = _corroborated_claim()
    assert claim.schema_version == PERCEPTION_CLAIM_SCHEMA_VERSION
    assert claim.claim_id.startswith("perception_claim_")
    assert claim.corroborated is True
    assert claim.evidence_only is True
    assert claim.approval_created is False
    assert claim.dispatch_authority_created is False
    assert claim.physical_execution_invoked is False
    assert _camera_only_claim().corroborated is False


def test_claim_rejects_unhashed_frame_ref() -> None:
    with pytest.raises(ValueError):
        build_perception_claim(
            claim_kind="path_clear",
            source_frame_ref="frame_042.png",
            confidence=0.9,
            observed_at=NOW,
        )


def test_claim_rejects_camera_and_unknown_corroboration_sources() -> None:
    for bad_source in ("camera:front_rgb", "vlm:frame_judgment", "sonar:ping"):
        with pytest.raises(ValueError):
            build_perception_claim(
                claim_kind="corridor_blocked_by_object",
                source_frame_ref=FRAME_REF,
                confidence=0.8,
                corroborated_by=[bad_source],
                observed_at=NOW,
            )


def test_claim_rejects_command_like_metadata() -> None:
    with pytest.raises(PerceptionClaimError):
        build_perception_claim(
            claim_kind="corridor_blocked_by_object",
            source_frame_ref=FRAME_REF,
            confidence=0.8,
            observed_at=NOW,
            metadata={"cmd_vel": {"linear_x": 0.2}},
        )


def test_uncorroborated_claim_supports_conservative_actions_only() -> None:
    claim = _camera_only_claim()
    for action in sorted(CONSERVATIVE_RECOVERY_ACTIONS):
        support = guard_perception_claim_support(
            selected_action=action,
            cited_claim_ids=[claim.claim_id],
            perception_claims=[claim],
        )
        assert support["blocking_reasons"] == [], action
        assert support["checks"]["perception_claim_support_respected"] is True

    blocked = guard_perception_claim_support(
        selected_action="avoid_obstacle",
        cited_claim_ids=[claim.claim_id],
        perception_claims=[claim],
    )
    assert blocked["checks"]["perception_claim_support_respected"] is False
    assert blocked["uncorroborated_claim_ids_in_context"] == [claim.claim_id]
    assert blocked["blocking_reasons"] == [
        "perception_claim_without_bound_live_corroboration_requires_"
        f"conservative_action:{claim.claim_id}"
    ]


def test_uncited_uncorroborated_claim_still_blocks_progressive_action() -> None:
    """Review found citation omission could bypass the guard; the rule now
    enforces on every claim in context, cited or not."""

    claim = _camera_only_claim()
    blocked = guard_perception_claim_support(
        selected_action="avoid_obstacle",
        cited_claim_ids=[],
        perception_claims=[claim],
    )
    assert blocked["checks"]["perception_claim_support_respected"] is False
    assert blocked["uncorroborated_claim_ids_in_context"] == [claim.claim_id]
    assert blocked["blocking_reasons"] == [
        "perception_claim_without_bound_live_corroboration_requires_"
        f"conservative_action:{claim.claim_id}"
    ]


def test_corroborated_claim_supports_progressive_action() -> None:
    claim = _bound_claim()
    support = guard_perception_claim_support(
        selected_action="avoid_obstacle",
        cited_claim_ids=[claim.claim_id],
        perception_claims=[claim],
    )
    assert support["blocking_reasons"] == []
    assert support["checks"]["cited_perception_claims_known"] is True
    assert support["checks"]["perception_claim_support_respected"] is True


def test_unknown_cited_claim_id_is_blocked() -> None:
    support = guard_perception_claim_support(
        selected_action="hold",
        cited_claim_ids=["perception_claim_deadbeef00000000"],
        perception_claims=[],
    )
    assert support["checks"]["cited_perception_claims_known"] is False
    assert support["blocking_reasons"] == [
        "cited_perception_claim_unknown:perception_claim_deadbeef00000000"
    ]


def test_planner_guard_blocks_progressive_action_on_camera_only_claim() -> None:
    claim = _camera_only_claim()
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Camera shows the corridor is blocked; go around it.",
            "input_observations": {},
            "cited_perception_claim_ids": [claim.claim_id],
        },
        perception_claims=[claim],
    )
    assert guardrail["guardrail_passed"] is False
    assert guardrail["checks"]["perception_claim_support_respected"] is False
    assert guardrail["validated_proposal"] == {}
    assert (
        "perception_claim_without_bound_live_corroboration_requires_"
        f"conservative_action:{claim.claim_id}"
    ) in guardrail["blocking_reasons"]


def test_planner_guard_blocks_progressive_action_when_citation_omitted() -> None:
    """The bypass the review found: rely on a camera claim, select a
    progressive action, and just not cite the claim. Blocked now — the
    support rule runs on context, and the citation field itself is required
    whenever claims were provided."""

    claim = _camera_only_claim()
    no_field = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Going around what the camera showed.",
            "input_observations": {},
        },
        perception_claims=[claim],
    )
    assert no_field["guardrail_passed"] is False
    assert (
        "cited_perception_claim_ids_required_when_claims_present"
        in no_field["blocking_reasons"]
    )

    empty_citation = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Going around what the camera showed.",
            "input_observations": {},
            "cited_perception_claim_ids": [],
        },
        perception_claims=[claim],
    )
    assert empty_citation["guardrail_passed"] is False
    assert (
        "perception_claim_without_bound_live_corroboration_requires_"
        f"conservative_action:{claim.claim_id}"
    ) in empty_citation["blocking_reasons"]


def test_planner_guard_passes_conservative_action_on_camera_only_claim() -> None:
    claim = _camera_only_claim()
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "hold",
            "reason": "Camera shows a possible obstruction; hold and confirm.",
            "input_observations": {},
            "cited_perception_claim_ids": [claim.claim_id],
        },
        perception_claims=[claim],
    )
    assert guardrail["guardrail_passed"] is True
    assert guardrail["validated_proposal"]["cited_perception_claim_ids"] == [
        claim.claim_id
    ]
    assert guardrail["perception_claim_support"][
        "selected_action_is_conservative"
    ] is True
    assert guardrail["dispatch_authority_created"] is False


def test_planner_guard_passes_progressive_action_on_corroborated_claim() -> None:
    claim = _bound_claim()
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Camera and costmap agree the corridor is blocked.",
            "input_observations": {},
            "cited_perception_claim_ids": [claim.claim_id],
        },
        perception_claims=[claim],
    )
    assert guardrail["guardrail_passed"] is True
    assert guardrail["checks"]["perception_claim_support_respected"] is True


def test_planner_guard_rejects_malformed_cited_ids() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "hold",
            "reason": "Holding.",
            "input_observations": {},
            "cited_perception_claim_ids": "not-a-list",
        },
        perception_claims=[],
    )
    assert guardrail["guardrail_passed"] is False
    assert guardrail["checks"]["cited_perception_claim_ids_list"] is False
    assert (
        "cited_perception_claim_ids_must_be_string_list"
        in guardrail["blocking_reasons"]
    )


def test_planner_guard_without_citations_keeps_existing_behavior() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Battery reserve is insufficient for the remaining route.",
            "input_observations": {"battery_start_pct": 20.0},
        },
        source_observations={"battery_start_pct": 20.0},
    )
    assert guardrail["guardrail_passed"] is True
    assert guardrail["validated_proposal"]["cited_perception_claim_ids"] == []
    assert guardrail["perception_claim_support"] == {}
