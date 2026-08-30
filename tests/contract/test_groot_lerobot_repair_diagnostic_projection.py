from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from missionos_core import canonical_sha256
from src.runtime.groot_lerobot_repair_diagnostic_projection import (
    PROJECTION_SCHEMA_VERSION,
    project_groot_repair_diagnostics,
)


ROOT = Path(__file__).resolve().parents[2]
COHORT = ROOT / "docs/agents/evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json"
PUBLICATION = ROOT / "docs/agents/evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-publication.json"


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_projects_all_loops_without_backfilling_missing_axes() -> None:
    result = project_groot_repair_diagnostics(COHORT, PUBLICATION)

    assert result["schema_version"] == PROJECTION_SCHEMA_VERSION
    assert result["report_count"] == 5
    assert result["fresh_inference_performed"] is False
    assert result["paid_compute_used"] is False
    for report in result["reports"]:
        axes = {axis["axis"]: axis for axis in report["axes"]}
        assert axes["action_activity"]["status"] == "not_observed"
        assert axes["corrective_alignment"]["status"] == "not_observed"
        assert axes["predicate_recovery"]["status"] == "not_satisfied"
        assert axes["preservation"]["status"] == "satisfied"
        assert axes["stable_hold"]["status"] == "not_observed"
        assert axes["action_activity"]["evidence_refs"] == []
        assert axes["corrective_alignment"]["evidence_refs"] == []
        assert axes["stable_hold"]["evidence_refs"] == []
        assert report["first_failed_axis"] == "undetermined"
        assert report["next_unobserved_axis"] == "action_activity"
        assert report["approval_created"] is False
        assert report["dispatch_authority_created"] is False
        assert report["physical_execution_invoked"] is False

    body = {key: value for key, value in result.items() if key != "result_sha256"}
    assert result["result_sha256"] == canonical_sha256(body)


def test_rejects_source_file_digest_mismatch(tmp_path: Path) -> None:
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    copied_cohort = tmp_path / COHORT.name
    _write_json(copied_cohort, cohort)
    copied_cohort.write_text(
        copied_cohort.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    publication = json.loads(PUBLICATION.read_text(encoding="utf-8"))
    copied_publication = tmp_path / PUBLICATION.name
    _write_json(copied_publication, publication)

    with pytest.raises(ValueError, match="source_file_digest_mismatch"):
        project_groot_repair_diagnostics(copied_cohort, copied_publication)


def test_rejects_predicate_result_inconsistent_with_cohort(tmp_path: Path) -> None:
    cohort_bytes = COHORT.read_bytes()
    copied_cohort = tmp_path / COHORT.name
    copied_cohort.write_bytes(cohort_bytes)

    publication = json.loads(PUBLICATION.read_text(encoding="utf-8"))
    publication["cohort_result"]["source_record_sha256"] = hashlib.sha256(
        cohort_bytes
    ).hexdigest()
    altered = deepcopy(publication)
    altered["native_loop_context"][0]["terminal_goal_predicate_vector"][0] = True
    copied_publication = tmp_path / PUBLICATION.name
    _write_json(copied_publication, altered)

    with pytest.raises(ValueError, match="recovery_result_mismatch"):
        project_groot_repair_diagnostics(copied_cohort, copied_publication)


def test_criterion_references_bind_retained_material() -> None:
    result = project_groot_repair_diagnostics(COHORT, PUBLICATION)

    for report in result["reports"]:
        for axis in report["axes"]:
            assert axis["criterion_ref"] == (
                "sha256:" + canonical_sha256(axis["measurements"]["criterion"])
            )
