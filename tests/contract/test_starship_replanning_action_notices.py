"""Event revision and dispatch notices must match registered operational input."""

from copy import deepcopy

import pytest

from src.runtime.starship_replanning import notices
from src.runtime.starship_replanning_verifier import event_operation_notice_reasons


def operation(now):
    return {
        "time_s": now,
        "observation": {"state": {"time_s": now}},
        "notice": notices("event_tradeoff", now),
    }


def study():
    return {"plan_revisions": [operation(1450.25)], "dispatch": operation(2280.7)}


def test_registered_notice_at_each_action_time_is_valid():
    assert event_operation_notice_reasons(study(), "event_tradeoff") == []


@pytest.mark.parametrize("kind", ["revision", "dispatch"])
@pytest.mark.parametrize("tamper", ["forged_clearance", "old_notice", "wrong_time"])
def test_self_consistent_action_check_cannot_replace_registered_notice(kind, tamper):
    record = study()
    target = record["plan_revisions"][0] if kind == "revision" else record["dispatch"]
    if tamper == "forged_clearance":
        target["notice"]["areas"] = {"invented_clearance": [0.0, 100000.0]}
    elif tamper == "old_notice":
        target["notice"] = notices("event_tradeoff", 1400.0)
    else:
        target["observation"]["state"]["time_s"] -= 0.25
    # This independent identity/time check does not trust a cached admit result.
    target["check"] = {"accepted": True}
    issue = "_observation_time" if tamper == "wrong_time" else "_notice_registration"
    assert event_operation_notice_reasons(record, "event_tradeoff") == [kind + issue]


def test_expected_case_is_trusted_instead_of_study_case():
    record = study()
    record["case"] = "event_normal"
    assert event_operation_notice_reasons(record, "event_tradeoff") == []
    assert event_operation_notice_reasons(record, "event_normal") == [
        "revision_notice_registration",
        "dispatch_notice_registration",
    ]


def test_check_is_read_only_and_all_revisions_are_covered():
    record = study()
    record["plan_revisions"].append(operation(1600.0))
    record["plan_revisions"][1]["notice"] = notices("event_tradeoff", 1000.0)
    before = deepcopy(record)
    assert event_operation_notice_reasons(record, "event_tradeoff") == [
        "revision_notice_registration"
    ]
    assert record == before


@pytest.mark.parametrize("sequence", [2.0, True])
def test_python_equal_numeric_notice_values_do_not_share_canonical_identity(sequence):
    record = study()
    target = record["plan_revisions"][0]
    target["notice"]["sequence"] = sequence
    assert event_operation_notice_reasons(record, "event_tradeoff") == [
        "revision_notice_registration"
    ]


@pytest.mark.parametrize("when", [True, float("nan"), float("inf")])
def test_action_times_must_be_finite_numbers(when):
    record = study()
    record["dispatch"]["time_s"] = when
    record["dispatch"]["observation"]["state"]["time_s"] = when
    assert event_operation_notice_reasons(record, "event_tradeoff") == [
        "dispatch_observation_time"
    ]
