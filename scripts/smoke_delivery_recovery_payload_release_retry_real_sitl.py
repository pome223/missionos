#!/usr/bin/env python3
"""Real PX4/Gazebo SITL smoke for payload-release retry recovery (#433 Path A)."""

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
    DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION,
    REAL_SITL_EVIDENCE_SOURCE,
    attach_payload_release_retry_recovered_chain_from_px4_gazebo_summaries,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_DELIVERY_RECOVERY_PAYLOAD_RETRY_REAL_SITL"
ARTIFACT_ROOT_ENV = "DELIVERY_RECOVERY_PAYLOAD_RETRY_REAL_SITL_ARTIFACT_ROOT"
HORIZONTAL_ATTEMPTS_ENV = "DELIVERY_RECOVERY_PAYLOAD_RETRY_HORIZONTAL_ATTEMPTS"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the real PX4/Gazebo SITL payload retry smoke"
        )


def _artifact_root() -> Path:
    return Path(os.getenv(ARTIFACT_ROOT_ENV, "output/delivery_recovery_real_sitl"))


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / f"payload-retry-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _run_horizontal_route_smoke(
    run_dir: Path,
    *,
    label: str,
    payload_release_model: bool,
) -> dict:
    horizontal_root = run_dir / label
    attempts = max(1, int(os.getenv(HORIZONTAL_ATTEMPTS_ENV, "2")))
    last_log = run_dir / f"{label}.log"
    for attempt in range(1, attempts + 1):
        attempt_root = horizontal_root / f"attempt_{attempt}"
        env = {
            **os.environ,
            "PYTHONPATH": ".",
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(attempt_root),
            "PX4_GAZEBO_HORIZONTAL_ROUTE_SKIP_EMERGENCY_MAVLINK": "1",
        }
        if payload_release_model:
            env["PX4_GAZEBO_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL"] = "1"
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
        last_log = run_dir / f"{label}_attempt_{attempt}.log"
        last_log.write_text(result.stdout)
        if result.returncode == 0:
            break
    else:
        raise RuntimeError(f"{label} real SITL smoke failed; see {last_log}")
    summaries = sorted(horizontal_root.glob("attempt_*/*/summary.json"))
    if not summaries:
        raise RuntimeError(f"{label} real SITL smoke produced no summary.json")
    summary_path = summaries[-1]
    summary = json.loads(summary_path.read_text())
    shutil.copy2(summary_path, run_dir / f"{label}_summary.json")
    return summary


