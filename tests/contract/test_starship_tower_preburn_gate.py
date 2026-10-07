"""Preburn lifecycle fixtures; no integrations or physical qualification."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from src.runtime.starship_tower_preburn_gate import (
    BINDING_FIELDS, FixturePreburnGate, PATH_SCHEMA, PreburnGateError, SCHEMA, digest,
)
from src.runtime.starship_tower_supervision import tower_observation


def body(time_s=140.):
    return {"time_s": time_s, "r_eci_m": [6378137., 0., 20000.], "v_eci_mps": [1., 2., 3.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 78000.,
        "engine_states": [{"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": True}],
        "flap_angles_rad": [0.]*6}


def inputs(*, status="complete", achieved=True):
    bindings = {name: digest(name) for name in BINDING_FIELDS}
    contract = {"schema": SCHEMA, "mode": "fixture", "bindings": bindings,
        "issued_simulation_time_s": 139., "issued_wall_time_s": 1000.,
        "original_simulation_deadline_s": 150., "original_wall_deadline_s": 1010.,
        "latest_preburn_choice_time_s": 147., "maximum_observation_age_s": 2.,
        "expected_phase": "recovery_boostback_slew"}
    marker = {"state_sha256": digest(body()), "time_s": 140.,
        "controller_context_sha256": digest("current-controller"), "fault_prefix_sha256": digest("current-faults")}
    paths = [{"schema": PATH_SCHEMA, "mode": "fixture", "site_id": site, "bindings": deepcopy(bindings),
        "raw_record_sha256": digest(site+"-raw"), "verifier_receipt_sha256": digest(site+"-verdict"),
        "declared_status": status, "declared_terminal_objective_achieved": achieved,
        "common_preburn_prefix_sha256": digest("shared-prefix"), "applicable_states": [deepcopy(marker)]}
        for site in ("capture", "divert")]
    context = {"session_id": "session", "plan_id": "plan", "run_id": "run", "request_id": "request",
        "plan_sha256": bindings["plan_sha256"]}
    contract["context"] = deepcopy(context)
    observation = tower_observation(context=context, observation_id="observation", state=body(),
        wall_time_s=1001., tower_ready=True, return_mode="undecided")
    directive = {"schema": "missionos.starship_tower_directive.v1", "context": deepcopy(context),
        "source_sha256": bindings["source_sha256"], "approval_record_sha256": bindings["approval_record_sha256"],
        "action": "continue_capture", "reason": "fixture", "simulation_time_s": 140., "wall_time_s": 1001.,
        "observed_evidence_sha256": observation["evidence_sha256"], "state_sha256": observation["state_sha256"],
        "consumed_once": True, "numeric_flight_authority": False, "requires_executor_rules_check": True,
        "executor_application_reported": False, "physical_effect_verified": False}
    directive["sha256"] = digest(directive)
    kwargs = {"simulation_time_s": 140., "wall_time_s": 1001., "phase": "recovery_boostback_slew",
        "source_sha256": bindings["source_sha256"], "plan_sha256": bindings["plan_sha256"],
        "controller_context_sha256": marker["controller_context_sha256"],
        "fault_prefix_sha256": marker["fault_prefix_sha256"], "first_site_specific_command_issued": False,
        "directive": directive}
    return contract, paths, observation, kwargs


def test_fixture_choice_retains_state_and_never_admits_a_physical_path():
    contract, paths, observation, kwargs = inputs()
    original = deepcopy((contract, paths, observation, kwargs))
    gate = FixturePreburnGate(contract, paths)
    check = gate.consider(observation, **kwargs)
    assert check["fixture_requested_site_id"] == "capture"
    assert (contract, paths, observation, kwargs) == original
    receipt = gate.receipt()
    assert receipt["qualification_evidence_status"] == "caller_assertions_unchecked"
    assert all(receipt[name] is False for name in ("qualification_verified", "physical_default_available",
        "physical_activation_allowed", "goal_application_reported", "physical_effect_verified", "state_written"))
    assert gate.consider(observation, **kwargs)["reason"] == "choice_already_consumed"


@pytest.mark.parametrize("mutation", ["different-origin", "different-faults", "different-tracker", "different-source",
    "different-prefix", "unknown-site", "numeric-authority", "live-mode"])
def test_unbound_or_extra_authority_path_contract_rejected(mutation):
    contract, paths, _, _ = inputs()
    if mutation == "different-prefix":
        paths[1]["common_preburn_prefix_sha256"] = digest("other-prefix")
    elif mutation == "unknown-site":
        paths[1]["site_id"] = "ocean"
    elif mutation == "numeric-authority":
        contract["throttle"] = .5
    elif mutation == "live-mode":
        contract["mode"] = "live"
    else:
        name = {"different-origin": "origin_state_sha256", "different-faults": "fault_history_sha256",
            "different-tracker": "origin_controller_context_sha256", "different-source": "source_sha256"}[mutation]
        paths[1]["bindings"][name] = digest("different")
    with pytest.raises(PreburnGateError):
        FixturePreburnGate(contract, paths)


@pytest.mark.parametrize("change,reason", [
    ({"first_site_specific_command_issued": True}, "first_site_specific_command_already_issued"),
    ({"simulation_time_s": 147.}, "latest_preburn_boundary_reached"),
    ({"phase": "recovery_boostback_burn"}, "phase_outside_preburn_gate"),
    ({"source_sha256": "f"*64}, "source_or_plan_changed"),
    ({"controller_context_sha256": "f"*64}, "both_path_assertions_not_applicable"),
    ({"fault_prefix_sha256": "f"*64}, "both_path_assertions_not_applicable"),
    ({"wall_time_s": 1003.1}, "fresh_preburn_observation_required"),
    ({"simulation_time_s": 140.1}, "same_time_actual_preburn_state_required"),
])
def test_current_gate_rejects_late_stale_or_cross_context_choice(change, reason):
    contract, paths, observation, kwargs = inputs()
    kwargs.update(change)
    gate = FixturePreburnGate(contract, paths)
    check = gate.consider(observation, **kwargs)
    assert check["reason"] == reason and check["fixture_requested_site_id"] is None
    assert gate.receipt()["fixture_choice"] is None


@pytest.mark.parametrize("status,achieved", [("partial", True), ("failed", False), ("complete", False)])
def test_incomplete_path_cannot_supply_even_a_fixture_default(status, achieved):
    contract, paths, observation, kwargs = inputs(status=status, achieved=achieved)
    observation["tower_ready"] = False
    observation["evidence_sha256"] = digest({k: v for k, v in observation.items() if k != "evidence_sha256"})
    kwargs["directive"] = None
    check = FixturePreburnGate(contract, paths).consider(observation, **kwargs)
    assert check["reason"] == "both_path_assertions_not_applicable" and check["fixture_requested_site_id"] is None


def test_deadline_or_tower_default_remains_fixture_only():
    for deadline in (False, True):
        contract, paths, observation, kwargs = inputs()
        if deadline:
            contract["original_wall_deadline_s"] = 1001.
        else:
            observation["tower_ready"] = False
            observation["evidence_sha256"] = digest({k: v for k, v in observation.items() if k != "evidence_sha256"})
        kwargs["directive"] = None
        gate = FixturePreburnGate(contract, paths)
        assert gate.consider(observation, **kwargs)["fixture_requested_site_id"] == "divert"
        assert gate.receipt()["physical_default_available"] is False


@pytest.mark.parametrize("change", [{"schema": "model-proposal"}, {"numeric_flight_authority": True},
    {"state_sha256": "f"*64}, {"executor_application_reported": True}, {"consumed_once": False}])
def test_proposal_or_mutated_directive_cannot_be_consumed(change):
    contract, paths, observation, kwargs = inputs()
    kwargs["directive"].update(change)
    kwargs["directive"]["sha256"] = digest({k: v for k, v in kwargs["directive"].items() if k != "sha256"})
    with pytest.raises(PreburnGateError):
        FixturePreburnGate(contract, paths).consider(observation, **kwargs)


def test_one_choice_under_two_concurrent_fixture_calls():
    contract, paths, observation, kwargs = inputs()
    gate = FixturePreburnGate(contract, paths)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: gate.consider(observation, **kwargs), range(2)))
    assert sorted(row["reason"] for row in results) == ["choice_already_consumed", "untrusted_directive_assertion_fixture_eligible"]


def test_command_and_later_state_are_separate_external_assertions():
    contract, paths, observation, kwargs = inputs()
    gate = FixturePreburnGate(contract, paths)
    gate.consider(observation, **kwargs)
    choice = gate.receipt()["fixture_choice"]
    with pytest.raises(PreburnGateError):
        gate.record_later_observation_assertion(choice_sha256=choice["sha256"], state=body(141.),
            site_id="capture", controller_context_sha256=digest("later-controller"))
    wrong = body()
    wrong["v_eci_mps"][0] += 1.
    with pytest.raises(PreburnGateError):
        gate.record_first_command_assertion(choice_sha256=choice["sha256"], state=wrong, site_id="capture",
            command_sha256=digest("command"))
    gate.record_first_command_assertion(choice_sha256=choice["sha256"], state=body(), site_id="capture",
        command_sha256=digest("command"))
    with pytest.raises(PreburnGateError):
        gate.record_later_observation_assertion(choice_sha256=choice["sha256"], state=body(),
            site_id="capture", controller_context_sha256=digest("later-controller"))
    gate.record_later_observation_assertion(choice_sha256=choice["sha256"], state=body(141.),
        site_id="capture", controller_context_sha256=digest("later-controller"))
    receipt = gate.receipt()
    assert receipt["first_site_specific_command_report"]["command_issued_by_gate"] is False
    assert receipt["later_observation_report"]["physical_effect_verified"] is False
    receipt["contract"]["original_simulation_deadline_s"] += 900.
    assert gate.receipt()["contract"] == contract


def test_checkpoints_are_not_widened_to_a_state_envelope():
    contract, paths, observation, kwargs = inputs()
    observation["state"]["propellant_kg"] -= .001
    observation["state_sha256"] = digest(observation["state"])
    observation["evidence_sha256"] = digest({k: v for k, v in observation.items() if k != "evidence_sha256"})
    kwargs["directive"] = None
    check = FixturePreburnGate(contract, paths).consider(observation, **kwargs)
    assert check["reason"] == "both_path_assertions_not_applicable"


@pytest.mark.parametrize("field", ["run_id", "plan_sha256", "request_id"])
def test_other_run_plan_or_request_observation_is_rejected(field):
    contract, paths, observation, kwargs = inputs()
    observation["context"][field] = "f"*64 if field == "plan_sha256" else "different"
    observation["evidence_sha256"] = digest({k: v for k, v in observation.items() if k != "evidence_sha256"})
    kwargs["directive"] = None
    check = FixturePreburnGate(contract, paths).consider(observation, **kwargs)
    assert check["reason"] == "observation_cross_context"


def test_another_approval_record_does_not_supply_a_typed_choice():
    contract, paths, observation, kwargs = inputs()
    kwargs["directive"]["approval_record_sha256"] = "f"*64
    kwargs["directive"]["sha256"] = digest({k: v for k, v in kwargs["directive"].items() if k != "sha256"})
    with pytest.raises(PreburnGateError):
        FixturePreburnGate(contract, paths).consider(observation, **kwargs)


def test_self_hashed_directive_shape_is_explicitly_untrusted_everywhere():
    contract, paths, observation, kwargs = inputs()
    # A caller can construct this shape and hash. No signature, supervision
    # record, actor integrity, or authenticated human identity is verified.
    gate = FixturePreburnGate(contract, paths)
    check = gate.consider(observation, **kwargs)
    receipt = gate.receipt()
    assert check["reason"] == "untrusted_directive_assertion_fixture_eligible"
    for value in (check, receipt, receipt["fixture_choice"]):
        assert value["directive_evidence_status"] == "untrusted_caller_assertion"
        assert all(value[name] is False for name in ("directive_provenance_verified",
            "supervision_record_bound", "actor_integrity_verified", "approval_independently_verified"))
    choice = receipt["fixture_choice"]
    gate.record_first_command_assertion(choice_sha256=choice["sha256"], state=body(), site_id="capture",
        command_sha256=digest("command"))
    gate.record_later_observation_assertion(choice_sha256=choice["sha256"], state=body(141.), site_id="capture",
        controller_context_sha256=digest("later-controller"))
    for name in ("first_site_specific_command_report", "later_observation_report"):
        assert gate.receipt()[name]["directive_provenance_verified"] is False


def test_invalid_directive_is_transactional_and_cannot_advance_retained_clocks():
    contract, paths, observation, kwargs = inputs()
    gate = FixturePreburnGate(contract, paths)
    before = gate.receipt()
    invalid = deepcopy(kwargs)
    invalid["wall_time_s"] = 1002.
    invalid["directive"]["wall_time_s"] = 1002.
    invalid["directive"]["state_sha256"] = "f"*64
    invalid["directive"]["sha256"] = digest({k: v for k, v in invalid["directive"].items() if k != "sha256"})
    with pytest.raises(PreburnGateError):
        gate.consider(observation, **invalid)
    assert gate.receipt() == before
    assert gate.consider(observation, **kwargs)["fixture_requested_site_id"] == "capture"


def test_first_command_assertion_remains_bound_to_the_actual_choice_tick():
    contract, paths, observation, kwargs = inputs()
    gate = FixturePreburnGate(contract, paths)
    gate.consider(observation, **kwargs)
    choice = gate.receipt()["fixture_choice"]
    later = deepcopy(kwargs)
    later["simulation_time_s"] = 141.
    later["wall_time_s"] = 1002.
    later["directive"] = None
    gate.consider(observation, **later)
    with pytest.raises(PreburnGateError):
        gate.record_first_command_assertion(choice_sha256=choice["sha256"], state=body(), site_id="capture",
            command_sha256=digest("command"))


def test_invalid_assertion_after_choice_does_not_break_exact_state_command_record():
    contract, paths, observation, kwargs = inputs()
    gate = FixturePreburnGate(contract, paths)
    gate.consider(observation, **kwargs)
    choice = gate.receipt()["fixture_choice"]
    before = gate.receipt()
    invalid = deepcopy(kwargs)
    invalid["simulation_time_s"] = 141.
    invalid["wall_time_s"] = 1002.
    invalid["directive"]["numeric_flight_authority"] = True
    invalid["directive"]["sha256"] = digest({k: v for k, v in invalid["directive"].items() if k != "sha256"})
    with pytest.raises(PreburnGateError):
        gate.consider(observation, **invalid)
    assert gate.receipt() == before
    gate.record_first_command_assertion(choice_sha256=choice["sha256"], state=body(), site_id="capture",
        command_sha256=digest("command"))
