from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.libero_recovery_training_phase0 import canonical_sha256
from scripts.run_libero_recovery_geometry_cohort import (
    BASELINE,
    DIRECTIONS,
    DISTANCES_METRES,
    PREREGISTERED_GEOMETRY_PROBE,
    execute,
)
from scripts.generate_libero_registered_skill_fixture import DISPLACEMENT_DIRECTION


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/agents/evidence/20260905-libero-recovery-geometry-expansion.json"


def test_geometry_probe_covers_remaining_preregistered_conditions_once() -> None:
    assert DISPLACEMENT_DIRECTION == "negative_x"
    assert len(PREREGISTERED_GEOMETRY_PROBE) == 11
    assert len(set(PREREGISTERED_GEOMETRY_PROBE)) == 11
    assert BASELINE not in PREREGISTERED_GEOMETRY_PROBE
    assert set(PREREGISTERED_GEOMETRY_PROBE) | {BASELINE} == {
        (distance, direction) for distance in DISTANCES_METRES for direction in DIRECTIONS
    }


def test_geometry_probe_requires_explicit_opt_in(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RUN_MISSIONOS_LIBERO_RECOVERY_GEOMETRY_COHORT", raising=False)
    with pytest.raises(RuntimeError, match="opt_in_required"):
        execute(output_dir=tmp_path / "geometry")


def test_checked_in_geometry_screen_preserves_no_go_boundary() -> None:
    record = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    material = {key: value for key, value in record.items() if key != "result_sha256"}

    assert record["result_sha256"] == canonical_sha256(material)
    assert record["summary"] == {
        "artifact_backed_geometry_conditions_evaluated": 7,
        "new_recoverable_training_candidates": 0,
        "new_raw_negative_transition_captures": 1,
        "training_examples_admitted": 0,
        "strongest_bounded_result": (
            "the existing three-centimetre negative-x fixture remains the only "
            "admitted recoverable geometry in this screen"
        ),
    }
    assert record["runtime"]["gpu_used"] is False
    assert record["runtime"]["instance_absent_after_run"] is True
    assert record["runtime"]["boot_disk_absent_after_run"] is True
    assert record["access_and_quota_preflight"]["a100_quota_preference"] == {
        "region": "us-central1",
        "preferred_value": 1,
        "granted_value": 0,
        "reconciling": True,
    }
    assert record["next_gate"]["paid_training_authorized"] is False
    assert record["next_gate"]["gpu_provision_authorized"] is False
    assert record["claim_boundary"]["training_invoked"] is False
    assert record["claim_boundary"]["model_inference_invoked"] is False
    assert record["claim_boundary"]["physical_execution_invoked"] is False
