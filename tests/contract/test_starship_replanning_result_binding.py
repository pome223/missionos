"""Verification receipts identify inputs even for failed or malformed records."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from src.runtime import starship_replanning_verifier as verifier
from src.runtime import starship_sixdof_verifier as trajectory
from src.runtime.starship_replanning import contract


@pytest.fixture
def record_context(monkeypatch):
    """Isolate receipt identity from the separate detailed trajectory tests.

    This incomplete mission has no return command and no contact. Its saved
    record can be consistent without meeting the mission completion gate.
    """
    state = {"time_s": 1000.0}
    envelope = contract("fixture")
    sources = {"src/example.py": "a" * 64}
    study = {
        "run_id": "receipt-run-a",
        "case": "normal",
        "envelope": envelope,
        "source_sha256": sources,
        "profile": {},
        "launch": {
            "outcome": {"payload_released_count": 26, "orbit_gate_reached": True},
            "final_state": state,
        },
        "execution_origin": state,
        "candidates": {},
        "plan_revisions": [],
        "decisions": [],
        "forecast_count": 0,
        "pending_operations": [],
        "dispatch": None,
        "execution": {
            "samples": [state],
            "final_state": state,
            "outcome": {"contact_receipt": None},
        },
    }
    monkeypatch.setattr(trajectory, "verify_study", lambda *a, **k: {"passed": True})
    monkeypatch.setattr(trajectory, "_samples", lambda *a, **k: (state, state))
    monkeypatch.setattr(trajectory, "_same_state", lambda a, b: a == b)
    return study, {
        "expected_envelope": envelope,
        "expected_case": "normal",
        "expected_sources": sources,
        "expected_run_id": "receipt-run-a",
    }


def test_successful_record_check_binds_exact_study_and_expected_context(record_context):
    study, context = record_context
    result = verifier.verify(study, **context)
    assert result["passed"] is True
    assert result["case_accepted"] is False  # No return or contact was claimed.
    assert result["schema"] == "missionos.starship_m1_verification.v2"
    assert result["study_canonical_sha256"] == verifier.checksum(study)
    assert result["expected_case"] == "normal"
    assert result["expected_run_id"] == result["observed_run_id"] == "receipt-run-a"
    assert result["expected_sources_sha256"] == verifier.checksum(context["expected_sources"])
    assert result["expected_envelope_sha256"] == verifier.checksum(context["expected_envelope"])
    assert (
        result["verifier_source_sha256"] == sha256(Path(verifier.__file__).read_bytes()).hexdigest()
    )
    assert result["binding_complete"] is True
    assert result["binding_unavailable"] == []


def test_two_valid_records_cannot_share_a_verification_receipt(record_context):
    study, context = record_context
    first = verifier.verify(study, **context)
    other = deepcopy(study)
    other["run_id"] = "receipt-run-b"
    second = verifier.verify(other, **{**context, "expected_run_id": "receipt-run-b"})
    assert first["passed"] and second["passed"]
    assert first["study_canonical_sha256"] != second["study_canonical_sha256"]
    assert verifier.checksum(first) != verifier.checksum(second)
    assert first["verifier_source_sha256"] == second["verifier_source_sha256"]


def test_canonical_receipt_ignores_dictionary_order_but_not_record_values(record_context):
    study, context = record_context
    first = verifier.verify(study, **context)
    reordered = dict(reversed(list(study.items())))
    assert verifier.verify(reordered, **context) == first
    reordered["unrecognized_record_metadata"] = "different record"
    second = verifier.verify(reordered, **context)
    assert second["study_canonical_sha256"] != first["study_canonical_sha256"]


@pytest.mark.parametrize(
    "field", ["expected_case", "expected_sources", "expected_envelope", "expected_run_id"]
)
def test_failed_context_check_remains_bound_to_the_inspected_record(record_context, field):
    study, context = record_context
    changed = deepcopy(context)
    if field == "expected_sources":
        changed[field]["src/example.py"] = "b" * 64
    elif field == "expected_envelope":
        changed[field]["maximum_forecasts"] += 1
    else:
        changed[field] = "different"
    result = verifier.verify(study, **changed)
    assert result["passed"] is False
    assert result["study_canonical_sha256"] == verifier.checksum(study)
    assert result["binding_complete"] is True
    assert result["expected_case"] == changed["expected_case"]
    assert result["expected_sources_sha256"] == verifier.checksum(changed["expected_sources"])
    if field == "expected_run_id":
        assert "run_identity_binding" in result["issues"]


@pytest.mark.parametrize("malformed", [None, [], {"run_id": "broken"}])
def test_malformed_json_keeps_its_digest_without_a_successful_verdict(record_context, malformed):
    _, context = record_context
    result = verifier.verify(malformed, **context)
    assert result["passed"] is False
    assert result["case_accepted"] is False
    assert result["case_issues"] == ["invalid_record"]
    assert result["study_canonical_sha256"] == verifier.checksum(malformed)
    assert result["expected_case"] == "normal"
    assert result["independent_physics_validation"] is False
    assert result["verifier_source_sha256"]


def test_noncanonical_record_marks_digest_unavailable_instead_of_hashing_replacement(
    record_context,
):
    study, context = record_context
    study["invalid_number"] = float("nan")
    result = verifier.verify(study, **context)
    assert result["passed"] is False
    assert result["study_canonical_sha256"] is None
    assert "study_canonical_sha256" in result["binding_unavailable"]
    assert "study_canonical_sha256_unavailable" in result["issues"]
    assert result["binding_complete"] is False


def test_legacy_record_does_not_invent_a_run_identity(record_context):
    study, context = record_context
    del study["run_id"]
    del context["expected_run_id"]
    result = verifier.verify(study, **context)
    assert result["passed"] is True
    assert result["observed_run_id"] is None
    assert result["binding_complete"] is False
    assert result["binding_unavailable"] == ["observed_run_id"]


@pytest.mark.parametrize("fallback", [False, True])
def test_stale_event_response_is_not_classified_as_applied_fallback(
    record_context, monkeypatch, fallback
):
    from src.runtime import starship_replanning_events

    study, context = record_context
    study["case"] = context["expected_case"] = "event_normal"
    study["envelope"] = context["expected_envelope"] = contract("fixture", case="event_normal")
    study["decisions"] = [
        {
            "request": {
                "request_id": "receipt-run-a-0",
                "time_s": 1000.0,
                "deadline_s": 1035.0,
                "envelope_sha256": verifier.checksum(study["envelope"]),
                "allowed_actions": ["keep_plan"],
            },
            "response": None,
            "accepted_action": None,
            "later_time_s": 1002.0,
            "later_observation": {"state": {"time_s": 1002.0}},
            "request_disposition": "submitted",
            "interrupted_by_events": True,
            "fallback": fallback,
        }
    ]
    # The detailed event replay is tested separately. Here only the receipt's
    # distinction between interrupted context and fallback classification varies.
    monkeypatch.setattr(starship_replanning_events, "verify_events", lambda *a, **k: [])
    result = verifier.verify(study, **context)
    assert result["passed"] is (not fallback)
    assert ("fallback_classification" in result["issues"]) is fallback
