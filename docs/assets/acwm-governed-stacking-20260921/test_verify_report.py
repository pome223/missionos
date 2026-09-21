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
    assert verifier.verify()["scores"] == [9, 4]


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
