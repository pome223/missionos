"""Preregistration and observation-boundary tests; no provider or flight runs."""

from copy import deepcopy
from hashlib import sha256
import json
import math
import random

import pytest

from src.runtime.starship_operational_trials import (
    GENERATOR,
    TRIALS,
    delivered_notice,
    is_trial,
    trial_manifest,
)


def test_trial_design_is_fixed_immutable_and_has_disjoint_splits():
    assert list(TRIALS) == [f"op_trial_{i:02d}" for i in range(1, 9)]
    assert len({spec["seed"] for spec in TRIALS.values()}) == 8
    for split in ("development", "evaluation"):
        rows = [row for row in TRIALS.values() if row["split"] == split]
        assert len(rows) == 4
        assert {row["profile"] for row in rows} == {
            "nominal", "clearance_interruption", "service_forecast_correction", "delivery_outage",
        }
    with pytest.raises(TypeError):
        TRIALS["op_trial_01"] = {}
    with pytest.raises(TypeError):
        TRIALS["op_trial_01"]["seed"] = 1


def test_named_draws_reproduce_without_using_global_randomness():
    before = random.getstate()
    first = trial_manifest("op_trial_03")
    for case in reversed(TRIALS):
        trial_manifest(case)
    assert trial_manifest("op_trial_03") == first
    assert random.getstate() == before
    assert first["generator"] == GENERATOR == "sha256_named_draw_v1"
    assert all(draw["range"][0] <= draw["value"] <= draw["range"][1] for draw in first["draws"].values())
    encoded = json.dumps([trial_manifest(case) for case in TRIALS], sort_keys=True, separators=(",", ":"))
    # Changing this digest changes the preregistered schedules, not just a test
    # expectation: publish a new generator/catalog version before new trials.
    assert sha256(encoded.encode()).hexdigest() == "08d732e3e36f3b34e92040423c6be0aad7a37750496c3e1bd7c9804b2be631cd"


@pytest.mark.parametrize("case", TRIALS)
def test_all_values_are_json_native_finite_and_schedule_is_bounded(case):
    manifest = trial_manifest(case)
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert manifest["parameter_basis"] == "synthetic_engineering_assumptions_not_historical_rates_or_thresholds"
    assert manifest["model_calls"] == 0
    assert manifest["physics_fault_injection"] is False
    assert not {"expected_action", "winner", "correct_action"}.intersection(manifest)
    publications = manifest["publications"]
    assert publications[0]["notice"]["issued_at_s"] == 900.0
    assert publications[0]["delivery_at_s"] < 1000.0
    assert [row["notice"]["sequence"] for row in publications] == list(range(1, len(publications) + 1))
    for row in publications:
        notice = row["notice"]
        assert 900.0 <= notice["issued_at_s"] < row["delivery_at_s"] <= 2100.0
        assert notice["expires_at_s"] > notice["issued_at_s"]
        assert set(notice["areas"]) <= {"east", "west"}
        for area, interval in notice["areas"].items():
            assert interval == {"east": [4500.0, 5600.0], "west": [10000.0, 11000.0]}[area]
        forecast = notice["recovery_service_forecast"]
        assert forecast["basis"] == "synthetic_uncertain_estimate_not_authority_or_observed_completion"
        for area in ("east", "west"):
            lower, upper = forecast[f"{area}_delay_after_contact_s"]
            assert 0 <= lower < upper
            weather = notice["reported_weather"][area]
            assert set(weather) == {"wind_speed_mps", "significant_wave_height_m"}
            assert all(type(value) is float and math.isfinite(value) and value >= 0 for value in weather.values())
        assert notice["reported_weather"]["measured_at_s"] <= notice["issued_at_s"]


