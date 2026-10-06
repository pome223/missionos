"""Run opt-in analytic, NASA rotational-subset and native Basilisk checks."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_sixdof_validation import run_validation  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--native-basilisk", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_simulation:
        parser.error("--approve-simulation is required")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    result = run_validation(native_basilisk_enabled=args.native_basilisk, approve_simulation=True)
    path = args.output_dir / "validation.json"
    path.write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    manifest = {"schema": "missionos.starship_sixdof_validation_manifest.v1",
                "files": {"validation.json": sha256(path.read_bytes()).hexdigest()},
                "source_sha256": result["source_sha256"], "physical_execution": False}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]),
                      "native_basilisk_invoked": result["runtime_invocation"]["native_basilisk_invoked"],
                      "nasa_full_checkcase_passed": False, "output": str(path)}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
