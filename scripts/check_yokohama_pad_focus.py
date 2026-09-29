#!/usr/bin/env python3
"""Verify published native ANWM regional forecasts without GPU or simulator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.diagnose_yokohama_pad_focus import diagnose
from scripts.evaluate_yokohama_pad_focus import evaluate
from scripts.yokohama_pad_focus_data import sha


def check(bundle):
    manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()}
    if actual != {"evidence-manifest.json", *manifest["files"]}:
        raise ValueError("Unlisted public asset or missing evidence")
    for name, digest in manifest["files"].items():
        path = bundle / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(bundle.resolve())
            or sha(path) != digest
        ):
            raise ValueError("Public evidence changed: " + name)
    protocol = json.loads((bundle / "focus-protocol.json").read_text())
    for name, digest in protocol["source_sha256"].items():
        if sha(bundle / "executed-source" / name) != digest:
            raise ValueError("Executed source changed")
    result = evaluate(bundle / "prepared", bundle / "results", bundle / "focus-protocol.json")
    saved = json.loads((bundle / "evaluation.json").read_text())
    if (
        result["counts"] != saved["counts"]
        or result["evaluation_conditions"] != saved["evaluation_conditions"]
    ):
        raise ValueError("Native endpoint result changed")
    for recomputed, original in zip(result["rows"], saved["rows"], strict=True):
        if recomputed["id"] != original["id"] or recomputed["truth"] != original["truth"]:
            raise ValueError("Evaluation identity changed")
        for stage, row in recomputed["methods"].items():
            old = original["methods"][stage]
            if row["state"] != old["state"] or abs(row["rgb_mae"] - old["rgb_mae"]) > 1e-9:
                raise ValueError("Saved image metric changed")
    diagnosis = json.loads(json.dumps(diagnose(bundle)))
    if diagnosis != json.loads((bundle / "diagnosis.json").read_text()):
        raise ValueError("Post-hoc diagnosis changed")
    cost = json.loads((bundle / "cost.json").read_text())
    if (
        not cost["cleanup_confirmed"]
        or cost["cumulative_estimated_usd"] > cost["authorized_total_usd"]
    ):
        raise ValueError("Budget or cleanup not closed")
    return dict(
        status="verified",
        native_forecasts=result["evaluation_conditions"] * len(result["rows"][0]["methods"]),
        native_inference_reexecuted=False,
        aircraft_flown=False,
        counts=result["counts"],
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(check(args.bundle), indent=2))
