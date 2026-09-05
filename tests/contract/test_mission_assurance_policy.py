from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.intelligence.mission_assurance_policy import AssurancePolicy, PolicyStore, load_policy
from src.runtime.mission_assurance_policy_fixture import run_fixture

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/assurance-policy/fixture.yaml"


def setup_policy(tmp_path, **changes):
    policy = AssurancePolicy.model_validate({**load_policy(EXAMPLE).model_dump(), **changes})
    store = PolicyStore(tmp_path / "policy.db")
    store.approve(policy, operator="fixture-operator", expected_sha256=policy.sha256)
    return store, policy


@pytest.mark.parametrize("suffix", ["\nmode: bounded\n", "\nunknown: true\n", "\napproved: true\n"])
def test_strict_yaml_rejects_ambiguity_and_authority(tmp_path, suffix):
    path = tmp_path / "invalid.yaml"
    path.write_text(EXAMPLE.read_text() + suffix)
    with pytest.raises(ValueError):
        load_policy(path)


def test_file_alone_is_not_approval_and_reviewed_hash_is_required(tmp_path):
    policy = load_policy(EXAMPLE)
    store = PolicyStore(tmp_path / "policy.db")
    with pytest.raises(ValueError, match="approved_active_policy_required"):
        store.approved(policy.sha256)
    with pytest.raises(ValueError, match="reviewed_hash"):
        store.approve(policy, operator="human", expected_sha256="wrong")


def test_bounded_graph_executes_without_fabricating_individual_approval(tmp_path):
    store, policy = setup_policy(tmp_path)
    result = run_fixture(store, policy.sha256, "proposal-1")
    continued = result["continuation"]
    assert continued["blocking_reasons"] == []
    assert continued["executor_invoked"] is True
    assert continued["human_approval_observed"] is False
    assert continued["authorization_source"] == "human_approved_policy"
    assert continued["policy_authorization"]["budget_reserved"] is True
    assert continued["physical_execution_invoked"] is False
    assert result["live_model_invoked"] is False


def test_budget_and_replay_survive_store_restart(tmp_path):
    store, policy = setup_policy(tmp_path, max_total_actions=2)
    assert run_fixture(store, policy.sha256, "p1")["continuation"]["executor_invoked"]
    store = PolicyStore(store.path)
    replay = run_fixture(store, policy.sha256, "p1")["continuation"]
    assert not replay["executor_invoked"]
    assert "policy_proposal_already_reserved" in replay["blocking_reasons"]
    assert run_fixture(store, policy.sha256, "p2")["continuation"]["executor_invoked"]
    last = run_fixture(store, policy.sha256, "p3")["continuation"]
    assert not last["executor_invoked"]
    assert "policy_budget_exhausted" in last["blocking_reasons"]


def test_out_of_scope_has_no_dispatch(tmp_path):
    store, policy = setup_policy(tmp_path)
    result = run_fixture(store, policy.sha256, "outside", target_x=50)["continuation"]
    assert not result["executor_invoked"]
    assert "policy_parameter_out_of_range:target_x_m" in result["blocking_reasons"]


@pytest.mark.parametrize("mode", ["human", "shadow"])
def test_non_delegated_modes_require_individual_approval(tmp_path, mode):
    store, policy = setup_policy(tmp_path, mode=mode)
    blocked = run_fixture(store, policy.sha256, "p1")["continuation"]
    assert not blocked["executor_invoked"]
    assert blocked["policy_authorization"]["policy_eligible"]
    passed = run_fixture(store, policy.sha256, "p1", explicit_approval=True)["continuation"]
    assert passed["executor_invoked"]
    assert passed["human_approval_observed"]
    assert passed["authorization_source"] == "individual_human_approval"


def test_revocation_cannot_be_undone_by_reapproving_same_file(tmp_path):
    store, policy = setup_policy(tmp_path)
    store.revoke(policy.sha256)
    with pytest.raises(ValueError):
        store.approve(policy, operator="human", expected_sha256=policy.sha256)


