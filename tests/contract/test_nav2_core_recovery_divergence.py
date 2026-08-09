from __future__ import annotations

from scripts.smoke_nav2_core_recovery_divergence import build_report


def test_public_fixture_explains_core_recovery_candidate_divergence() -> None:
    report = build_report()

    assert report["comparison_scope"] == "publication_safe_fixture_pair"
    assert report["historical_internal_route_artifact_compared"] is False
    assert report["root_cause_classification"] == "missing_evidence"
    assert report["policy_binding_equal"] is True
    assert report["missing_reference_inputs"] == [
        "runtime_obstacle_size_z_m"
    ]

    blocked = report["blocked_case"]
    assert blocked["evaluation_status"] == "blocked"
    assert blocked["selected_candidate_id"] is None
    assert blocked["blocking_reasons"] == [
        "no_core_verified_recovery_candidate"
    ]
    assert len(blocked["candidates"]) == 1
    candidate = blocked["candidates"][0]
    assert candidate["core_action_feasibility_status"] == "unverified"
    assert candidate["unverified_reasons"] == [
        "nav2_obstacle_geometry_unverified",
        "nav2_candidate_3d_clearance_unverified",
    ]
    assert candidate["verification_items"] == [
        {
            "schema_version": "missionos_core_verification_item.v1",
            "item_id": "nav2_path_feasibility",
            "predicate": (
                "the bounded Nav2 path satisfies path, policy, costmap, "
                "frame, and 3D-clearance constraints"
            ),
            "status": "pending",
            "evidence_refs": candidate["evidence_refs"],
            "verification_basis": "unverified",
        }
    ]
    obstacle_fact = next(
        fact
        for fact in candidate["observed_fact_provenance"]
        if fact["name"] == "obstacle_collision_volume"
    )
    assert obstacle_fact["source"]["evidence_kind"] == (
        "source_backed_obstacle_collision_volume"
    )
    assert obstacle_fact["value"]["size_z_m"] is None
    assert all(value is False for value in blocked["authority"].values())
    assert all(value is False for value in candidate["authority"].values())

    reference = report["verified_reference_case"]
    assert reference["evaluation_status"] == "validated"
    assert reference["selected_candidate_id"] == "nav2-verified-bypass"
    assert reference["candidates"][0]["core_action_feasibility_status"] == (
        "verified_feasible"
    )


def test_divergence_report_never_upgrades_core_result_to_authority() -> None:
    report = build_report()

    assert all(value is False for value in report["authority_boundary"].values())
