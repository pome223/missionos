"""Check portable report integrity; raw flight re-verification is separate."""

import argparse
import hashlib
import json
import math
from pathlib import Path


def check(root):
    manifest = json.loads((root / "manifest.json").read_text())
    for name, digest in manifest["artifact_sha256"].items():
        if (
            Path(name).name != name
            or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
        ):
            raise ValueError("artifact differs: " + name)
    summary = json.loads((root / "summary.json").read_text())
    replay = json.loads((root / "replay-data.json").read_text())
    html = (root / "index.html").read_text()
    embedded = json.loads(html.split("const D=", 1)[1].split(", slider=", 1)[0])
    if embedded != replay or len(replay["cases"]) != 2 or summary["cohort_size"] != 2:
        raise ValueError("HTML/dataset/cohort differs")
    if [c["result"] for c in replay["cases"]] != summary["runs"]:
        raise ValueError("metrics differ between replay and summary")
    for case in replay["cases"]:
        previous = -1.0
        for sample in case["trace"]:
            values = [sample["t"], *sample["p"]]
            if (
                len(values) != 4
                or not all(type(v) in (int, float) and math.isfinite(v) for v in values)
                or sample["t"] <= previous
            ):
                raise ValueError("invalid/reversed/duplicate display sample")
            previous = sample["t"]
        if previous > case["result"]["elapsed_departure_through_disarm_sim_s"]:
            raise ValueError("trajectory exceeds observed interval")
    if summary["learned_navigation_benefit_established"] is not False:
        raise ValueError("unsupported learned-navigation claim")
    return {
        "portable_report_verified": True,
        "cases": len(replay["cases"]),
        "raw_flight_verification_repeated": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.root)))