@pytest.mark.parametrize("case", TRIALS)
def test_policy_sees_only_delivered_messages_not_future_or_harness_metadata(case):
    manifest = trial_manifest(case)
    previous = None
    forbidden = {"case", "seed", "split", "profile", "generator", "draws", "publications", "delivery_at_s"}

    def all_keys(value):
        if isinstance(value, dict):
            return set(value).union(*(all_keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(all_keys(child) for child in value))
        return set()

    for publication in manifest["publications"]:
        received = publication["delivery_at_s"]
        # Even already-issued information cannot cross a delayed link early.
        assert delivered_notice(case, publication["notice"]["issued_at_s"]) == previous
        assert delivered_notice(case, math.nextafter(received, -math.inf)) == previous
        observed = delivered_notice(case, received)
        assert observed == publication["notice"]
        assert not forbidden.intersection(all_keys(observed))
        assert case not in json.dumps(observed)
        assert manifest["profile"] not in json.dumps(observed)
        previous = observed
    assert delivered_notice(case, 20000.0) == previous


@pytest.mark.parametrize("case", TRIALS)
def test_mutating_returned_manifest_or_observation_cannot_change_later_reads(case):
    original = trial_manifest(case)
    changed = trial_manifest(case)
    changed["publications"][0]["notice"]["areas"].clear()
    changed["draws"].clear()
    assert trial_manifest(case) == original
    observed = delivered_notice(case, 1000.0)
    saved = deepcopy(observed)
    observed["areas"].clear()
    observed["recovery_service_forecast"]["east_delay_after_contact_s"][0] = -1
    assert delivered_notice(case, 1000.0) == saved


@pytest.mark.parametrize("case", ("op_trial_01", "op_trial_05"))
def test_nominal_update_changes_only_publication_metadata(case):
    notices = [deepcopy(row["notice"]) for row in trial_manifest(case)["publications"]]
    for notice in notices:
        del notice["sequence"]
        del notice["issued_at_s"]
    assert notices[0] == notices[1]


@pytest.mark.parametrize("case", ("op_trial_02", "op_trial_06"))
def test_uncertain_reopening_estimate_is_not_actual_reinstatement(case):
    publications = trial_manifest(case)["publications"]
    withdrawn = delivered_notice(case, publications[1]["delivery_at_s"])
    assert set(withdrawn["areas"]) == {"west"}
    estimate = withdrawn["clearance_reopening_estimate"]
    assert estimate["basis"] == "synthetic_uncertain_estimate_not_clearance_or_future_truth"
    actual = publications[2]["notice"]["issued_at_s"]
    assert actual not in estimate["east_time_s"]
    assert "reinstatement_issue_s" not in json.dumps(withdrawn)
    assert set(delivered_notice(case, publications[2]["delivery_at_s"])["areas"]) == {"east", "west"}


@pytest.mark.parametrize("case", ("op_trial_04", "op_trial_08"))
def test_delivery_outage_exposes_an_expired_old_notice_until_new_receipt(case):
    publications = trial_manifest(case)["publications"]
    first, delayed = publications
    expires = first["notice"]["expires_at_s"]
    assert expires < delayed["notice"]["issued_at_s"] < delayed["delivery_at_s"]
    stale = delivered_notice(case, expires + 1.0)
    assert stale == first["notice"]
    assert stale["expires_at_s"] < expires + 1.0
    assert delivered_notice(case, delayed["notice"]["issued_at_s"]) == stale
    assert delivered_notice(case, delayed["delivery_at_s"])["sequence"] == 2


@pytest.mark.parametrize("offset", range(4))
def test_evaluation_schedule_differs_from_development_without_using_an_expected_action(offset):
    development = trial_manifest(f"op_trial_{offset + 1:02d}")
    evaluation = trial_manifest(f"op_trial_{offset + 5:02d}")
    assert development["profile"] == evaluation["profile"]
    assert development["publications"] != evaluation["publications"]


@pytest.mark.parametrize("case", (None, [], {}, True, 1, "normal", "op_trial_00", "op_trial_09"))
def test_unknown_or_unhashable_cases_are_rejected(case):
    assert is_trial(case) is False
    with pytest.raises(ValueError, match="unknown_starship_operational_trial"):
        trial_manifest(case)
    with pytest.raises(ValueError, match="unknown_starship_operational_trial"):
        delivered_notice(case, 1000.0)


@pytest.mark.parametrize("time_s", (None, "1000", True, [], -1.0, math.nan, math.inf, -math.inf))
def test_invalid_observation_times_are_rejected(time_s):
    with pytest.raises(ValueError, match="invalid_operational_trial_time"):
        delivered_notice("op_trial_01", time_s)
