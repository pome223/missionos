"""Invented saved/current records and independent pure checks; no flight/AI."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from src.runtime import starship_final_qualification as q


def identity():
    return q.make_identity(source_sha256={"src/runtime/plant.py": "a"*64},
        backend={"python": "3.11-fixture", "platform": "fixture", "numpy": "2.4.6", "scipy": "1.17.1",
            "executable_sha256": "b"*64}, profile_sha256="c"*64, integration_policy_sha256="d"*64)


def domain(fault="nominal"):
    return {"ascent_condition": "O0", "retained_count": 13, "released_count": 13,
        "return_candidate_id": "nominal", "fault_family": fault,
        "plant_capability_factor": .8 if fault == "return_propulsion_80" else 1.,
        "sensor_gain": .8 if fault == "return_sensor_gain_80" else 1.,
        "controller_capability_factor": .8 if fault == "return_propulsion_80" else 1.}


def point_data():
    engine = {"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": True}
    state = {"time_s": 2000., "r_eci_m": [6500000., 0., 0.], "v_eci_mps": [0., 7000., 0.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 90000.,
        "engine_states": [deepcopy(engine)], "flap_angles_rad": [0.]}
    return {"state": state, "vehicle": {"engines": [{"name": "invented_engine", "max_thrust_n": 1000000.}],
            "aero_panels": [{"name": "invented_panel"}]},
        "inventory": {"total_count": 26, "retained_count": 13, "released_count": 13,
            "release_receipts": [{"id": i} for i in range(13)]},
        "actuator_history": [{"time_s": t, "engine_states": [deepcopy(engine)], "flap_angles_rad": [0.]}
            for t in (1999., 2000.)]}


def saved_record(*, fault="nominal", speed=4.4, **flags):
    return {"schema": "invented_qualification_record.v1", "identity": identity(), "domain": domain(fault),
        "point_data": point_data(), "contact": {"speed_mps": speed, "tilt_deg": 2.,
            "body_rate_rad_s": .001, "propellant_kg": 55000.},
        "record_checks_passed": True, "actual_state_continuity_verified": True,
        "controller_plant_separation_verified": True, "prediction_only": False, **flags}


def raw(value):
    return q.canonical_bytes(value)


def pointers():
    return {key: "/point_data/"+key for key in ("state", "vehicle", "inventory", "actuator_history")}


def checker(record_raw, *, expected_identity, expected_cell):
    value = json.loads(record_raw)
    if value["identity"] != expected_identity or value["domain"] != expected_cell["domain"]:
        raise ValueError("independent_fixture_identity_or_domain")
    return {"schema": q.CHECK_SCHEMA, "record_sha256": sha256(record_raw).hexdigest(),
        "identity_sha256": q.digest(expected_identity), "cell_id": expected_cell["cell_id"],
        "domain": value["domain"], "point_data": value["point_data"], "point_pointers": pointers(),
        **{key: value[key] for key in ("record_checks_passed", "actual_state_continuity_verified",
            "controller_plant_separation_verified", "prediction_only", "contact")},
        "new_flights_during_check": 0, "new_model_calls_during_check": 0}


def observer(record_raw, *, expected_identity):
    value = json.loads(record_raw)
    if value["identity"] != expected_identity:
        raise ValueError("independent_fixture_current_identity")
    return {"schema": q.OBSERVATION_SCHEMA, "record_sha256": sha256(record_raw).hexdigest(),
        "identity_sha256": q.digest(expected_identity), "domain": value["domain"],
        "point_data": value["point_data"], "point_pointers": pointers()}


def build(*values):
    values = values or (saved_record(),)
    cells = [{"cell_id": f"cell-{i}", "domain": value["domain"]} for i, value in enumerate(values)]
    kwargs = {"expected_identity": identity(), "required_cells": cells,
        "records_raw": {cell["cell_id"]: raw(value) for cell, value in zip(cells, values)}, "verify_record": checker}
    return q.build_certificate(**kwargs), kwargs


def match(certificate, kwargs, value=None, **override):
    return q.admit_point(certificate, **kwargs, current_record_raw=raw(value or saved_record()),
        observe_current=override.get("observe_current", observer))


def test_exact_finite_point_has_raw_record_and_full_preimages_without_execution_authority():
    cert, kwargs = build()
    result = match(cert, kwargs)
    assert cert["qualification_complete"] and result["point_domain_evidence_matched"]
    assert result["matched_cell_ids"] == ["cell-0"]
    assert cert["cells"][0]["point"] == q.make_point(point_data())
    for value in (cert, result):
        assert value["qualification_scope"] == "ship_return_exact_model_points_only"
        assert value["dispatch_authorized"] is value["physical_execution_invoked"] is False
        assert value["runtime_execution_inferred_from_artifact"] is False
        assert value["continuous_uncertainty_envelope_verified"] is False
    assert cert["old39_certificate_reused"] is result["old39_certificate_admitted"] is False


@pytest.mark.parametrize("fault", ["nominal", "return_propulsion_80", "return_sensor_gain_80"])
def test_registered_fault_domains_are_distinct(fault):
    value = saved_record(fault=fault)
    cert, kwargs = build(value)
    assert match(cert, kwargs, value)["point_domain_evidence_matched"]
    other = saved_record(fault="return_sensor_gain_80" if fault != "return_sensor_gain_80" else "nominal")
    assert not match(cert, kwargs, other)["point_domain_evidence_matched"]


@pytest.mark.parametrize("mutation", ["position", "time", "vehicle", "receipts", "actuator_history", "candidate", "controller_factor"])
def test_unsampled_or_reassigned_point_is_not_interpolated_or_promoted(mutation):
    cert, kwargs = build()
    value = saved_record()
    if mutation == "position":
        value["point_data"]["state"]["r_eci_m"][0] += .01
    elif mutation == "time":
        value["point_data"]["state"]["time_s"] += .001
        value["point_data"]["actuator_history"][-1]["time_s"] += .001
    elif mutation == "vehicle":
        value["point_data"]["vehicle"]["engines"][0]["max_thrust_n"] -= 1.
    elif mutation == "receipts":
        value["point_data"]["inventory"]["release_receipts"][0]["id"] = 100
    elif mutation == "actuator_history":
        value["point_data"]["actuator_history"][0]["engine_states"][0]["throttle"] = .1
    elif mutation == "candidate":
        value["domain"]["return_candidate_id"] = "next_orbit"
    else:
        value["domain"]["controller_capability_factor"] = .99
    result = match(cert, kwargs, value)
    assert not result["point_domain_evidence_matched"] and result["matched_cell_ids"] == []


@pytest.mark.parametrize("field,bad", [("speed_mps", 5.001), ("tilt_deg", 5.001),
    ("body_rate_rad_s", .02001), ("propellant_kg", 27999.)])
def test_contact_failures_stay_present_and_cannot_be_removed_from_census(field, bad):
    negative = saved_record()
    negative["contact"][field] = bad
    cert, kwargs = build(saved_record(), negative)
    assert not cert["qualification_complete"] and len(cert["cells"]) == 2
    assert [row["cell_passed"] for row in cert["cells"]] == [True, False]
    assert not match(cert, kwargs)["point_domain_evidence_matched"]
    del kwargs["records_raw"]["cell-1"]
    with pytest.raises(ValueError, match="complete_raw_cell_census"):
        q.build_certificate(**kwargs)


@pytest.mark.parametrize("flags", [{"record_checks_passed": False}, {"actual_state_continuity_verified": False},
    {"controller_plant_separation_verified": False}, {"prediction_only": True}, {"contact": None}])
def test_incomplete_or_prediction_only_record_is_not_qualification(flags):
    cert, kwargs = build(saved_record(**flags))
    assert not cert["qualification_complete"] and not match(cert, kwargs)["point_domain_evidence_matched"]


@pytest.mark.parametrize("mutation", ["producer_flag", "point_hash", "point_preimage", "schema", "limits", "identity"])
def test_forged_or_old_certificate_is_rejected_by_rebuild(mutation):
    cert, kwargs = build()
    if mutation == "producer_flag":
        cert = {"qualification_complete": True}
    elif mutation == "point_hash":
        cert["cells"][0]["point"]["state_sha256"] = "f"*64
    elif mutation == "point_preimage":
        cert["cells"][0]["point"] = point_data()
    elif mutation == "schema":
        cert["schema"] = "missionos.starship_state_return_qualification.v2"
    elif mutation == "limits":
        cert["contact_limits"]["maximum_speed_mps"] = 10.
    else:
        cert["identity"]["profile_sha256"] = "f"*64
    with pytest.raises(ValueError, match="not_independently_rebuilt"):
        match(cert, kwargs)


@pytest.mark.parametrize("target", ["saved", "current"])
@pytest.mark.parametrize("mutation", ["raw_hash", "preimage", "pointer", "hash_only", "producer_boolean", "flight", "model"])
def test_checker_result_requires_real_raw_preimages_and_pure_independent_check(target, mutation):
    cert, kwargs = build()
    def alter(value):
        if mutation == "raw_hash":
            value["record_sha256"] = "f"*64
        elif mutation == "preimage":
            value["point_data"]["state"]["r_eci_m"][0] += 1.
        elif mutation == "pointer":
            value["point_pointers"]["state"] = "/missing"
        elif mutation == "hash_only":
            value["point_data"] = q.make_point(value["point_data"])
        elif mutation == "producer_boolean":
            return {"passed": True}
        else:
            value["new_flights_during_check" if mutation == "flight" else "new_model_calls_during_check"] = 1
        return value
    if target == "saved":
        kwargs["verify_record"] = lambda data, **expected: alter(checker(data, **expected))
        with pytest.raises(ValueError):
            q.build_certificate(**kwargs)
    else:
        def callback(data, **expected):
            return alter(observer(data, **expected))
        with pytest.raises(ValueError):
            match(cert, kwargs, observe_current=callback)


@pytest.mark.parametrize("value", [True, None, {"passed": True}, "checker"])
def test_checker_is_explicit_trusted_code_not_input_json(value):
    _, kwargs = build()
    kwargs["verify_record"] = value
    with pytest.raises(ValueError, match="checker_callback_required"):
        q.build_certificate(**kwargs)


def test_duplicate_or_nonfinite_raw_json_is_rejected_even_if_callback_decodes_it():
    value = saved_record()
    encoded = raw(value)
    for invalid in (encoded[:-1]+b',"schema":"invented_qualification_record.v1"}',
            encoded.replace(b'90000.0', b'NaN')):
        _, kwargs = build()
        kwargs["records_raw"]["cell-0"] = invalid
        with pytest.raises(ValueError):
            q.build_certificate(**kwargs)


@pytest.mark.parametrize("mutation", ["missing_raw", "extra_raw", "text_raw", "duplicate_cell", "bool_inventory", "source_path"])
def test_registered_identity_and_complete_raw_census_are_closed(mutation):
    _, kwargs = build()
    if mutation == "missing_raw":
        kwargs["records_raw"].clear()
    elif mutation == "extra_raw":
        kwargs["records_raw"]["extra"] = next(iter(kwargs["records_raw"].values()))
    elif mutation == "text_raw":
        kwargs["records_raw"]["cell-0"] = kwargs["records_raw"]["cell-0"].decode()
    elif mutation == "duplicate_cell":
        kwargs["required_cells"] *= 2
    elif mutation == "bool_inventory":
        kwargs["required_cells"][0]["domain"]["retained_count"] = True
    else:
        kwargs["expected_identity"]["source_sha256"] = {"../private.py": "a"*64}
    with pytest.raises(ValueError):
        q.build_certificate(**kwargs)


def test_history_extraction_can_read_full_saved_state_rows_without_losing_actuator_history():
    value = saved_record()
    state = value["point_data"]["state"]
    value["history"] = [{"state": {**deepcopy(state), "time_s": t}, "retained_count": 13} for t in (1999., 2000.)]
    def projected(data, **expected):
        result = checker(data, **expected)
        result["point_pointers"]["actuator_history"] = "/history"
        return result
    cert, kwargs = build()
    kwargs.update(records_raw={"cell-0": raw(value)}, verify_record=projected)
    rebuilt = q.build_certificate(**kwargs)
    assert rebuilt["cells"][0]["point"] == cert["cells"][0]["point"]


def test_certificate_is_detached_and_record_reverification_is_required_on_each_admission():
    cert, kwargs = build()
    calls = []
    original = kwargs["verify_record"]
    def counted(data, **expected):
        calls.append(sha256(data).hexdigest())
        return original(data, **expected)
    kwargs["verify_record"] = counted
    match(cert, kwargs)
    match(cert, kwargs)
    assert len(calls) == 2
    cert["cells"][0]["domain"]["retained_count"] = 0
    assert kwargs["required_cells"][0]["domain"]["retained_count"] == 13
