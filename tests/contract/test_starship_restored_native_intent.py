"""Independent saved-goal checks; contacts/model receipts below are synthetic."""

from copy import deepcopy
from hashlib import sha256

import pytest

from src.runtime import starship_restored_native_verifier as native
from src.runtime import starship_restored_intent as producer
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_restored_operations import RegisteredRestoredOperations
from tests.contract import test_starship_restored_native_verifier as legacy
from tests.contract.test_starship_restored_verifier import contact_fixture

# The same fail-on-use plant/controller/provider/process boundary as the old
# saved-only contracts; no fixture here integrates or calls a hosted model.
saved_only = legacy.saved_only


def intent(slot=13, *, value=13, metric="inventory.retained_count", comparison="eq"):
    return {
        "schema": "missionos.operational_intent.v1",
        "intent_id": f"inventory-at-{slot}",
        "generation": 0,
        "issuer": "registered_operations",
        "intent_class": "desired_outcome_not_authority",
        "evaluation_point": "closed_inventory",
        "trigger": {"phase": "deployment", "released_count": slot},
        "objective": {"metric": metric, "comparison": comparison, "value": value, "unit": "count"},
    }


def operations(*, slot=13, goal=None):
    row = legacy.operations()
    row.update(schema=native.OPS_INTENT_SCHEMA, deployment_intents={"0": None, "13": None})
    row["deployment_intents"][str(slot)] = intent(slot) if goal is None else goal
    return row


def prefix(retained=13):
    _, _, profile, _, launch = contact_fixture(retained=retained)
    handoff = launch["restored_prefix"]["handoff_record"]
    launch["samples"] = [
        native._read(
            native._wire(
                mission._sample(
                    dyn.state_from_dict(row["state"]),
                    mission.vehicle(profile, payload_count=row["retained_count"]),
                    "orbital_coast",
                )
            )
        )
        for row in handoff["state_history"]
    ]
    return launch, profile


def request(ops, actual, source):
    """Use the real immutable Ops notice to produce a genuine wire receipt."""
    summary = actual["summary"]
    value = {
        "schema": "missionos.starship_restored_decision_request.v1",
        "request_id": "saved-intent",
        "phase": "deployment",
        "scope_sha256": "c" * 64,
        "time_s": summary["time_s"],
        "deadline_s": summary["time_s"] + 35.0,
        "facts": {
            "time_s": summary["time_s"],
            "released_count": summary["released_count"],
            "retained_count": summary["retained_count"],
            "release_ready": summary["release_ready"],
            "sequence_closed": False,
        },
        "notice_summary": ops["deployment_notices"][str(summary["released_count"])],
        "allowed_actions": ["continue_deployment", "withhold_deployment"],
    }
    notice = RegisteredRestoredOperations(ops).deployment_notice(summary)
    if type(notice) is producer.OperationalIntentNotice:
        observation = notice.observe(
            source_sha256=native._hash(source), scope_sha256=value["scope_sha256"]
        )
        value.update(schema=native.INTENT_REQUEST_SCHEMA, operational_intent=observation)
        value["facts"].update(producer.numeric_projection(observation))
    return native._read(native._wire(value))


def forbid_intent_producers(monkeypatch):
    def forbidden(*_a, **_kw):
        pytest.fail("Independent checker reused an intent producer")

    for name in (
        "validate_intent",
        "validate_observation",
        "numeric_projection",
        "project_effects",
        "evaluate_objective",
        "public_goal",
    ):
        monkeypatch.setattr(producer, name, forbidden)
    from src.intelligence import starship_restored_agent as agent

    for name in ("validate_observation", "numeric_projection", "project_effects", "public_goal"):
        monkeypatch.setattr(agent, name, forbidden)


