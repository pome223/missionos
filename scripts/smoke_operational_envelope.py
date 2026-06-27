#!/usr/bin/env python3
"""Runtime smoke for operational_envelope.v1 parameter transfer boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from src.runtime.operational_envelope import (
    build_operational_envelope,
    operational_envelope_ready,
)


def _source_run(index: int) -> dict:
    return {
        "schema_version": "mission_designer_wind_drift_behavior_audit.v1",
        "audit_id": f"wind_form1_smoke:run_{index}",
        "causal_form": "Form 1a",
        "condition_kind": "source_bound_wind_drift",
        "form1_claim_supported": True,
        "progress_counted": True,
        "source_bound": True,
        "mission_contract_ref": "mission_contract:operational_envelope_smoke",
        "task_graph_ref": "task_graph:operational_envelope_smoke",
        "source_backend_type": "px4_gazebo",
        "backend_context": {
            "backend_type": "px4_gazebo",
            "image_version": "px4-gazebo-smoke@sha256:test",
            "sim_version": "gz-sim-test",
            "sdf_hash": "sdf_hash_test",
            "applicator_chain_refs": ["simulator_condition_application:wind"],
            "verifier_version": "wind_drift_verifier.v1",
            "audit_script_version": "smoke_operational_envelope.v1",
        },
        "parameter_observations": [
            {
                "parameter": "wind_speed_mps",
                "value": 3.0 + index * 0.2,
                "unit": "m/s",
            }
        ],
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def main() -> int:
    envelope = build_operational_envelope(
        source_runs=[_source_run(index) for index in range(10)],
        now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    if not operational_envelope_ready(envelope):
        raise RuntimeError("operational envelope did not reach parameter-ready state")
    if envelope["causal_verification_transferred"] is not False:
        raise RuntimeError("operational envelope transferred causal verification")
    if envelope["physical_form1_required"] is not True:
        raise RuntimeError("operational envelope did not require physical Form 1")
    if envelope["progress_counted"] is not False:
        raise RuntimeError("operational envelope must remain Form 0b")
    output_dir = Path("output/mission_designer_behavior_delta_audits")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "operational_envelope_smoke.json"
    output_path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