@pytest.fixture
def check_case(tmp_path):
    store, policy = setup_policy(tmp_path)
    graph = run_fixture(store, policy.sha256, "initial")["mission_incident_graph"]
    request = {
        "task_id": policy.mission_id,
        "proposal_id": "fresh",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {"target_x_m": 2.0, "target_y_m": 1.0},
    }
    facts = {
        "mission_id": policy.mission_id,
        "execution_scope": "fixture",
        "mission_contract_sha256": policy.mission_contract_sha256,
        "observed_at": datetime.now(UTC).isoformat(),
        "source_ref": "fixture-world",
        "predicates": dict.fromkeys(policy.preserve, True),
    }
    return store, policy, graph, request, facts


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("execution_scope", "physical", "policy_execution_scope_mismatch"),
        ("mission_id", "other-mission", "policy_mission_mismatch"),
        ("mission_contract_sha256", "changed", "policy_mission_contract_changed"),
        ("observed_at", "2000-01-01T00:00:00Z", "policy_fresh_observation_required"),
        ("observed_at", "2099-01-01T00:00:00Z", "policy_fresh_observation_required"),
        ("predicates", {"payload_integrity": True}, "policy_preserve_condition_not_verified"),
        ("predicates", {"payload_integrity": "true"}, "policy_preserve_condition_not_verified"),
    ],
)
def test_runtime_facts_fail_closed(check_case, field, value, reason):
    store, policy, graph, request, facts = check_case
    facts[field] = value
    result = store.check(policy.sha256, graph=graph, request=request, facts=facts, consume=True)
    assert not result["policy_authorized"]
    assert reason in result["blocking_reasons"]


def test_judged_candidate_cannot_be_replaced(check_case):
    store, policy, graph, request, facts = check_case
    request["recovery_parameters"]["target_x_m"] = 3.0
    result = store.check(policy.sha256, graph=graph, request=request, facts=facts, consume=True)
    assert not result["policy_authorized"]
    assert "policy_judged_parameters_mismatch" in result["blocking_reasons"]


def test_concurrent_budget_reservations_are_atomic(check_case):
    store, policy, graph, request, facts = check_case

    def reserve(index):
        return store.check(
            policy.sha256,
            graph=graph,
            request={**request, "proposal_id": f"p{index}"},
            facts=facts,
            consume=True,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    # The initial graph fixture already consumed one of the three slots.
    assert sum(result["policy_authorized"] for result in results) == 2


def test_changed_policy_requires_fresh_assurance_and_preserves_budget(check_case):
    store, policy, graph, request, facts = check_case
    changed = AssurancePolicy.model_validate(
        {**policy.model_dump(), "policy_id": "new-version", "max_total_actions": 1}
    )
    store.approve(changed, operator="human", expected_sha256=changed.sha256)
    result = store.check(changed.sha256, graph=graph, request=request, facts=facts, consume=True)
    assert not result["policy_authorized"]
    assert "policy_not_bound_to_assurance_judgment" in result["blocking_reasons"]
    assert "policy_budget_exhausted" in result["blocking_reasons"]


def test_expiry_is_rechecked_after_approval(check_case, monkeypatch):
    from src.intelligence import mission_assurance_policy as module

    store, policy, graph, request, facts = check_case

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2100, 1, 1, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", FutureDatetime)
    result = store.check(policy.sha256, graph=graph, request=request, facts=facts, consume=True)
    assert not result["policy_authorized"]
    assert "policy_expired" in result["blocking_reasons"]


def test_failed_revalidation_never_consumes_policy_budget(check_case):
    from src.intelligence.missionos_mission_incident_continuation_graph import (
        run_missionos_mission_incident_continuation_graph,
    )

    store, policy, graph, request, facts = check_case
    calls = []
    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=graph,
        continuation_request=request,
        action_revalidation_handler=lambda _: {
            "validation_status": "blocked",
            "reasons": ["stale"],
        },
        policy_authorization_handler=store.handler(policy.sha256, lambda: facts),
        executor_handler=lambda _: calls.append("execute"),
        verifier_handler=lambda _: {},
        observation_handler=lambda _: {},
    )
    assert not calls
    assert result["blocking_reasons"] == ["stale"]
    assert store.check(policy.sha256, graph=graph, request=request, facts=facts, consume=True)[
        "policy_authorized"
    ]


def test_json_authority_fields_cannot_replace_trusted_handler(check_case):
    from src.intelligence.missionos_mission_incident_continuation_graph import (
        run_missionos_mission_incident_continuation_graph,
    )

    _store, policy, graph, request, _facts = check_case
    request.update(policy_authorized=True, policy_sha256=policy.sha256)
    calls = []
    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=graph,
        continuation_request=request,
        action_revalidation_handler=lambda _: {
            "validation_status": "valid",
            "proposal_id": "fresh",
        },
        executor_handler=lambda _: calls.append("execute"),
        verifier_handler=lambda _: {},
        observation_handler=lambda _: {},
    )
    assert not calls
    assert "explicit_recovery_dispatch_approval_required" in result["blocking_reasons"]


def test_bounded_policy_cannot_be_bypassed_with_individual_flag(tmp_path):
    store, policy = setup_policy(tmp_path)
    result = run_fixture(store, policy.sha256, "outside", target_x=50, explicit_approval=True)
    assert not result["continuation"]["executor_invoked"]