def test_actual_checkpoint_and_public_projection_match_real_producer_then_producers_are_forbidden(
    monkeypatch,
):
    launch, profile = prefix()
    ops, source = operations(), {"src/runtime/starship_restored_intent.py": "d" * 64}
    actual = launch["restored_prefix"]["decisions"][1]
    value = request(ops, actual, source)
    from src.intelligence.starship_restored_agent import public_input

    public = public_input(value, provider="typesafe")
    forbid_intent_producers(monkeypatch)
    native._operations(ops)
    native._intent_request(value, source)
    native._deployment_intent(value, actual, launch, profile, ops, source)
    assert native._intent_public_input(value, "typesafe") == public
    assert native._goal_outcomes(ops, launch, closed=True)["all_goals_met"] is True


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "ops",
        "scope",
        "intent",
        "checkpoint_time",
        "checkpoint_hash",
        "ready",
        "sequencer",
        "counts",
        "effect",
        "current_value",
        "authority",
        "closed",
        "release",
        "bool_integer",
        "schema",
        "deadline",
        "action",
    ],
)
def test_independent_checkpoint_rejects_detached_tampering(monkeypatch, field):
    launch, profile = prefix()
    ops, source = operations(), {"source.py": "a" * 64}
    actual = launch["restored_prefix"]["decisions"][1]
    value = request(ops, actual, source)
    observation = value["operational_intent"]
    if field == "source":
        observation["source_sha256"] = "e" * 64
    elif field == "ops":
        observation["operations_sha256"] = "e" * 64
    elif field == "scope":
        observation["scope_sha256"] = "e" * 64
    elif field == "intent":
        observation["intent"]["objective"]["value"] = 0
    elif field == "checkpoint_time":
        observation["checkpoint"]["time_s"] += 1
    elif field == "checkpoint_hash":
        observation["checkpoint_sha256"] = "e" * 64
    elif field == "ready":
        observation["checkpoint"]["release_ready"] = False
    elif field == "sequencer":
        observation["checkpoint"]["sequencer_state"] = "skipped"
    elif field == "counts":
        observation["checkpoint"].update(released_count=0, retained_count=26)
    elif field == "effect":
        value["facts"]["effect.continue_deployment.retained_count_if_completed"] = 13
    elif field == "current_value":
        value["facts"]["intent.observed_value"] = 0
    elif field == "authority":
        observation["authority_granted"] = True
    elif field == "closed":
        observation["closed_inventory_inferred"] = True
    elif field == "release":
        observation["physical_release_inferred"] = True
    elif field == "bool_integer":
        value["facts"]["intent.active"] = 1
    elif field == "schema":
        value["schema"] = "missionos.starship_restored_decision_request.v1"
    elif field == "deadline":
        value["deadline_s"] += 1
    elif field == "action":
        value["allowed_actions"] = ["withhold_deployment"]
    forbid_intent_producers(monkeypatch)
    with pytest.raises(ValueError):
        native._deployment_intent(value, actual, launch, profile, ops, source)
        native._intent_request(value, source)


def test_pending_initial_checkpoint_is_not_replaced_by_later_resolution_state(monkeypatch):
    launch, profile = prefix()
    ops, source = operations(), {"source.py": "a" * 64}
    actual = launch["restored_prefix"]["decisions"][1]
    value = request(ops, actual, source)
    when = value["time_s"]
    actual["pending"] = {
        "released_count": 13,
        "started_s": when,
        "deadline_s": when + 35.0,
        "poll_count": 1,
    }
    actual["summary"].update(time_s=when + 2.0, release_ready=False)
    forbid_intent_producers(monkeypatch)
    native._deployment_intent(value, actual, launch, profile, ops, source)
    launch["samples"] = [row for row in launch["samples"] if row["time_s"] != when]
    with pytest.raises(ValueError, match="actual_inventory_witness"):
        native._deployment_intent(value, actual, launch, profile, ops, source)


