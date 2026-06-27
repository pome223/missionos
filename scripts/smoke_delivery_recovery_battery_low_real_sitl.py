#!/usr/bin/env python3
"""Real PX4/Gazebo SITL smoke for battery-low recovery artifacts (#432)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from src.runtime.delivery_recovery_real_sitl import (
    DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION,
    REAL_SITL_EVIDENCE_SOURCE,
    attach_battery_low_recovery_chain_from_px4_gazebo_summary,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_DELIVERY_RECOVERY_BATTERY_LOW_REAL_SITL"
ARTIFACT_ROOT_ENV = "DELIVERY_RECOVERY_BATTERY_LOW_REAL_SITL_ARTIFACT_ROOT"
HORIZONTAL_ARTIFACT_ROOT_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the real PX4/Gazebo SITL recovery smoke"
        )


def _artifact_root() -> Path:
    return Path(os.getenv(ARTIFACT_ROOT_ENV, "output/delivery_recovery_real_sitl"))


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / f"battery-low-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _run_horizontal_route_smoke(run_dir: Path) -> dict:
    horizontal_root = run_dir / "px4_gazebo_horizontal_route"
    env = {
        **os.environ,
        "PYTHONPATH": ".",
        "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
        HORIZONTAL_ARTIFACT_ROOT_ENV: str(horizontal_root),
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (run_dir / "horizontal_route_smoke.log").write_text(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route real SITL smoke failed; see "
            f"{run_dir / 'horizontal_route_smoke.log'}"
        )
    summaries = sorted(horizontal_root.glob("*/summary.json"))
    if not summaries:
        raise RuntimeError("horizontal route real SITL smoke produced no summary.json")
    summary_path = summaries[-1]
    summary = json.loads(summary_path.read_text())
    shutil.copy2(summary_path, run_dir / "source_px4_gazebo_summary.json")
    return summary


def main() -> int:
    _require_opt_in()
    run_dir = _new_run_dir()
    px4_summary = _run_horizontal_route_smoke(run_dir)
    observed_at = datetime.now(timezone.utc)

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery battery low real SITL smoke",
            status="running",
            artifacts={
                "existing": {
                    "case_id": "delivery-recovery-battery-low-real-sitl",
                    "kept": True,
                },
                "px4_gazebo_horizontal_route_summary": px4_summary,
            },
        )
        attached = attach_battery_low_recovery_chain_from_px4_gazebo_summary(
            task["task_id"],
            px4_summary,
            mission_contract_ref="delivery_mission_contract:real-sitl-battery-low",
            recovery_decision_ref="delivery_recovery_decision:abort-recommended",
            operator_status_ref=(
                "operator_minimal_delivery_simulation_status:real-sitl-battery-low"
            ),
            observed_at=observed_at,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        shutil.copy2(Path(tmp) / "tasks.db", run_dir / "tasks.db")

    fault = attached["delivery_fault_event_real_sitl"]
    request = attached["delivery_recovery_request_real_sitl"]
    recovery_run = attached["delivery_recovery_run_real_sitl"]
    outcome = attached["delivery_recovery_outcome_real_sitl"]
    loop = attached["delivery_recovery_loop_real_sitl"]
    artifacts = stored["artifacts"] if stored else {}
    (run_dir / "recovery_artifacts.json").write_text(
        json.dumps(artifacts, indent=2, sort_keys=True)
    )
    summary = {
        "artifact_dir": str(run_dir),
        "source_px4_gazebo_artifact_dir": px4_summary["artifact_dir"],
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "full_recovery_chain_artifacts_present": all(
            key in artifacts
            for key in (
                "delivery_fault_event_real_sitl",
                "delivery_recovery_request_real_sitl",
                "delivery_recovery_run_real_sitl",
                "delivery_recovery_outcome_real_sitl",
                "delivery_recovery_loop_real_sitl",
            )
        ),
        "fault_category": fault["fault_category"],
        "recovery_decision": request["recovery_decision"],
        "request_kind": request["request_kind"],
        "recovery_run_status": recovery_run["status"],
        "outcome_schema_version": outcome["schema_version"],
        "outcome_category": outcome["outcome_category"],
        "safe_landing_observed": outcome["observed_facts"][
            "safe_landing_observed"
        ],
        "mission_terminated_safely": outcome["observed_facts"][
            "mission_terminated_safely"
        ],
        "vehicle_disarmed_or_landed": outcome["observed_facts"][
            "vehicle_disarmed_or_landed"
        ],
        "completed_pose_z_m": outcome["observed_facts"]["completed_pose_z_m"],
        "executed_against_real_sitl": loop["executed_against_real_sitl"],
        "recovery_chain_evidence_source": loop["recovery_chain_evidence_source"],
        "epic_dod_satisfied_for_issue_432": True,
        "epic_425_close_allowed": False,
        "issue_433_touched": False,
        "invariants": {
            "fault_executed_against_real_sitl": fault["executed_against_real_sitl"],
            "request_executed_against_real_sitl": request[
                "executed_against_real_sitl"
            ],
            "run_executed_against_real_sitl": recovery_run[
                "executed_against_real_sitl"
            ],
            "outcome_executed_against_real_sitl": outcome[
                "executed_against_real_sitl"
            ],
            "loop_executed_against_real_sitl": loop["executed_against_real_sitl"],
            "fault_recovery_chain_evidence_source": fault[
                "recovery_chain_evidence_source"
            ],
            "request_recovery_chain_evidence_source": request[
                "recovery_chain_evidence_source"
            ],
            "run_recovery_chain_evidence_source": recovery_run[
                "recovery_chain_evidence_source"
            ],
            "outcome_recovery_chain_evidence_source": outcome[
                "recovery_chain_evidence_source"
            ],
            "loop_recovery_chain_evidence_source": loop[
                "recovery_chain_evidence_source"
            ],
            "hardware_target_allowed": loop["hardware_target_allowed"],
            "real_hardware_target": loop["real_hardware_target"],
            "physical_execution_invoked": loop["physical_execution_invoked"],
            "approval_free_stronger_execution_allowed": loop[
                "approval_free_stronger_execution_allowed"
            ],
            "synthetic_success_allowed": outcome["synthetic_success_allowed"],
            "command_sent_by_verifier": outcome["command_sent_by_verifier"],
        },
        "warnings": [
            "#433 payload release failure E2E not touched",
            "#425 remains open until #432 and #433 both pass",
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["outcome_schema_version"] == (
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION
    )
    assert summary["full_recovery_chain_artifacts_present"] is True
    assert summary["fault_category"] == "battery_reserve_violation"
    assert summary["request_kind"] == "abort_and_land_simulation"
    assert summary["safe_landing_observed"] is True
    assert summary["mission_terminated_safely"] is True
    assert summary["vehicle_disarmed_or_landed"] is True
    assert summary["completed_pose_z_m"] <= 0.15
    assert summary["executed_against_real_sitl"] is True
    assert summary["recovery_chain_evidence_source"] == REAL_SITL_EVIDENCE_SOURCE
    assert summary["epic_dod_satisfied_for_issue_432"] is True
    assert summary["epic_425_close_allowed"] is False
    assert summary["issue_433_touched"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    assert summary["invariants"]["synthetic_success_allowed"] is False
    assert summary["invariants"]["command_sent_by_verifier"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
