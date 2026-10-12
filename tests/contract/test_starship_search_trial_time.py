"""Scalar search timestamps tolerate arithmetic rounding, not altered evidence."""
from copy import deepcopy
import math

import pytest

from test_starship_return_search_regressions import item, rehash, study_for  # noqa: F401
from src.runtime import starship_return_time_search as screen
from src.runtime import starship_m2_verifier as verifier


def test_recomputed_trial_times_accept_one_ulp_without_changing_origin(request):
    fixture = request.getfixturevalue("item")
    assert screen.search(fixture.actor)
    study = study_for(fixture)
    row = study["return_study"]["return_time_search"]
    before = deepcopy(row)
    for trial in row["trials"]:
        trial["return_time_s"] = math.nextafter(trial["return_time_s"], math.inf)
    assert 0 < row["trials"][0]["return_time_s"]-before["trials"][0]["return_time_s"] < 1e-7
    rehash(study)
    verifier._search_evidence(study, fixture.actor.deployment_binding)


@pytest.mark.parametrize("mutation", ["time", "nonfinite", "boolean", "origin", "scale", "source", "receipt"])
def test_float_tolerance_does_not_relax_other_trial_bindings(request, mutation):
    fixture = request.getfixturevalue("item")
    assert screen.search(fixture.actor)
    study = study_for(fixture)
    row = study["return_study"]["return_time_search"]
    trial = row["trials"][0]
    reason = "search_trial_binding"
    if mutation == "time":
        trial["return_time_s"] += 1e-6
    elif mutation == "nonfinite":
        trial["return_time_s"] = math.inf
    elif mutation == "boolean":
        trial["return_time_s"] = True
    elif mutation == "origin":
        trial["origin"]["state"]["r_eci_m"][0] += 0.001
    elif mutation == "scale":
        trial["integration_scale"] = 0.5
    elif mutation == "source":
        row["source_sha256"]["fixture.py"] = "b"*64
        reason = "search_authority_binding"
    else:
        study["return_study"]["execution"]["events"][-1]["record_sha256"] = "b"*64
        reason = "search_execution_receipt"
    if mutation not in {"receipt", "nonfinite"}:
        rehash(study)
    with pytest.raises(ValueError, match=reason):
        verifier._search_evidence(study, fixture.actor.deployment_binding)


@pytest.mark.parametrize("mutation", [None, "horizon", "overrun", "wrong_flag", "integer_flag", "missing_flag", "adopted_after_cutoff"])
def test_optional_search_clock_metadata_is_independently_bound(request, mutation):
    fixture = request.getfixturevalue("item")
    assert screen.search(fixture.actor)
    study = study_for(fixture)
    row = study["return_study"]["return_time_search"]
    row["clock_horizon_s"] = fixture.actor.scheduled+fixture.actor.envelope["maximum_delay_s"]
    row["search_cutoff_exceeded"] = row["end_time_s"] >= row["cutoff_time_s"]
    if mutation == "horizon":
        row["clock_horizon_s"] += 1.
    elif mutation == "overrun":
        row["end_time_s"] = row["clock_horizon_s"]+1.
        row["later_observation"]["state"]["time_s"] = row["end_time_s"]
    elif mutation == "wrong_flag":
        row["search_cutoff_exceeded"] = not row["search_cutoff_exceeded"]
    elif mutation == "integer_flag":
        row["search_cutoff_exceeded"] = int(row["search_cutoff_exceeded"])
    elif mutation == "missing_flag":
        del row["search_cutoff_exceeded"]
    elif mutation == "adopted_after_cutoff":
        row["cutoff_time_s"] = row["end_time_s"]
        row["search_cutoff_exceeded"] = True
        assert row["accepted_for_trial"] is True
    rehash(study)
    if mutation is None:
        verifier._search_evidence(study, fixture.actor.deployment_binding)
    else:
        with pytest.raises(ValueError, match="search_catchup_horizon_or_cutoff"):
            verifier._search_evidence(study, fixture.actor.deployment_binding)


def test_pre_metadata_saved_search_records_remain_readable(request):
    fixture = request.getfixturevalue("item")
    assert screen.search(fixture.actor)
    study = study_for(fixture)
    row = study["return_study"]["return_time_search"]
    row.pop("clock_horizon_s", None)
    row.pop("search_cutoff_exceeded", None)
    rehash(study)
    verifier._search_evidence(study, fixture.actor.deployment_binding)
