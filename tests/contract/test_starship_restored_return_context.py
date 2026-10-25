"""Numeric context/effect projections only; no private records or plant runs."""

from copy import deepcopy
import socket
import subprocess

import pytest

from src.runtime import starship_restored_return_context as context
from src.intelligence import starship_restored_agent as agent
from src.runtime.starship_restored_decision_pipe import _deep_request
from tests.contract.test_starship_restored_agent import (
    SyntheticTransport,
    request as legacy_request,
)

ACTIONS = [
    "select_nominal",
    "keep_reserved_return",
    "select_next_orbit",
    "request_scope_amendment",
    "wait",
    "request_deep_reasoning",
]
AREA = "synthetic_area_or_contact_availability_window"


@pytest.fixture(autouse=True)
def no_physics_or_provider(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_restored_return as returning
    from src.intelligence.starship_m2_broker import DeadlineTransport

    def forbidden(*_a, **_k):
        pytest.fail("Return context contracts cannot run a plant, process, network or provider")

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (env, "step"),
        (returning, "forecast_restored_return"),
        (DeadlineTransport, "_invoke"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


def tool_pairs(*, nominal_available=False):
    pairs = {}
    for name, when in (("nominal", 3600.0), ("next_orbit", 9100.0)):
        available = name == "next_orbit" or nominal_available
        reasons = [] if available else [AREA]
        pairs[name] = (
            {"proposal": {"candidate_id": name, "return_time_s": when}},
            {
                "stage": "selection",
                "time_s": 2200.0,
                "accepted": available,
                "reasons": reasons,
                "numerical_contact_capability_met": True,
                "matched_synthetic_site_ids": [name + "-area"] if available else [],
            },
        )
    return pairs


def request(*, reserved="nominal", nominal_available=False):
    pairs = tool_pairs(nominal_available=nominal_available)
    reservation = {
        "candidate_id": reserved,
        "return_time_s": pairs[reserved][0]["proposal"]["return_time_s"],
    }
    facts = {
        "time_s": 2200.0,
        "released_count": 13,
        "retained_count": 13,
        "sequence_closed": True,
        "authority.revision": int(reserved == "next_orbit"),
        "authority.nominal_allowed": True,
        "authority.next_orbit_allowed": reserved == "next_orbit",
        "authority.can_request_amendment": reserved == "nominal",
    }
    for name, (row, check) in pairs.items():
        facts.update(
            {
                f"candidate.{name}.return_time_s": row["proposal"]["return_time_s"],
                f"candidate.{name}.feasibility_passed": check["accepted"],
            }
        )
    facts.update(context.projection_facts(reservation, pairs, observed_s=2200.0))
    return {
        "schema": agent.SCHEMA,
        "request_id": "context-0",
        "phase": "return",
        "scope_sha256": "a" * 64,
        "time_s": 2200.0,
        "deadline_s": 2380.0,
        "facts": facts,
        "notice_summary": "Untrusted prose does not change the registered tools.",
        "allowed_actions": ACTIONS.copy()
        if reserved == "nominal"
        else ["keep_reserved_return", "wait", "request_deep_reasoning"],
    }


def test_existing_tool_rejection_and_scope_are_separate_and_keep_names_actual_reservation():
    value = request()
    before = deepcopy(value)
    public = agent.public_input(value, provider="typesafe")
    assert value == before and public["allowed_actions"] == ACTIONS
    current = public["return_context"]["current_reservation"]
    assert current == {
        "candidate_id": "nominal",
        "return_time_s": 3600.0,
        "independent_tools_accepted": False,
        "numerical_contact_capability_met": True,
        "operationally_available_at_predicted_contact": False,
        "admission_reasons": [AREA],
        "current_scope_allows_candidate": True,
    }
    keep = public["conditional_return_effects"]["keep_reserved_return"]
    assert keep["candidate_id"] == "nominal" and keep["return_time_s"] == 3600.0
    assert keep["would_preserve_current_reservation"] and not keep["independent_tools_accepted"]
    assert keep["current_scope_allows_candidate"] and not keep["dispatch_permission_granted"]
    next_effect = public["conditional_return_effects"]["request_scope_amendment"]
    assert next_effect["candidate_id"] == "next_orbit" and next_effect["independent_tools_accepted"]
    assert (
        next_effect["operator_scope_amendment_required"]
        and not next_effect["answer_alone_changes_reservation"]
    )
    for action in ACTIONS:
        effect = public["conditional_return_effects"][action]
        assert not effect["dispatch_permission_granted"] and not effect["deadline_extended"]
    assert "energy remaining" in public["return_context"]["metric_semantics"]["candidate.*.power_kwh"]
    assert "decision time" in public["return_context"]["metric_semantics"]["resources.power_kwh"]


def test_keep_after_actual_change_names_next_and_does_not_assume_nominal():
    value = request(reserved="next_orbit")
    public = agent.public_input(value, provider="typesafe")
    for action in value["allowed_actions"]:
        effect = public["conditional_return_effects"][action]
        assert effect["candidate_id"] == "next_orbit" and effect["return_time_s"] == 9100.0
        assert effect["would_preserve_current_reservation"]
        assert not effect["operator_scope_amendment_required"]


@pytest.mark.parametrize("reason", sorted(context.REASONS))
def test_each_existing_admission_reason_is_a_true_numeric_flag_not_arbitrary_prose(reason):
    pairs = tool_pairs(nominal_available=True)
    check = pairs["nominal"][1]
    check.update(accepted=False, reasons=[reason])
    if reason == AREA:
        check["matched_synthetic_site_ids"] = []
    if reason == "numerical_contact_capability_unmet":
        check["numerical_contact_capability_met"] = False
    output = context.projection_facts(
        {"candidate_id": "nominal", "return_time_s": 3600.0}, pairs, observed_s=2200.0
    )
    assert output[f"candidate.nominal.reason.{reason}"] is True
    assert output["candidate.nominal.admission_reason_count"] == 1
    assert all(type(value) in (bool, int, float) for value in output.values())


@pytest.mark.parametrize(
    "mutation",
    [
        "bool_version",
        "version",
        "missing",
        "two_reserved",
        "no_reserved",
        "time",
        "unknown_reason",
        "false_reason",
        "count_bool",
        "count",
        "accepted",
        "capability",
        "availability",
        "authority",
    ],
)
def test_malformed_or_contradictory_projection_fails_before_provider(mutation):
    value = request()
    facts = value["facts"]
    if mutation == "bool_version":
        facts[context.VERSION_KEY] = True
    elif mutation == "version":
        facts[context.VERSION_KEY] = 2
    elif mutation == "missing":
        facts.pop("reservation.return_time_s")
    elif mutation == "two_reserved":
        facts["reservation.next_orbit"] = True
    elif mutation == "no_reserved":
        facts["reservation.nominal"] = False
    elif mutation == "time":
        facts["reservation.return_time_s"] = 9100.0
    elif mutation == "unknown_reason":
        facts["candidate.nominal.reason.untrusted_instruction"] = True
    elif mutation == "false_reason":
        facts["candidate.nominal.reason." + AREA] = False
    elif mutation == "count_bool":
        facts["candidate.nominal.admission_reason_count"] = True
    elif mutation == "count":
        facts["candidate.nominal.admission_reason_count"] = 0
    elif mutation == "accepted":
        facts["candidate.nominal.feasibility_passed"] = True
    elif mutation == "capability":
        facts["candidate.nominal.numerical_contact_capability_met"] = False
    elif mutation == "availability":
        facts["notice.nominal_available"] = True
    else:
        facts["authority.next_orbit_allowed"] = 0
    with pytest.raises(ValueError):
        agent.public_input(value, provider="typesafe")


@pytest.mark.parametrize(
    "mutation",
    [
        "epoch",
        "stage",
        "unknown_reason",
        "duplicate_reason",
        "accepted",
        "matched",
        "capability",
        "reservation",
    ],
)
def test_projection_uses_same_observed_tools_and_actual_binding_without_mutation(mutation):
    pairs = tool_pairs()
    binding = {"candidate_id": "nominal", "return_time_s": 3600.0}
    check = pairs["nominal"][1]
    if mutation == "epoch":
        check["time_s"] += 0.25
    elif mutation == "stage":
        check["stage"] = "dispatch"
    elif mutation == "unknown_reason":
        check["reasons"] = ["new_unreviewed_reason"]
    elif mutation == "duplicate_reason":
        check["reasons"] *= 2
    elif mutation == "accepted":
        check["accepted"] = True
    elif mutation == "matched":
        check["matched_synthetic_site_ids"] = ["nominal-area"]
    elif mutation == "capability":
        check["numerical_contact_capability_met"] = False
    else:
        binding["return_time_s"] = 9100.0
    before = deepcopy((binding, pairs))
    with pytest.raises(ValueError):
        context.projection_facts(binding, pairs, observed_s=2200.0)
    assert (binding, pairs) == before


def test_no_marker_has_exact_legacy_public_shape_and_no_new_goal_or_effect():
    old = legacy_request()
    public = agent.public_input(old, provider="typesafe")
    assert set(public) == {
        "phase",
        "time_s",
        "deadline_s",
        "evidence",
        "operations_notice",
        "allowed_actions",
        "effects",
        "scope",
    }
    assert public["evidence"] == old["facts"]
    assert "return_context" not in public and "conditional_return_effects" not in public
    partial = request()
    partial["facts"].pop(context.VERSION_KEY)
    with pytest.raises(ValueError, match="facts_or_actions"):
        agent.public_input(partial, provider="typesafe")


def test_marker_cannot_borrow_deployment_scope_or_change_delegation_epoch():
    wrong = request()
    wrong.update(phase="deployment", allowed_actions=list(agent.DEPLOYMENT))
    with pytest.raises(ValueError, match="requires_return"):
        agent.validate_request(wrong)
    original = request()
    delegated = _deep_request(original, 150.0)
    assert (
        delegated["time_s"] == original["time_s"]
        and delegated["deadline_s"] == original["time_s"] + 150.0
    )
    assert delegated["facts"] == original["facts"]
    public = agent.public_input(delegated, provider="deepseek")
    assert "request_deep_reasoning" not in public["conditional_return_effects"]
    assert public["return_context"]["current_reservation"]["candidate_id"] == "nominal"


def test_valid_contrary_keep_is_decoded_without_action_filter_or_confidence_retry():
    value = request()
    probabilities = {
        action: (0.37 if action == "keep_reserved_return" else 0.126) for action in ACTIONS
    }
    answer = {
        "model": agent.JEV_MODEL,
        "answers": {
            "decision": {
                "type": "choice",
                "choice": "keep_reserved_return",
                "probabilities": probabilities,
                "confidence": 0.24,
            }
        },
    }
    transport = SyntheticTransport(answer)
    result = agent.RestoredAgent(transport=transport).assess(
        value, provider="typesafe", remaining_s=180.0
    )
    assert (
        result["status"] == "valid"
        and result["action"] == "keep_reserved_return"
        and transport.calls == 1
    )
    assert (
        "cannot make a failed check acceptable"
        in transport.sent["questions"]["decision"]["instructions"]
    )
    assert transport.sent["state"]["allowed_actions"] == ACTIONS


def test_legacy_decoder_payload_does_not_gain_context_instructions():
    value = legacy_request()
    transport = SyntheticTransport(
        {
            "model": agent.JEV_MODEL,
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "select_nominal",
                    "probabilities": {
                        "select_nominal": 0.5,
                        "select_next_orbit": 0.3,
                        "request_deep_reasoning": 0.2,
                    },
                    "confidence": 0.5,
                }
            },
        }
    )
    agent.RestoredAgent(transport=transport).assess(value, provider="typesafe", remaining_s=180.0)
    assert transport.sent["questions"]["decision"]["instructions"] == (
        "Choose an eligible mission-level action using the supplied numeric evidence and effects. "
        "Keeping a plan can be correct. A choice is not approval, dispatch or recovery. "
        "If necessary and eligible, explicitly request deeper reasoning."
    )
