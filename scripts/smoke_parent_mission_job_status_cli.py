#!/usr/bin/env python3
"""Exercise parent-mission job-status through the real CLI and HTTP boundary."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
from urllib.parse import unquote

from missionos_core import (
    EvidenceOrigin,
    HardwareExecutionMode,
    PromotionComponentKind,
    PromotionEvidenceScope,
    PromotionGapRequirement,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    VirtualToRealPromotionPolicy,
    VirtualToRealPromotionValidationContext,
    validate_virtual_to_real_promotion_receipt,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
TASK_ID = "smoke:parent-mission-job-status"
TASK_PAYLOAD_ENV = "MISSIONOS_PARENT_MISSION_JOB_STATUS_TASK_PAYLOAD"


def _missing_promotion_receipt_validation() -> dict:
    policy = VirtualToRealPromotionPolicy(
        policy_id="policy:control-tower:missing-receipt:v1",
        policy_version="1",
        source_scope=HardwareExecutionMode.SIM,
        allowed_target_scopes=(HardwareExecutionMode.BENCH,),
        required_source_verification_basis=VerificationBasis.DETERMINISTIC,
        approval_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        required_component_kinds=(PromotionComponentKind.CONTROLLER_PROFILE,),
        required_gaps=(
            PromotionGapRequirement(
                gap_id="safe_stop",
                required_evidence_kind="safe_stop_receipt",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                evidence_scope=PromotionEvidenceScope.TARGET,
            ),
        ),
        required_target_evidence_kinds=("safe_stop_receipt",),
        required_rollback_condition_ids=("rollback:disable-profile",),
        required_disable_condition_ids=("disable:on-evidence-drift",),
        maximum_age_seconds=3600.0,
    )
    return validate_virtual_to_real_promotion_receipt(
        None,
        context=VirtualToRealPromotionValidationContext(
            expected_source_scope=HardwareExecutionMode.SIM,
            expected_target_scope=HardwareExecutionMode.BENCH,
            observed_source_outcome_evidence_ref="evidence:parent-stage",
            observed_source_outcome_claim_satisfied=True,
            observed_source_verification_basis=VerificationBasis.DETERMINISTIC,
            expected_target_executor_profile_sha256="d" * 64,
            expected_target_controller_profile_sha256="e" * 64,
            active_policy=policy,
            evidence_sources={},
            evaluated_at="2026-08-01T00:30:00+00:00",
        ),
    ).to_dict()


def _stage(index: int, stage_ref: str) -> dict:
    return {
        "stage_index": index,
        "stage_ref": stage_ref,
        "executor_ref": f"sim:{stage_ref}",
        "transition_authority": {
            "transition_status": "authorized",
            "dispatch_authority_present": True,
            "dispatch_authority_source": "preexisting_mission_approval",
            "prerequisite_stage_ref": None if index == 1 else "stage_1",
            "prerequisite_predicate_satisfied": None if index == 1 else True,
        },
        "runner_invoked": True,
        "predicate_evaluation": {
            "predicate_package_id": f"missionos.smoke.stage-{index}",
            "predicate_package_version": "1",
            "evidence_readiness": "ready",
            "outcome_claim_scope": f"bounded_sim_stage_{index}",
            "observation_content_sha256": str(index + 2) * 64,
            "evidence_origins": ["stored_artifact"],
        },
        "stage_result": {
            "child_contract_sha256": str(index) * 64,
            "predicate_status": "satisfied",
            "predicate_satisfied": True,
            "actual_verification_basis": "deterministic",
        },
    }


def _task_payload() -> dict:
    coordinator = {
        "schema_version": "missionos_parent_mission_run_record.v1",
        "parent_mission_id": TASK_ID,
        "parent_mission_sha256": "a" * 64,
        "approval_binding_sha256": "b" * 64,
        "stage_count": 2,
        "stage_records": [
            _stage(1, "stage_1"),
            _stage(2, "stage_2"),
        ],
        "stages_satisfied": 2,
        "unreached_stage_refs": [],
        "coordinator_status": "stages_satisfied",
        "blocking_reasons": [],
        "mission_completion_claimed": False,
        "mission_completion_status": "unverified",
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }
    return {
        "task_id": TASK_ID,
        "kind": "parent_mission_execution",
        "status": "completed",
        "artifacts": {
            "virtual_to_real_promotion_validation": (_missing_promotion_receipt_validation()),
            "missionos_parent_mission_run_record": {
                "schema_version": "missionos_issue164_live_parent_mission.v1",
                "parent_contract_frozen_at": "2026-07-31T00:00:00+00:00",
                "parent_mission_sha256": "a" * 64,
                "approval_binding_sha256": "b" * 64,
                "shared_target_descriptor_sha256": "c" * 64,
                "coordinator_record": coordinator,
                "mission_completion_claimed": False,
                "identity_continuity_claimed": False,
                "shared_world_claimed": False,
                "physical_execution_invoked": False,
            },
        },
    }


class _TaskHandler(BaseHTTPRequestHandler):
    task_payload = _task_payload()

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        task_id = str(self.task_payload.get("task_id") or "")
        if unquote(self.path) != f"/tasks/{task_id}":
            self.send_error(404)
            return
        payload = json.dumps(self.task_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    task_payload = _task_payload()
    supplied_path = os.environ.get(TASK_PAYLOAD_ENV, "").strip()
    if supplied_path:
        loaded = json.loads(Path(supplied_path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not str(loaded.get("task_id") or ""):
            raise SystemExit(f"{TASK_PAYLOAD_ENV} must contain one task mapping")
        task_payload = loaded
    task_id = str(task_payload["task_id"])
    _TaskHandler.task_payload = task_payload
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TaskHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway_url = f"http://127.0.0.1:{server.server_port}"
    pythonpath = os.pathsep.join(
        (
            str(ROOT_DIR / "packages/missionos-cli/src"),
            str(ROOT_DIR / "packages/missionos-core/src"),
            str(ROOT_DIR),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    try:
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "missionos_cli",
                "--gateway-url",
                gateway_url,
                "--timeout",
                "5",
                "job-status",
                "--task-id",
                task_id,
                "--timeline-limit",
                "0",
            ),
            cwd=ROOT_DIR,
            env={**os.environ, "PYTHONPATH": pythonpath},
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    parent_record = task_payload.get("artifacts", {}).get(
        "missionos_parent_mission_run_record",
        {},
    )
    coordinator = parent_record.get("coordinator_record", {})
    stage_count = coordinator.get("stage_count")
    stage_records = coordinator.get("stage_records")
    stage_records = stage_records if isinstance(stage_records, list) else []
    stage_lines = tuple(
        f"Stage {record.get('stage_index')}/{stage_count}: ref={record.get('stage_ref')}"
        for record in stage_records
        if isinstance(record, dict)
    )
    expected = (
        f"Stages Satisfied: {stage_count}/{stage_count}; parent mission completion remains unverified",
        *stage_lines,
        "claimed=False; status=unverified",
        "claims=identity_continuity,shared_world,physical_execution",
        "Physical AI Control Tower:",
        "Promotion: receipt=absent",
        "Safe stop: request=unknown; ack=unknown; effect=unknown",
        "Physical: deployment_authority=unknown; execution=False",
    )
    missing = [text for text in expected if text not in completed.stdout]
    result = {
        "schema_version": "missionos_parent_mission_job_status_smoke.v1",
        "cli_returncode": completed.returncode,
        "http_task_boundary_exercised": True,
        "parent_completion_claimed": False,
        "expected_lines_observed": not missing,
        "missing_expected_lines": missing,
        "stderr": completed.stderr.strip(),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if completed.returncode != 0 or missing:
        print(completed.stdout)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
