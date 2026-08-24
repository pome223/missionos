from __future__ import annotations

import math

import pytest

from scripts.run_groot_lerobot_same_world_repair import execute_live
from src.runtime.libero_repair_failure_fixture import (
    FAILURE_FIXTURE_SPECS,
    SCRIPTED_FAILURE_FIXTURE_BASIS,
    failure_fixture_contract,
    failure_fixture_spec,
)


def test_failure_fixture_catalog_uses_visible_failures() -> None:
    assert set(FAILURE_FIXTURE_SPECS) == {
        "displaced_from_stove",
        "wrong_table_location",
        "tipped_over",
        "dropped_during_scripted_transfer",
    }
    for spec in FAILURE_FIXTURE_SPECS.values():
        assert spec.minimum_translation_metres >= 0.05
        assert spec.minimum_outside_clearance_metres >= 0.05
        assert spec.settle_steps >= 60
        assert math.isfinite(spec.local_x_clearance_metres)
        assert math.isfinite(spec.local_y_clearance_metres)


def test_failure_fixture_contract_grants_no_authority() -> None:
    material = failure_fixture_contract("displaced_from_stove")

    assert material["source_failure_basis"] == SCRIPTED_FAILURE_FIXTURE_BASIS
    assert material["authority"] == "test_fixture_only"
    assert material["human_approval_created"] is False
    assert material["repair_proposal_created"] is False
    assert material["governed_dispatch_created"] is False
    assert material["model_inference_invoked"] is False
    assert material["physical_execution_invoked"] is False
    assert material["specification"]["minimum_translation_metres"] == 0.05


def test_failure_fixture_scenario_fails_closed() -> None:
    with pytest.raises(
        ValueError,
        match="libero_repair_failure_fixture_scenario_invalid",
    ):
        failure_fixture_spec("nearly_on_threshold")


def test_live_runner_requires_explicit_scripted_fixture_basis(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    with pytest.raises(
        ValueError,
        match="scripted_failure_fixture_source_basis_required",
    ):
        execute_live(
            checkpoint_path=checkpoint,
            operator_approval_ref="operator:test",
            dispatch_state_path=tmp_path / "dispatch.json",
            maximum_repair_chunks=45,
            scripted_failure_fixture="displaced_from_stove",
        )


def test_live_runner_rejects_fixture_basis_without_scenario(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    with pytest.raises(
        ValueError,
        match="scripted_failure_fixture_scenario_required",
    ):
        execute_live(
            checkpoint_path=checkpoint,
            operator_approval_ref="operator:test",
            dispatch_state_path=tmp_path / "dispatch.json",
            maximum_repair_chunks=45,
            source_failure_basis=SCRIPTED_FAILURE_FIXTURE_BASIS,
        )
