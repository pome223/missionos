#!/usr/bin/env python3
"""Emit public fault observations from a credential-free synthetic worker.

This one-way producer never reads stdin or receives model decisions. A separate
parent may request advisory judgments; every executed action stays with the
training-selected history rule. No provider client is imported here.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_dispenser_experiment import (  # noqa: E402
    FiniteModelSolver,
    PublicBudget,
    PublicHistory,
    digest,
    run_dispenser_experiment,
    run_policy_tape,
)
from src.runtime.starship_dispenser_verifier import verify_dispenser_experiment  # noqa: E402


MAX_FRAME_BYTES = 16 * 1024
_CREDENTIAL_NAME_PARTS = ("API_KEY", "ACCESS_KEY", "SECRET", "TOKEN", "CREDENTIAL")
_CLOUD_CONFIG_NAMES = frozenset(
    {"AWS_CONFIG_FILE", "CLOUDSDK_CONFIG", "AZURE_CONFIG_DIR", "GOOGLE_AUTH_TOKEN"}
)


class _WorkerArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("shadow_worker_invalid_arguments")


def provider_credentials_present() -> bool:
    """Reject credential-bearing environment names without reading their values.

    This is environment hygiene, not an operating-system sandbox or a scan of
    local credential stores. The parent still launches with an env allowlist.
    """
    return any(
        name.upper() in _CLOUD_CONFIG_NAMES
        or any(part in name.upper() for part in _CREDENTIAL_NAME_PARTS)
        for name in os.environ
    )


def _write(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, indent=2, allow_nan=False)
        target.write("\n")


def _emit(frame: dict) -> None:
    line = json.dumps(frame, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("shadow_worker_frame_too_large")
    sys.stdout.write(line)
    sys.stdout.flush()


def run_worker(output_dir: Path) -> None:
    if provider_credentials_present():
        raise ValueError("shadow_worker_credentials_rejected")
    output_dir.mkdir(parents=True, exist_ok=False)
    study = run_dispenser_experiment(approved=True)
    _write(output_dir / "study.json", study)
    study = json.loads((output_dir / "study.json").read_text(encoding="utf-8"))
    verification = verify_dispenser_experiment(study)
    _write(output_dir / "verification.json", verification)
    if verification.get("verified") is not True:
        raise ValueError("shadow_worker_baseline_verification_failed")

    baseline_hash = digest(study)
    config = study["config"]
    parameters = study["tuning"]["history_rule"]["selected_parameters"]
    worlds = {world["world_id"]: world for world in study["worlds"]}
    expected_runs = [
        run
        for run in study["policy_runs"]
        if run["split"] == "eval" and run["policy"] == "history_rule"
    ]
    if len(expected_runs) != 60:
        raise ValueError("shadow_worker_evaluation_count_mismatch")
    solver = FiniteModelSolver(config)
    execution_runs = []
    emitted_count = 0
    for expected in expected_runs:
        case_ref = sha256(expected["world_id"].encode("utf-8")).hexdigest()
        emitted = False

        def observe(history: PublicHistory, budget: PublicBudget, step_index: int) -> None:
            nonlocal emitted, emitted_count
            if emitted or not any(result.success is False for result in history.retry_results):
                return
            frame = {
                "kind": "fault_observation",
                "case_ref": case_ref,
                "step_index": step_index,
                "public_history": {
                    "observations": [asdict(item) for item in history.observations],
                    "retry_results": [asdict(item) for item in history.retry_results],
                },
                "public_budget": asdict(budget),
            }
            _emit(frame)
            emitted = True
            emitted_count += 1

        rerun = run_policy_tape(
            config,
            worlds[expected["world_id"]],
            "history_rule",
            parameters,
            solver,
            approved=True,
            observation_callback=observe,
        )
        if digest(rerun) != digest(expected):
            raise ValueError("shadow_worker_baseline_run_changed")
        execution_runs.append(rerun)

    if digest(study) != baseline_hash:
        raise ValueError("shadow_worker_baseline_study_changed")
    _write(output_dir / "execution-runs.json", execution_runs)
    saved_runs = json.loads((output_dir / "execution-runs.json").read_text(encoding="utf-8"))
    if digest(saved_runs) != digest(expected_runs):
        raise ValueError("shadow_worker_saved_execution_changed")
    if provider_credentials_present():
        raise ValueError("shadow_worker_credentials_rejected")
    _emit(
        {
            "kind": "complete",
            "worker_pid": os.getpid(),
            "provider_credentials_present": False,
            "emitted_count": emitted_count,
            "evaluation_count": len(execution_runs),
        }
    )


def main() -> int:
    # This internal JSONL producer keeps even CLI rejection output fixed and
    # on stderr; caller-supplied argument text is never echoed.
    parser = _WorkerArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approve-synthetic", action="store_true")
    try:
        args = parser.parse_args()
    except ValueError:
        print("shadow_worker_invalid_arguments", file=sys.stderr)
        return 2
    if not args.approve_synthetic:
        print("shadow_worker_synthetic_approval_required", file=sys.stderr)
        return 2
    try:
        run_worker(args.output_dir)
    except Exception:
        # Exceptions may include local paths or caller-controlled data.
        print("shadow_worker_failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
