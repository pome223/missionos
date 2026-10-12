"""Offline reference selection is source binding, never model-written numbers."""

from copy import deepcopy
import json

import pytest

from src.runtime import starship_reference_ids as references
from src.runtime.starship_decision_evidence import build_reference_catalog
from src.runtime.starship_replanning import digest


def request():
    return {
        "request_id": "reference-probe-1",
        "time_s": 1597.45,
        "candidates": {
            "nominal": {
                "comparison_material": {
                    "return_time_s": 2280.7,
                    "waiting_at_burn": {
                        "elapsed_wait_s": 0.0,
                        "energy_remaining_kwh": 22.0,
                        "temperature_upper_k": 295.0,
                        "propellant_loss_upper_kg": 0.0,
                    },
                }
            },
            "next_orbit": {
                "comparison_material": {
                    "return_time_s": 7731.7,
                    "waiting_at_burn": {
                        "elapsed_wait_s": 5451.0,
                        "energy_remaining_kwh": 12.915,
                        "temperature_upper_k": 300.451,
                        "propellant_loss_upper_kg": 109.02,
                    },
                }
            },
        },
    }


def energy_ids(value):
    return [
        entry["reference_id"]
        for entry in references.catalog_for(value)["entries"]
        if entry["field_path"] == "/waiting_at_burn/energy_remaining_kwh"
    ]


def test_catalog_is_deterministic_decision_bound_and_json_roundtrippable():
    value = request()
    catalog = references.catalog_for(value)
    reordered = json.loads(json.dumps(value, sort_keys=True))
    assert catalog == references.catalog_for(reordered)
    assert catalog["schema"] == references.SCHEMA
    assert catalog["request_sha256"] == digest(value)
    assert len(catalog["entries"]) == 10
    assert all(len(entry["reference_id"]) == 35 for entry in catalog["entries"])
    assert len({entry["reference_id"] for entry in catalog["entries"]}) == 10
    assert [
        {key: entry[key] for key in ("candidate_id", "field_path", "value", "unit")}
        for entry in catalog["entries"]
    ] == sorted(
        build_reference_catalog(value), key=lambda item: (item["candidate_id"], item["field_path"])
    )


def test_ids_resolve_exact_values_without_claiming_model_numeric_assertions():
    value = request()
    original = deepcopy(value)
    audit = references.verify_reference_ids(value, energy_ids(value), ["nominal", "next_orbit"])
    assert audit["status"] == "valid"
    assert audit["errors"] == []
    assert {entry["candidate_id"]: entry["value"] for entry in audit["resolved_references"]} == {
        "nominal": 22.0,
        "next_orbit": 12.915,
    }
    assert audit["host_resolved_values"]
    for claim in (
        "model_numeric_values_verified",
        "free_prose_verified",
        "internal_reasoning_verified",
        "forecast_truth_verified",
        "affects_action_admission",
    ):
        assert audit[claim] is False
    audit["resolved_references"][0]["value"] = 999
    assert value == original


@pytest.mark.parametrize("field", ["request_id", "time_s", "cost", "metadata_unit"])
def test_old_decision_ids_cannot_be_reused_after_request_changes(field):
    before = request()
    ids = energy_ids(before)
    after = deepcopy(before)
    if field == "request_id":
        after["request_id"] = "reference-probe-2"
    elif field == "time_s":
        after["time_s"] += 0.2
    elif field == "cost":
        after["candidates"]["nominal"]["comparison_material"]["waiting_at_burn"][
            "energy_remaining_kwh"
        ] = 21.0
    else:
        after["display_unit"] = "Wh"
    assert not set(ids) & set(energy_ids(after))
    audit = references.verify_reference_ids(after, ids, ["nominal", "next_orbit"])
    assert audit["status"] == "invalid"
    assert audit["resolved_references"] == []
    assert "stale_reference_id" in audit["errors"]


@pytest.mark.parametrize(
    "field,replacement",
    [("candidate_id", "other"), ("field_path", "/other"), ("value", 23.0), ("unit", "Wh")],
)
def test_suffix_binds_every_reference_field_even_with_identical_request_hash(field, replacement):
    value = request()
    source = build_reference_catalog(value)[0]
    changed = {**source, field: replacement}
    assert references._identifier(digest(value), source) != references._identifier(
        digest(value), changed
    )


def test_catalog_tampering_is_not_trusted_for_resolution():
    value = request()
    catalog = references.catalog_for(value)
    entry = next(
        e
        for e in catalog["entries"]
        if e["candidate_id"] == "nominal"
        and e["field_path"] == "/waiting_at_burn/energy_remaining_kwh"
    )
    entry.update(value=999.0, unit="Wh", candidate_id="next_orbit")
    audit = references.verify_reference_ids(value, [entry["reference_id"]], ["nominal"])
    assert audit["status"] == "valid"
    assert audit["resolved_references"][0]["value"] == 22.0
    assert audit["resolved_references"][0]["unit"] == "kWh"
    assert audit["resolved_references"][0]["candidate_id"] == "nominal"


