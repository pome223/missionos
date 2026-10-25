"""New exact integration envelope; old verifier API/schema stay unchanged."""
from copy import deepcopy

import pytest

from src.runtime import starship_restored_verifier as verifier
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
from tests.contract import test_starship_restored_verifier as old
from tests.contract.starship_port_integration_fixtures import integration_receipt


@pytest.fixture(autouse=True)
def forbid_integration_and_external_calls(monkeypatch):
    old.no_integration_or_external_calls.__wrapped__(monkeypatch)


@pytest.fixture
def valid_negative(monkeypatch):
    return old.valid_negative.__wrapped__(monkeypatch)


def integrated(values):
    original, profile, expected = values
    record, expected = deepcopy(original), deepcopy(expected)
    record["schema"] = verifier.INTEGRATED_SCHEMA
    record["execution"]["schema"] = verifier.INTEGRATED_EXECUTION_SCHEMA
    # Unclosed negatives do not have an origin that would need re-encoding.
    expected["expected_source_sha256"]["src/runtime/starship_restored_integration.py"] = "4"*64
    record["execution"]["source_sha256"] = deepcopy(expected["expected_source_sha256"])
    record["execution"]["integration_policy"] = RestoredIntegrationPolicy(2).record()
    record["numerical_integration"] = integration_receipt(2)
    expected["expected_integration_policy"] = RestoredIntegrationPolicy(2)
    return record, profile, expected


def test_old_default_wrapper_and_result_shape_remain_exact(valid_negative):
    record, profile, expected = valid_negative
    default = verifier.verify_restored_mission(record, profile, **expected)
    explicit = verifier.verify_restored_mission(record, profile, **expected, expected_integration_policy=None)
    assert default == explicit and default["passed"]
    assert default["schema"] == "missionos.starship_restored_mission_verification.v1"
    assert "integration_policy" not in default and "integration_execution_checked" not in default


def test_new_typed_scope_accepts_valid_physical_negative_without_fabricating_return(valid_negative):
    record, profile, expected = integrated(valid_negative)
    checked = verifier.verify_restored_mission(record, profile, **expected)
    assert checked["passed"], checked
    assert checked["schema"] == "missionos.starship_restored_mission_verification.v2"
    assert checked["integration_execution_checked"] and not checked["physical_success"]
    assert record["return"] is None and not checked["qualification_refreshed"]


@pytest.mark.parametrize("mutation", ["schema", "execution_schema", "missing_receipt", "wrong_policy", "untyped", "missing_source",
    "active", "failure", "calls_bool", "calls_zero", "substeps", "recompute_bool", "recompute", "cadence", "fuel", "contact",
    "models_bool", "models", "extra", "grant", "old_request_scope", "reservation"])
def test_new_policy_never_admits_changed_flags_receipt_actor_or_old_envelope(valid_negative, mutation):
    record, profile, expected = integrated(valid_negative)
    receipt, trace = record["numerical_integration"], record["numerical_integration"]["trace"]
    if mutation == "schema":
        record["schema"] = verifier.SCHEMA
    elif mutation == "execution_schema":
        record["execution"]["schema"] = verifier.EXECUTION_SCHEMA
    elif mutation == "missing_receipt":
        record.pop("numerical_integration")
    elif mutation == "wrong_policy":
        expected["expected_integration_policy"] = RestoredIntegrationPolicy(4)
    elif mutation == "untyped":
        expected["expected_integration_policy"] = {"subdivisions": 2, "passed": True}
    elif mutation == "missing_source":
        expected["expected_source_sha256"].pop("src/runtime/starship_restored_integration.py")
    elif mutation == "active":
        receipt["status"] = "active"
    elif mutation == "failure":
        receipt["failure_class"] = "ValueError"
    elif mutation == "calls_bool":
        trace["rk4_calls"] = True
    elif mutation == "calls_zero":
        trace.update(rk4_calls=0, rk4_substeps=0)
    elif mutation == "substeps":
        trace["rk4_substeps"] += 1
    elif mutation == "recompute_bool":
        trace["commands_recomputed_inside_rk4"] = False
    elif mutation == "recompute":
        trace["commands_recomputed_inside_rk4"] = 1
    elif mutation in ("cadence", "fuel", "contact"):
        trace[{"cadence": "outer_control_cadence_changed", "fuel": "fuel_event_search_changed", "contact": "contact_search_changed"}[mutation]] = True
    elif mutation == "models_bool":
        receipt["hosted_model_calls"] = False
    elif mutation == "models":
        receipt["hosted_model_calls"] = 1
    elif mutation == "extra":
        receipt["qualified"] = True
    elif mutation == "grant":
        receipt["mission_authority_granted"] = True
    elif mutation == "old_request_scope":
        expected["expected_integration_policy"] = None
    else:
        record["return"] = {"reservation_changes": [{"approved": True}]}
    assert not verifier.verify_restored_mission(record, profile, **expected)["passed"]
