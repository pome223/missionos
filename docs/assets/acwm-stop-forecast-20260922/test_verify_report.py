"""Corrupt artifacts and refresh hashes: semantic checks must still reject them."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "verify_stop_forecast", BASE / "verify_report.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def manifest(root):
    (root / "manifest.json").write_text(
        json.dumps(
            {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*")
                if p.is_file()
                and "__pycache__" not in p.parts
                and p.name != "manifest.json"
            }
        )
    )


def test_real_package():
    assert module.verify()["new_games_executed"] == 0


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "duplicate",
        "label",
        "threshold",
        "aggregate",
        "png",
        "exposure",
        "gate",
    ],
)
def test_semantic_corruption(tmp_path, kind):
    root = tmp_path / "evidence"
    shutil.copytree(BASE, root, ignore=shutil.ignore_patterns("__pycache__"))
    path = root / "final-evaluation.json"
    data = json.loads(path.read_text())
    stage = data["after-seed195"]
    rows = stage["rows"]
    if kind == "empty":
        rows.clear()
    elif kind == "duplicate":
        rows[1] = rows[0].copy()
    elif kind == "label":
        rows[0]["collapsed"] = not rows[0]["collapsed"]
    elif kind == "threshold":
        rows[0]["predicted"] = not rows[0]["predicted"]
    elif kind == "aggregate":
        stage["summary"]["bank"]["tp"] += 1
    elif kind == "png":
        (root / "initial-bank-failure.png").write_bytes(b"x" * 100000)
    elif kind == "exposure":
        p = root / "final-training.json"
        x = json.loads(p.read_text())
        x["exposures"][next(iter(x["exposures"]))] += 1
        p.write_text(json.dumps(x))
    elif kind == "gate":
        p = root / "experiment.json"
        x = json.loads(p.read_text())
        x["final_gate_pass"] = not x["final_gate_pass"]
        p.write_text(json.dumps(x))
    path.write_text(json.dumps(data))
    manifest(root)
    with pytest.raises((AssertionError, ValueError, KeyError)):
        module.verify(root)
