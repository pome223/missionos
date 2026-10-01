"""The report must retain failed flights and reject misleading denominators."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import report_onboard_uncertainty as report


@pytest.fixture
def batch(tmp_path, monkeypatch):
    repo = Path(report.__file__).resolve().parents[1]
    name = "src/runtime/ship_onboard_uncertainty.py"
    data = (repo / name).read_bytes()
    target = tmp_path / "source" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    matrix = [
        (c, p)
        for c in ("short_clear", "long_block", "brake_stop")
        for p in ("onboard_stopping", "onboard_uncertainty")
    ]
    protocol = {
        "schema_version": "ship_onboard_uncertainty_batch.v1",
        "gpu_cost_usd": 0,
        "source_sha256": {name: hashlib.sha256(data).hexdigest()},
        "matrix": matrix,
        "frozen_at_utc": "2026-09-25T00:00:00+00:00",
    }
    (tmp_path / "protocol-freeze.json").write_text(json.dumps(protocol))
    attempts, verified = [], {}
    for i, (case, policy) in enumerate(matrix):
        directory = tmp_path / str(i)
        directory.mkdir()
        result = {
            "status": "completed",
            "container_removed": True,
            "config": {"run_id": str(i), "urban": {"case": case, "policy": policy}},
        }
        (directory / "result.json").write_text(json.dumps(result))
        (directory / "events.jsonl").write_text(json.dumps({"event": "urban_decision"}) + "\n")
        attempts.append(
            {
                "case": case,
                "policy": policy,
                "directory": str(i),
                "urban_decision_recorded": True,
                "started_at_utc": "2026-09-25T01:00:00+00:00",
                "status": "completed",
            }
        )
        verified[str(i)] = {
            "case": case,
            "policy": policy,
            "action": "wait",
            "urban_elapsed_s": 60,
            "world_sha256": "world",
            "sources_sha256": {"source": "same"},
            "image_id": "image",
            "scenario_parameters": {"offshore_distance_m": 100},
            "matched_image_rule_replay": {
                "onboard_stopping": {"action": "wait"},
                "onboard_uncertainty": {"action": "wait", "uncertainty_gate": {}},
            },
        }
    (tmp_path / "attempts.json").write_text(json.dumps(attempts))

    def reverify(path):
        value = verified[path.name]
        if value is None:
            raise ValueError("incomplete flight")
        return deepcopy(value)

    monkeypatch.setattr(report, "reverify_onboard_run", reverify)
    return tmp_path, attempts, verified


def test_matched_integration_does_not_claim_cpu_transfer_or_native_models(batch):
    root, _, _ = batch
    result = report.audit_batch(root)
    assert result["paired_matrix_complete"] and result["initial_gate_runtime_integration_passed"]
    assert not result["step2_native_vla_wam_completed"]
    assert not result["two_dynamic_route_cpu_efficacy_transferred"]


def test_failure_stays_in_denominator_and_blocks_integration_success(batch):
    root, attempts, verified = batch
    attempts[-1]["status"] = "blocked"
    (root / "attempts.json").write_text(json.dumps(attempts))
    path = root / attempts[-1]["directory"] / "result.json"
    value = json.loads(path.read_text())
    value["status"] = "blocked"
    path.write_text(json.dumps(value))
    verified[attempts[-1]["directory"]] = None
    result = report.audit_batch(root)
    assert result["attempt_count"] == 6 and result["verified_flight_count"] == 5
    assert len(result["failures_retained"]) == 1
    assert not result["paired_matrix_complete"]
    assert not result["initial_gate_runtime_integration_passed"]


@pytest.mark.parametrize("fault", ["drop", "hide_decision", "same_identity", "source"])
def test_invalid_evidence_cannot_become_a_successful_comparison(batch, fault):
    root, attempts, _ = batch
    if fault == "drop":
        attempts.pop()
    elif fault == "hide_decision":
        attempts[-1]["urban_decision_recorded"] = False
    elif fault == "same_identity":
        path = root / attempts[-1]["directory"] / "result.json"
        value = json.loads(path.read_text())
        value["config"]["run_id"] = "0"
        path.write_text(json.dumps(value))
    else:
        (root / "source/src/runtime/ship_onboard_uncertainty.py").write_text("changed")
    (root / "attempts.json").write_text(json.dumps(attempts))
    with pytest.raises(ValueError):
        report.audit_batch(root)