@pytest.mark.parametrize(
    "retained,slot,value,status",
    [
        (13, 13, 13, "met"),
        (13, 0, 26, "unmet"),
        (0, 13, 13, "unmet"),
        (26, 0, 26, "met"),
        (26, 13, 13, "not_evaluable"),
    ],
)
def test_final_goal_uses_closed_actual_receipts_not_readiness_or_valid_choice(
    retained, slot, value, status
):
    launch, _ = prefix(retained)
    ops = operations(slot=slot, goal=intent(slot, value=value))
    result = native._goal_outcomes(ops, launch, closed=True)
    assert result["outcomes"][slot // 13]["status"] == status
    assert result["actual_released_count"] == 26 - retained
    assert result["actual_retained_count"] == retained
    assert result["physical_success_inferred"] is result["ai_work_inferred"] is False
    assert result["all_goals_met"] is (status == "met")


@pytest.mark.parametrize(
    "metric,comparison,value,met",
    [
        ("inventory.released_count", "gte", 13, True),
        ("inventory.retained_count", "lte", 12, False),
        ("inventory.retained_count", "eq", 13, True),
    ],
)
def test_generic_registered_metric_and_comparison_are_independent(metric, comparison, value, met):
    launch, _ = prefix()
    ops = operations(goal=intent(metric=metric, comparison=comparison, value=value))
    assert native._goal_outcomes(ops, launch, closed=True)["all_goals_met"] is met


def test_unclosed_inventory_cannot_infer_goal_or_physical_release_from_checkpoint():
    launch, _ = prefix()
    result = native._goal_outcomes(operations(), launch)
    assert result["outcomes"][1]["status"] == "not_evaluable"
    assert result["outcomes"][0]["status"] == "not_applicable"
    assert result["actual_released_count"] is result["actual_retained_count"] is None
    assert result["inventory_receipt_chain_sha256"] is None


def test_final_counts_must_equal_both_actual_receipt_chains():
    launch, _ = prefix()
    launch["restored_prefix"]["handoff_record"]["inventory"]["retained_count"] = 0
    with pytest.raises(ValueError, match="receipt_derived"):
        native._goal_outcomes(operations(), launch, closed=True)


@pytest.mark.parametrize(
    "change",
    [
        "int_bool",
        "value_bool",
        "evaluation",
        "issuer",
        "extra",
        "trigger",
        "comparison",
        "duplicate",
        "v1_borrow",
    ],
)
def test_external_registered_goal_is_strict_before_any_model_or_plant(change):
    ops = operations()
    goal = ops["deployment_intents"]["13"]
    if change == "int_bool":
        goal["generation"] = True
    elif change == "value_bool":
        goal["objective"]["value"] = True
    elif change == "evaluation":
        goal["evaluation_point"] = "current_checkpoint"
    elif change == "issuer":
        goal["issuer"] = "ai"
    elif change == "extra":
        goal["authority_granted"] = True
    elif change == "trigger":
        goal["trigger"]["released_count"] = 0
    elif change == "comparison":
        goal["objective"]["comparison"] = "approximately"
    elif change == "duplicate":
        ops["deployment_intents"]["0"] = deepcopy(goal)
        ops["deployment_intents"]["0"]["trigger"]["released_count"] = 0
    else:
        ops["schema"] = "missionos.starship_restored_native_operations.v1"
    with pytest.raises(ValueError):
        native._operations(ops)


def test_none_slot_cannot_borrow_typed_goal_and_projections():
    launch, profile = prefix()
    actual = launch["restored_prefix"]["decisions"][1]
    source, ops = {"source.py": "a" * 64}, operations()
    value = request(ops, actual, source)
    ops["deployment_intents"]["13"] = None
    with pytest.raises(ValueError, match="unregistered_intent"):
        native._deployment_intent(value, actual, launch, profile, ops, source)


def full_v2(monkeypatch, *, slot=13, target=13, changed_scope=False):
    """Real saved orchestration, invented contacts/host receipts; no inference."""
    original_case = legacy.case

    def source_case(*args, **kwargs):
        values = list(original_case(*args, **kwargs))
        values[4] = {**values[4], "src/runtime/starship_restored_intent.py": "d" * 64}
        return tuple(values)

    monkeypatch.setattr(legacy, "case", source_case)
    record, profile, args = legacy.synthetic_native_closed(monkeypatch, live=True)
    # Detach every alias before rewriting protocol hashes, just as real JSON.
    record = native._read(native._wire(record))
    host = native._read(args["host_record_raw"])
    ops = deepcopy(args["expected_operations"])
    ops.update(schema=native.OPS_INTENT_SCHEMA, deployment_intents={"0": None, "13": None})
    ops["deployment_intents"][str(slot)] = intent(slot, value=target)
    args["expected_operations"] = ops
    from src.intelligence.starship_restored_agent import public_input

    for index, actual in enumerate(record["prefix"]["restored_prefix"]["decisions"]):
        old = deepcopy(record["governance"]["deployment"]["decisions"][index]["request"])
        new = request(ops, actual, args["expected_source_sha256"])
        new["request_id"] = old["request_id"]
        new["scope_sha256"] = (
            "e" * 64
            if changed_scope
            else native._hash(
                {
                    "context": args["expected_context"],
                    "plan_sha256": args["expected_plan_sha256"],
                    "source_sha256": args["expected_source_sha256"],
                    "phase": "deployment",
                }
            )
        )
        if "operational_intent" in new:
            new["operational_intent"]["scope_sha256"] = new["scope_sha256"]
        substitutions = {
            native._hash(old): native._hash(new),
            native._hash(public_input(old, provider="typesafe")): native._hash(
                public_input(new, provider="typesafe")
            ),
        }

        def rewrite(value):
            if type(value) is dict:
                if value == old:
                    return deepcopy(new)
                return {key: rewrite(item) for key, item in value.items()}
            if type(value) is list:
                return [rewrite(item) for item in value]
            return substitutions.get(value, value) if type(value) is str else value

        record, host = rewrite(record), rewrite(host)
    args["host_record_raw"] = native._wire(host)
    args["expected_host_record_sha256"] = sha256(args["host_record_raw"]).hexdigest()
    return record, profile, args


def test_full_v2_goal_met_and_valid_continue_goal_unmet_do_not_change_ai_work_or_contact(
    monkeypatch,
):
    record, profile, args = full_v2(monkeypatch, slot=0, target=26)
    forbid_intent_producers(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["physical_success"] is result["ai_control_completed"] is True
    assert (
        record["governance"]["deployment"]["decisions"][0]["applied_action"]
        == "continue_deployment"
    )
    assert result["operational_goal_outcomes"]["outcomes"][0]["status"] == "unmet"
    assert result["operational_goal_outcomes"]["all_goals_met"] is False


def test_full_v2_met_goal_proof_and_real_receipts(monkeypatch):
    record, profile, args = full_v2(monkeypatch)
    forbid_intent_producers(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    goal = result["operational_goal_outcomes"]
    assert goal["all_goals_met"] is True and goal["outcomes"][1]["status"] == "met"
    assert goal["actual_released_count"] == goal["actual_retained_count"] == 13


def test_v1_result_still_has_no_new_goal_claim(monkeypatch):
    record, profile, args = legacy.synthetic_native_closed(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"] and "operational_goal_outcomes" not in result


def test_coherently_rehashed_request_and_intent_cannot_borrow_another_deployment_scope(monkeypatch):
    record, profile, args = full_v2(monkeypatch, changed_scope=True)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"]
    assert "deployment_scope_not_registered" in result["issues"][0]["detail"]
