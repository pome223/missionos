"""Decision-time citation provenance, independently of mission/action success."""

from copy import deepcopy
import json

import pytest

from src.runtime.starship_decision_evidence import (
    MAX_REFERENCES,
    SCHEMA,
    build_reference_catalog,
    verify_numeric_evidence,
)
from src.runtime.starship_replanning import digest


def request_and_evidence():
    def material(wait, energy):
        return {
            "return_time_s": 2280.7 + wait,
            "waiting_at_burn": {
                "elapsed_wait_s": wait,
                "energy_remaining_kwh": energy,
                "temperature_upper_k": 295.0 + wait * 0.001,
                "propellant_loss_upper_kg": wait * 0.02,
                "valid_until_s": 8280.7,
            },
            "sampled_contact_ranges": {"time_s": [4900.0 + wait, 4930.0 + wait]},
            "reserve_after_loss_kg": [62000.0, 62100.0],
            "area_service_estimates": {
                "east": {
                    "service_delay_after_contact_s": [450.0, 1050.0],
                    "estimated_completion_time_s": [5350.0 + wait, 5980.0 + wait],
                }
            },
        }

    request = {
        "request_id": "decision-7",
        "time_s": 1597.45,
        "candidates": {
            "nominal": {"comparison_material": material(0.0, 22.0)},
            "next_orbit": {"comparison_material": material(5451.0, 12.915)},
        },
    }
    evidence = {
        "schema": SCHEMA,
        "request_sha256": digest(request),
        "references": [
            {
                "candidate_id": name,
                "field_path": "/waiting_at_burn/energy_remaining_kwh",
                "value": energy,
                "unit": "kWh",
            }
            for name, energy in (("nominal", 22.0), ("next_orbit", 12.915))
        ],
    }
    return request, evidence


def test_exact_request_time_values_are_verified_without_claiming_prose_or_action():
    request, evidence = request_and_evidence()
    original = deepcopy((request, evidence))
    audit = verify_numeric_evidence(request, evidence, ["nominal", "next_orbit"])
    assert audit["status"] == "valid"
    assert audit["errors"] == []
    assert audit["verified_references"] == evidence["references"]
    assert audit["request_sha256"] == digest(request)
    assert audit["referenced_candidate_ids"] == ["next_orbit", "nominal"]
    assert not audit["free_prose_verified"]
    assert not audit["internal_reasoning_verified"]
    assert not audit["forecast_truth_verified"]
    assert not audit["affects_action_admission"]
    audit["verified_references"][0]["value"] = 0
    assert (request, evidence) == original


def test_json_roundtrip_and_integer_equivalent_preserve_exact_numeric_value():
    request, evidence = request_and_evidence()
    evidence["references"][0]["value"] = 22
    assert (
        verify_numeric_evidence(*json.loads(json.dumps([request, evidence])))["status"] == "valid"
    )


@pytest.mark.parametrize(
    "value", [12.9, True, None, "12.915", float("nan"), float("inf"), 10**1000]
)
def test_rounding_nonfinite_bool_and_non_numeric_values_are_not_evidence(value):
    request, evidence = request_and_evidence()
    evidence["references"][1]["value"] = value
    audit = verify_numeric_evidence(request, evidence, ["nominal", "next_orbit"])
    assert audit["status"] == "invalid"
    assert "value_mismatch" in audit["errors"]
    assert "missing_compared_candidate_reference" in audit["errors"]
    assert audit["verified_references"] == evidence["references"][:1]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("candidate_id", "unobserved_future", "unknown_candidate"),
        ("candidate_id", {}, "unknown_candidate"),
        ("field_path", "/future/actual_contact/time_s", "unknown_field_or_unavailable_value"),
        ("field_path", {}, "unknown_field_or_unavailable_value"),
        ("field_path", "/sampled_contact_ranges/time_s/2", "unknown_field_or_unavailable_value"),
        ("unit", "Wh", "unit_mismatch"),
        ("unit", None, "unit_mismatch"),
    ],
)
def test_reference_fields_are_allowlisted(field, value, reason):
    request, evidence = request_and_evidence()
    evidence["references"][0][field] = value
    audit = verify_numeric_evidence(request, evidence)
    assert audit["status"] == "invalid"
    assert reason in audit["errors"]


@pytest.mark.parametrize("change", ["revision", "time", "cost", "candidate_list"])
def test_evidence_cannot_be_reused_on_later_or_different_request(change):
    request, evidence = request_and_evidence()
    if change == "revision":
        request["request_id"] = "decision-8"
    elif change == "time":
        request["time_s"] += 0.2
    elif change == "cost":
        request["candidates"]["next_orbit"]["comparison_material"]["waiting_at_burn"][
            "energy_remaining_kwh"
        ] = 12.91
    else:
        request["candidates"]["third"] = deepcopy(request["candidates"]["nominal"])
    audit = verify_numeric_evidence(request, evidence)
    assert audit["status"] == "invalid"
    assert audit["errors"] == ["request_hash_mismatch"]
    assert audit["verified_references"] == []