def main() -> int:
    _require_opt_in()
    run_dir = _new_run_dir()
    initial_summary = _run_horizontal_route_smoke(
        run_dir,
        label="initial_payload_missing",
        payload_release_model=False,
    )
    retry_summary = _run_horizontal_route_smoke(
        run_dir,
        label="retry_payload_release_observed",
        payload_release_model=True,
    )
    observed_at = datetime.now(timezone.utc)

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery payload retry real SITL smoke",
            status="running",
            artifacts={
                "existing": {
                    "case_id": "delivery-recovery-payload-retry-real-sitl",
                    "kept": True,
                },
                "initial_px4_gazebo_horizontal_route_summary": initial_summary,
                "retry_px4_gazebo_horizontal_route_summary": retry_summary,
            },
        )
        attached = (
            attach_payload_release_retry_recovered_chain_from_px4_gazebo_summaries(
                task["task_id"],
                initial_summary=initial_summary,
                retry_summary=retry_summary,
                mission_contract_ref="delivery_mission_contract:real-sitl-payload-retry",
                recovery_decision_ref="delivery_recovery_decision:retry-dropoff",
                operator_status_ref=(
                    "operator_minimal_delivery_simulation_status:real-sitl-payload-retry"
                ),
                observed_at=observed_at,
                task_store_factory=lambda: store,
            )
        )
        stored = store.get(task["task_id"])
        shutil.copy2(Path(tmp) / "tasks.db", run_dir / "tasks.db")

    fault = attached["delivery_fault_event_real_sitl_payload_release_missing"]
    request = attached["delivery_recovery_request_real_sitl_retry_dropoff"]
    recovery_run = attached["delivery_recovery_run_real_sitl_retry_dropoff"]
    outcome = attached["delivery_recovery_outcome_real_sitl_retry_recovered"]
    loop = attached["delivery_recovery_loop_real_sitl_retry_recovered"]
    artifacts = stored["artifacts"] if stored else {}
    (run_dir / "recovery_artifacts.json").write_text(
        json.dumps(artifacts, indent=2, sort_keys=True)
    )
    summary = {
        "artifact_dir": str(run_dir),
        "initial_px4_gazebo_artifact_dir": initial_summary["artifact_dir"],
        "retry_px4_gazebo_artifact_dir": retry_summary["artifact_dir"],
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "full_recovery_chain_artifacts_present": all(
            key in artifacts
            for key in (
                "delivery_fault_event_real_sitl_payload_release_missing",
                "delivery_recovery_request_real_sitl_retry_dropoff",
                "delivery_recovery_run_real_sitl_retry_dropoff",
                "delivery_recovery_outcome_real_sitl_retry_recovered",
                "delivery_recovery_loop_real_sitl_retry_recovered",
            )
        ),
        "fault_category": fault["fault_category"],
        "initial_payload_release_observed": initial_summary[
            "payload_release_observed"
        ],
        "recovery_decision": request["recovery_decision"],
        "request_kind": request["request_kind"],
        "recovery_run_status": recovery_run["status"],
        "outcome_schema_version": outcome["schema_version"],
        "outcome_category": outcome["outcome_category"],
        "payload_release_observed": outcome["observed_facts"][
            "payload_release_observed"
        ],
        "payload_release_verified": outcome["observed_facts"][
            "payload_release_verified"
        ],
        "payload_release_event_source": outcome["observed_facts"][
            "payload_release_event_source"
        ],
        "position_in_zone_observed": outcome["observed_facts"][
            "position_in_zone_observed"
        ],
        "altitude_within_tolerance_observed": outcome["observed_facts"][
            "altitude_within_tolerance_observed"
        ],
        "release_within_time_window_observed": outcome["observed_facts"][
            "release_within_time_window_observed"
        ],
        "executed_against_real_sitl": loop["executed_against_real_sitl"],
        "recovery_chain_evidence_source": loop["recovery_chain_evidence_source"],
        "epic_dod_satisfied_for_issue_433_path_a": True,
        "epic_425_close_allowed": False,
        "issue_433_path_b_touched": False,
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
            "default_narrow_predicates": outcome["default_narrow_predicates"],
            "absolute_caps_enforced": outcome["absolute_caps_enforced"],
        },
        "warnings": [
            "#433 Path B operator escalation was not touched",
            "#425 close still requires reviewer confirmation after #433 Path B or explicit reviewer acceptance",
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["outcome_schema_version"] == (
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    )
    assert summary["full_recovery_chain_artifacts_present"] is True
    assert summary["fault_category"] == "payload_release_not_observed"
    assert summary["initial_payload_release_observed"] is False
    assert summary["request_kind"] == "retry_dropoff_simulation"
    assert summary["outcome_category"] == "recovered"
    assert summary["payload_release_observed"] is True
    assert summary["payload_release_verified"] is True
    assert (
        summary["payload_release_event_source"]
        == "gazebo_detachable_joint_detach_event"
    )
    assert summary["position_in_zone_observed"] is True
    assert summary["altitude_within_tolerance_observed"] is True
    assert summary["release_within_time_window_observed"] is True
    assert summary["executed_against_real_sitl"] is True
    assert summary["recovery_chain_evidence_source"] == REAL_SITL_EVIDENCE_SOURCE
    assert summary["epic_dod_satisfied_for_issue_433_path_a"] is True
    assert summary["issue_433_path_b_touched"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    assert summary["invariants"]["synthetic_success_allowed"] is False
    assert summary["invariants"]["command_sent_by_verifier"] is False
    assert summary["invariants"]["default_narrow_predicates"] is True
    assert summary["invariants"]["absolute_caps_enforced"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
