"""Adversarial consistency checks independent of the manifest checksum guard."""

import importlib.util
import json
from pathlib import Path
import shutil
import pytest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("verify_governed_acwm", ROOT / "verify_report.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def test_published_chain():
    verified = verifier.verify()
    assert verified["scores"] == [9, 4]
    legacy = verified["known_legacy_inconsistencies"]["decision.llm_invoked"]
    assert legacy["recorded_value"] is False
    assert legacy["actual_invocation_value"] is True
    assert legacy["affected_decisions"] == verified["llm_judgments"] == 15


@pytest.mark.parametrize("mutation", ["empty", "score", "ticket", "prediction", "timing", "vla"])
def test_corruption_is_detected(tmp_path, mutation):
    root = tmp_path / "evidence"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    p = root / "run.json"
    data = json.loads(p.read_text())
    first = data["games"][0]["steps"][0]
    if mutation == "empty":
        data["games"] = []
    elif mutation == "score":
        data["games"][0]["score"] = 10
    elif mutation == "ticket":
        first["receipt"]["dispatch_ticket"] = "unbound"
    elif mutation == "prediction":
        first["generation"]["readout_output"] = 0.99
    elif mutation == "timing":
        first["receipt"]["runtime_invocation"]["started_at"] = 0
    else:
        first["vla_inferences"] = []
    p.write_text(json.dumps(data))
    with pytest.raises(AssertionError):
        verifier.verify(root, manifest=False)


def test_modified_api_prompt_detected_without_manifest(tmp_path):
    root = tmp_path / "evidence"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    data = json.loads((root / "run.json").read_text())
    ref = data["games"][0]["steps"][0]["decision"]["proposal"]["model_invocation_evidence"][
        "record_ref"
    ]
    p = root / ref
    record = json.loads(p.read_text())
    record["request_payload"]["messages"][1]["content"] = "No prediction provided"
    p.write_text(json.dumps(record))
    with pytest.raises(AssertionError):
        verifier.verify(root, manifest=False)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("legacy", True, "known legacy llm_invoked"),
        ("legacy", 0, "known legacy llm_invoked"),
        ("actual", False, "actual model invocation required"),
    ],
)
def test_known_legacy_mismatch_is_explicitly_checked(tmp_path, field, value, message):
    root = tmp_path / "evidence"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    p = root / "run.json"
    data = json.loads(p.read_text())
    decision = data["games"][0]["steps"][0]["decision"]
    if field == "legacy":
        decision["llm_invoked"] = value
    else:
        decision["proposal"]["model_inference_invoked"] = value
    p.write_text(json.dumps(data))
    with pytest.raises(AssertionError, match=message):
        verifier.verify(root, manifest=False)