@pytest.mark.parametrize(
    "kind", ["wrong_schema", "extra_top", "extra_ref", "missing_ref_field", "duplicate"]
)
def test_schema_and_reference_structure_are_strict(kind):
    request, evidence = request_and_evidence()
    reasons = {
        "wrong_schema": "evidence_schema",
        "extra_top": "evidence_shape",
        "extra_ref": "reference_shape",
        "missing_ref_field": "reference_shape",
        "duplicate": "duplicate_reference",
    }
    if kind == "wrong_schema":
        evidence["schema"] += ".changed"
    elif kind == "extra_top":
        evidence["source"] = "future"
    elif kind == "extra_ref":
        evidence["references"][0]["tolerance"] = 1.0
    elif kind == "missing_ref_field":
        evidence["references"][0].pop("unit")
    else:
        evidence["references"].append(deepcopy(evidence["references"][0]))
    audit = verify_numeric_evidence(request, evidence)
    assert audit["status"] == "invalid"
    assert reasons[kind] in audit["errors"]


@pytest.mark.parametrize("refs", [[], None, {}, [None] * (MAX_REFERENCES + 1)])
def test_reference_collection_is_bounded_and_nonempty(refs):
    request, evidence = request_and_evidence()
    evidence["references"] = refs
    assert verify_numeric_evidence(request, evidence)["errors"] == ["reference_count_or_type"]


@pytest.mark.parametrize("ids", [[], (), ["nominal", "nominal"], ["unknown"], [{}]])
def test_compared_ids_are_typed_unique_and_observed(ids):
    request, evidence = request_and_evidence()
    assert verify_numeric_evidence(request, evidence, ids)["errors"] == ["compared_candidate_ids"]


def test_all_compared_candidates_need_at_least_one_valid_numeric_reference():
    request, evidence = request_and_evidence()
    evidence["references"] = evidence["references"][:1]
    audit = verify_numeric_evidence(request, evidence, ["nominal", "next_orbit"])
    assert audit["errors"] == ["missing_compared_candidate_reference"]
    request, evidence = request_and_evidence()
    audit = verify_numeric_evidence(request, evidence, ["nominal"])
    assert audit["errors"] == ["candidate_not_compared"]


def test_absent_legacy_evidence_is_not_manufactured():
    request, _ = request_and_evidence()
    audit = verify_numeric_evidence(request, None)
    assert audit["status"] == "not_present"
    assert audit["errors"] == []
    assert audit["verified_references"] == []


def test_catalog_ignores_boolean_nonfinite_extra_and_unavailable_material():
    request, _ = request_and_evidence()
    material = request["candidates"]["nominal"]["comparison_material"]
    material["future_truth"] = 8000
    material["waiting_at_burn"]["energy_remaining_kwh"] = True
    material["waiting_at_burn"]["temperature_upper_k"] = float("nan")
    material["area_service_estimates"]["east"]["estimated_completion_time_s"] = None
    request["candidates"]["no_material"] = {}
    catalog = build_reference_catalog(request)
    paths = {ref["field_path"] for ref in catalog if ref["candidate_id"] == "nominal"}
    assert "/waiting_at_burn/energy_remaining_kwh" not in paths
    assert "/waiting_at_burn/temperature_upper_k" not in paths
    assert "/area_service_estimates/east/estimated_completion_time_s/0" not in paths
    assert not any("future_truth" in ref["field_path"] for ref in catalog)
    assert not any(ref["candidate_id"] == "no_material" for ref in catalog)


def test_interval_endpoints_and_escaped_area_names_resolve_only_existing_values():
    request, _ = request_and_evidence()
    areas = request["candidates"]["nominal"]["comparison_material"]["area_service_estimates"]
    areas["region/a~b"] = areas.pop("east")
    refs = [
        ref
        for ref in build_reference_catalog(request)
        if ref["candidate_id"] == "nominal" and "region~1a~0b" in ref["field_path"]
    ]
    assert len(refs) == 4
    evidence = {"schema": SCHEMA, "request_sha256": digest(request), "references": refs}
    assert verify_numeric_evidence(request, evidence, ["nominal"])["status"] == "valid"


def test_malformed_request_returns_finite_diagnostic_not_exception():
    _, evidence = request_and_evidence()
    for request in (None, [], {"bad": float("nan")}, {"bad": object()}):
        assert verify_numeric_evidence(request, evidence)["errors"] == ["invalid_request"]