def test_unknown_current_decision_id_and_duplicate_are_rejected():
    value = request()
    ids = energy_ids(value)
    forged = "r_" + digest(value)[:12] + "_" + "0" * 20
    assert forged not in ids
    audit = references.verify_reference_ids(
        value, ids + [forged, ids[0]], ["nominal", "next_orbit"]
    )
    assert audit["status"] == "invalid"
    assert audit["errors"] == ["unknown_reference_id", "duplicate_reference_id"]
    assert len(audit["resolved_references"]) == 2


def test_one_valid_reference_is_required_for_each_compared_candidate():
    value = request()
    ids = energy_ids(value)
    first_name = references.catalog_for(value)["entries"][0]["candidate_id"]
    audit = references.verify_reference_ids(value, ids[:1], ["nominal", "next_orbit"])
    assert audit["errors"] == ["missing_compared_candidate_reference"]
    audit = references.verify_reference_ids(value, ids, [first_name])
    assert audit["errors"] == ["candidate_not_compared"]


@pytest.mark.parametrize("ids", [None, "r_fake", (), [], ["r_fake"] * 17])
def test_reference_list_is_nonempty_typed_and_bounded(ids):
    value = request()
    assert references.verify_reference_ids(value, ids, ["nominal"])["errors"] == [
        "reference_count_or_type"
    ]


@pytest.mark.parametrize(
    "reference_id", [None, True, {}, [], "r_fake", "r_" + "0" * 12 + "_" + "0" * 20 + "\n"]
)
def test_malformed_reference_id_is_not_interpreted(reference_id):
    audit = references.verify_reference_ids(request(), [reference_id], ["nominal"])
    assert audit["status"] == "invalid"
    assert "reference_id_shape" in audit["errors"]


@pytest.mark.parametrize(
    "compared", [None, (), [], ["nominal", "nominal"], ["future"], [True], [{}]]
)
def test_compared_candidates_are_unique_observed_and_typed(compared):
    value = request()
    assert references.verify_reference_ids(value, energy_ids(value), compared)["errors"] == [
        "compared_candidate_ids"
    ]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), object()])
def test_non_json_or_nonfinite_request_does_not_receive_ids(bad):
    value = request()
    value["bad"] = bad
    with pytest.raises(ValueError, match="invalid_reference_request"):
        references.catalog_for(value)
    assert references.verify_reference_ids(value, ["anything"], ["nominal"])["errors"] == [
        "invalid_reference_request"
    ]


@pytest.mark.parametrize("value", [None, [], {}, {"candidates": None}, {"candidates": []}])
def test_malformed_request_fails_closed(value):
    with pytest.raises(ValueError, match="invalid_reference_request"):
        references.catalog_for(value)
    assert references.verify_reference_ids(value, [], ["nominal"])["status"] == "invalid"


def test_unavailable_numeric_field_does_not_get_an_id():
    value = request()
    value["candidates"]["nominal"]["comparison_material"]["waiting_at_burn"][
        "energy_remaining_kwh"
    ] = True
    assert not any(
        e["candidate_id"] == "nominal"
        and e["field_path"] == "/waiting_at_burn/energy_remaining_kwh"
        for e in references.catalog_for(value)["entries"]
    )


def test_short_id_collision_fails_closed(monkeypatch):
    monkeypatch.setattr(references, "_identifier", lambda *_: "r_" + "0" * 12 + "_" + "0" * 20)
    with pytest.raises(ValueError, match="reference_id_collision"):
        references.catalog_for(request())
    assert references.verify_reference_ids(request(), ["anything"], ["nominal"])["errors"] == [
        "reference_id_collision"
    ]


def test_duplicate_source_field_fails_closed(monkeypatch):
    source = build_reference_catalog(request())[0]
    monkeypatch.setattr(references, "build_reference_catalog", lambda _: [source, source])
    with pytest.raises(ValueError, match="duplicate_catalog_reference"):
        references.catalog_for(request())


def test_same_visible_request_prefix_still_uses_full_request_hash(monkeypatch):
    real_digest = references._digest
    monkeypatch.setattr(references, "_digest", lambda value: "0" * 12 + real_digest(value)[12:])
    before, after = request(), request()
    after["request_id"] = "reference-probe-2"
    old_ids = energy_ids(before)
    assert (
        references.catalog_for(before)["request_sha256"][:12]
        == references.catalog_for(after)["request_sha256"][:12]
    )
    assert not set(old_ids) & set(energy_ids(after))
    audit = references.verify_reference_ids(after, old_ids, ["nominal", "next_orbit"])
    assert audit["status"] == "invalid"
    assert "unknown_reference_id" in audit["errors"]
    assert audit["resolved_references"] == []
