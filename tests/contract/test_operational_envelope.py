from src.runtime.operational_envelope import (
    build_physical_run_operational_envelope_consumption,
    operational_envelope_ready,
)
from tests.fixtures.operational_envelope import (
    NOW,
    build_ready_operational_envelope,
)


AUTHORITY_FIELDS = (
    "physical_backend_execution_allowed",
    "physical_execution_invoked",
    "hardware_target_allowed",
    "delivery_completion_claimed",
)


def test_operational_envelope_transfers_parameter_knowledge_only() -> None:
    envelope = build_ready_operational_envelope()

    assert operational_envelope_ready(envelope) is True
    assert envelope["audit_status"] == "parameter_knowledge_ready"
    assert envelope["envelope_status"] == "active"
    assert envelope["transfer_scope"] == "parameter_knowledge_only"
    assert envelope["accepted_sim_run_count"] == 10
    assert envelope["rejected_sim_run_count"] == 0
    wind_bounds = envelope["accepted_parameter_bounds"]["wind_speed_mps"]
    assert wind_bounds["min_value"] == 3.0
    assert wind_bounds["max_value"] == 4.8
    assert wind_bounds["unit"] == "m/s"
    assert wind_bounds["sample_count"] == 10
    assert wind_bounds["source_run_count"] == 10
    assert envelope["causal_verification_transferred"] is False
    assert envelope["physical_form1_required"] is True
    assert envelope["progress_counted"] is False
    assert envelope["safety_boundary"]["dispatch_authority_created"] is False
    for field in AUTHORITY_FIELDS:
        assert envelope[field] is False


def test_physical_consumption_requires_matching_context_and_new_form1() -> None:
    envelope = build_ready_operational_envelope()
    active = build_physical_run_operational_envelope_consumption(
        envelope=envelope,
        physical_run_ref="physical_run:matching_context",
        backend_context=envelope["backend_context"],
        now=NOW,
    )
    changed_context = {
        **envelope["backend_context"],
        "image_version": "px4-gazebo-fixture@sha256:changed",
    }
    blocked = build_physical_run_operational_envelope_consumption(
        envelope=envelope,
        physical_run_ref="physical_run:changed_context",
        backend_context=changed_context,
        now=NOW,
    )

    assert active["consumption_status"] == "parameter_knowledge_consumed"
    assert active["parameter_knowledge_consumed"] is True
    assert active["causal_verification_transferred"] is False
    assert active["physical_form1_required"] is True
    assert active["dispatch_authority_created"] is False
    assert blocked["consumption_status"] == "blocked"
    assert blocked["parameter_knowledge_consumed"] is False
    assert blocked["envelope_status_at_run"] == (
        "expired_due_to_image_version_change"
    )
    assert "expired_due_to_image_version_change" in blocked["blocked_reasons"]
    for artifact in (active, blocked):
        for field in AUTHORITY_FIELDS:
            assert artifact[field] is False
