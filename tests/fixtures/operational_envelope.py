from datetime import datetime, timezone
from typing import Any

from src.runtime.operational_envelope import build_operational_envelope


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def build_source_run(index: int) -> dict[str, Any]:
    return {
        "schema_version": "mission_designer_wind_drift_behavior_audit.v1",
        "audit_id": f"wind_form1_fixture:run_{index}",
        "causal_form": "Form 1a",
        "condition_kind": "source_bound_wind_drift",
        "form1_claim_supported": True,
        "progress_counted": True,
        "source_bound": True,
        "mission_contract_ref": "mission_contract:operational_envelope_fixture",
        "task_graph_ref": "task_graph:operational_envelope_fixture",
        "source_backend_type": "px4_gazebo",
        "backend_context": {
            "backend_type": "px4_gazebo",
            "image_version": "px4-gazebo-fixture@sha256:test",
            "sim_version": "gz-sim-test",
            "sdf_hash": "sdf_hash_test",
            "applicator_chain_refs": ["simulator_condition_application:wind"],
            "verifier_version": "wind_drift_verifier.v1",
            "audit_script_version": "test_operational_envelope.v1",
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


def build_ready_operational_envelope() -> dict[str, Any]:
    return build_operational_envelope(
        source_runs=[build_source_run(index) for index in range(10)],
        now=NOW,
    )
