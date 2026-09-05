from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from src.runtime.libero_recovery_training_phase0 import canonical_sha256
from src.runtime.libero_recovery_training_preflight import training_preflight_summary


ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/agents/evidence/20260905-libero-recovery-training-preflight.json"


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def _resign(record: dict) -> None:
    material = {key: value for key, value in record.items() if key != "result_sha256"}
    record["result_sha256"] = canonical_sha256(material)


def test_checked_in_preflight_is_valid_no_go() -> None:
    record = _record()
    assert training_preflight_summary(record, repository_root=ROOT) == {
        "status": "valid",
        "decision": "NO_GO",
        "paid_training_authorized": False,
        "gpu_provision_authorized": False,
        "errors": [],
    }


def test_l4_cannot_be_promoted_to_training_hardware() -> None:
    record = _record()
    record["hardware"]["l4_training_eligible"] = True
    _resign(record)
    assert "preflight_l4_must_be_rejected" in training_preflight_summary(record)["errors"]


def test_unreviewed_cost_cap_cannot_authorize_gpu() -> None:
    record = _record()
    record["decision"]["status"] = "GO"
    record["decision"]["gpu_provision_authorized"] = True
    _resign(record)
    errors = training_preflight_summary(record)["errors"]
    assert "preflight_decision_must_be_no_go" in errors
    assert "preflight_gpu_provision_must_be_false" in errors


def test_budget_alert_is_not_accepted_as_hard_stop() -> None:
    record = _record()
    record["cost_control"]["billing_budget_is_hard_stop"] = True
    _resign(record)
    assert "preflight_budget_must_not_claim_hard_stop" in training_preflight_summary(record)[
        "errors"
    ]


def test_tampering_is_detected() -> None:
    record = deepcopy(_record())
    record["cost_control"]["proposed_hard_cap_jpy"] = 100000
    assert "preflight_result_digest_mismatch" in training_preflight_summary(record)["errors"]


def test_dataset_manifest_file_digest_is_verified() -> None:
    record = _record()
    record["dataset"]["manifest_file_sha256"] = "0" * 64
    _resign(record)
    assert "preflight_dataset_manifest_file_digest_mismatch" in training_preflight_summary(
        record, repository_root=ROOT
    )["errors"]
