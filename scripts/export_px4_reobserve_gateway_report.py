"""Verify saved Gateway/flight bindings and export only reviewed portable fields."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.render_urban_reobserve_report import render  # noqa: E402
from src.runtime import px4_depth_navigation as depth  # noqa: E402
from src.runtime.px4_reobserve_navigation import verify_run  # noqa: E402


def export(run, output):
    def read(name):
        return json.loads((run / name).read_text())

    summary = read("summary.json")
    execution = read("gap-execution.json")
    task = read("gap-job-status.json")["task"]["task"]
    task_id = task["task_id"]
    if (
        summary["check_passed"] is not True
        or summary["scope"] != "live_px4_gateway"
        or summary["reobserve"] is not True
        or len(summary["cases"]) != 1
        or Path(task_id).name != task_id
        or execution["task_id"] != task_id
        or summary["cases"][0]["task_id"] != task_id
        or task["status"] != "completed"
    ):
        raise ValueError("completed same-task Gateway flight required")
    artifacts = task["artifacts"]
    request, result = artifacts[depth.REQUEST], artifacts[depth.RESULT]
    approval_id = result["execution_approval_id"]
    approvals = artifacts[depth.APPROVALS]
    approval = approvals[approval_id]
    if len(approvals) != 1 or approval["consumed_in_runtime"] is not True:
        raise ValueError("one consumed approval required")
    case = run / "flights" / task_id
    verified = verify_run(case, request, approval)
    if (
        result != verified
        or execution["execute_result"][depth.RESULT] != verified
        or execution["execute_result"]["task"]["task_id"] != task_id
        or summary["cases"][0]["result"] != verified
        or summary["cases"][0]["replay_rejected"] is not True
    ):
        raise ValueError("Gateway/raw-flight result mismatch")
    render(case, output, single_run=True)
    evidence = {
        "schema_version": "px4_reobserve_gateway_evidence.v1",
        "flight_attempts": 1,
        "same_task_in_execution_and_status": True,
        "task_status": task["status"],
        "approval_count": len(approvals),
        "approval_consumed": True,
        "approval_scope": approval["scope"],
        "replay_rejected": True,
        "phases": [e["phase"] for e in artifacts["px4_depth_navigation_lifecycle"]["entries"]],
        "runtime_source_sha256": request["runtime_source_sha256"],
        "request_sha256": result["request_sha256"],
        "model_calls": summary["new_model_calls"],
        "rented_gpus": summary["new_rented_gpus"],
        "gateway_result_equals_independent_verifier": True,
        "raw_sha256": {
            name: hashlib.sha256((run / name).read_bytes()).hexdigest()
            for name in ("summary.json", "gap-execution.json", "gap-job-status.json")
        },
        "gateway_binding_sha256": hashlib.sha256((case / "gateway-binding.json").read_bytes()).hexdigest(),
        "limitations": [
            "one integration flight, not a new comparison cohort",
            "private raw logs are retained separately; hashes are provenance, not public re-verification",
            "safe-abort task mapping checked by contract test, not a second Gateway flight",
            "no learned-model or physical-flight value established",
        ],
    }
    (output / "gateway-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"]["gateway-evidence.json"] = hashlib.sha256(
        (output / "gateway-evidence.json").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.run.resolve(), args.output.resolve())
