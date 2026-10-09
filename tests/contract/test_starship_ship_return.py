"""Shared return history survives JSON without resetting reference or plant."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from src.runtime.starship_attitude_reference import ConditionedGeographicFrame, ParallelTransportFrame
from src.runtime.starship_retained_return import CONDITIONED_POLICY_ID, CONTINUOUS_POLICY_ID, TRIMMED_POLICY_ID
from src.runtime.starship_ship_return import ReturnController, advance_plant
from src.runtime.starship_sixdof_mission import vehicle
from tests.contract.test_starship_return_prediction import example


@pytest.mark.parametrize("phase", ["orbital_coast", "deorbit_slew", "deorbit_burn", "ballistic_return", "landing_burn"])
@pytest.mark.parametrize("policy", ["fixed_v1", CONTINUOUS_POLICY_ID, CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID])
def test_resume_retains_reference_history_and_identical_finite_motion(phase, policy):
    profile, state, _ = example()
    craft = vehicle(profile, payload_count=1)
    controller = ReturnController(policy, 1090., 25, phase=phase, allocation_enabled=True)
    if policy != "fixed_v1" and phase in ("ballistic_return", "landing_burn"):
        controller.record["status"] = "active" if phase == "ballistic_return" else "triggered"
        controller.return_frame = (ConditionedGeographicFrame(state.q_body_to_eci, state.time_s,
            maximum_roll_rate_rad_s=.01) if policy in (CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID) else ParallelTransportFrame(state.q_body_to_eci))
        controller.return_frame.target((.2, .3, .9), *((0., 1., 0.), state.q_body_to_eci)
            if policy in (CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID) else (), **({"time_s": state.time_s, "force_bridge": True}
            if policy in (CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID) else {}))
    saved = controller.to_dict()
    resumed = ReturnController.from_dict(saved)
    assert resumed.to_dict() == saved
    original_state = asdict(state)
    left_samples, right_samples, left_events, right_events = [], [], [], []
    left = controller.update(state, craft, profile, left_samples, lambda *a, **kw: left_events.append((a, kw)))
    right = resumed.update(state, craft, profile, right_samples, lambda *a, **kw: right_events.append((a, kw)))
    assert left == right and left_samples == right_samples and left_events == right_events
    assert asdict(state) == original_state
    if not left.transition_only:
        command, diagnostics = controller.command(left, craft, profile, 300000.)
        command2, diagnostics2 = resumed.command(right, craft, profile, 300000.)
        assert command == command2 and diagnostics == diagnostics2
        args = (.1, profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"], 300000., 7500.)
        assert advance_plant(left.state, craft, command, *args) == advance_plant(right.state, craft, command2, *args)
    resumed.record["evaluation_count"] = 999
    assert controller.record["evaluation_count"] != 999
    assert saved == json.loads(json.dumps(saved))


@pytest.mark.parametrize("mutation", ["missing", "unknown", "nan", "policy", "boolean", "frame"])
def test_incomplete_or_invalid_saved_history_rejected(mutation):
    _, state, _ = example()
    controller = ReturnController(TRIMMED_POLICY_ID, 1090., 26)
    controller.return_frame = ParallelTransportFrame(state.q_body_to_eci)
    saved = deepcopy(controller.to_dict())
    if mutation == "missing":
        del saved["previous_budget"]
    elif mutation == "unknown":
        saved["reset_flaps"] = True
    elif mutation == "nan":
        saved["record"]["value"] = float("nan")
    elif mutation == "policy":
        saved["record"]["policy_id"] = "fixed_v1"
    elif mutation == "boolean":
        saved["registered"] = 1
    else:
        saved["return_frame"]["quaternion"] = [1., 1., 0., 0.]
    with pytest.raises(ValueError):
        ReturnController.from_dict(saved)


def test_start_is_due_once_and_does_not_reset_actuators():
    profile, state, _ = example()
    craft = vehicle(profile, payload_count=0)
    controller = ReturnController(TRIMMED_POLICY_ID, 1090., 26)
    samples = []
    with pytest.raises(ValueError):
        controller.start(state, craft, profile, samples, lambda *a, **kw: None)
    state = replace(state, time_s=1090.)
    before = asdict(state)
    controller.start(state, craft, profile, samples, lambda *a, **kw: None)
    assert asdict(state) == before and samples[-1]["engine_states"] == list(before["engine_states"])
    with pytest.raises(ValueError):
        controller.start(state, craft, profile, samples, lambda *a, **kw: None)


def test_prepared_trim_and_prior_budget_survive_multiple_terminal_steps():
    profile, state, _ = example()
    craft = vehicle(profile, payload_count=1)
    original = ReturnController(TRIMMED_POLICY_ID, 900., 25, phase="landing_burn", allocation_enabled=True)
    original.record["status"] = "triggered"
    original.entry_prepared = {"status": "prepared", "basis": "legacy_geographic",
                              "trim_angles_rad": [0.01]*len(craft.aero_panels)}
    original.trim_attempts, original.next_trim_attempt_s = 2, 950.
    original.previous_budget = {"state": asdict(state), "budget": {"fuel_budget_estimate_kg": 12345.}}
    original.landing_frame_started = True
    original.return_frame = ConditionedGeographicFrame(state.q_body_to_eci, state.time_s, maximum_roll_rate_rad_s=.01)
    original.return_frame.bridging = True
    restored = ReturnController.from_dict(original.to_dict())
    a, b = state, state
    for _ in range(4):
        left = original.update(a, craft, profile, [], lambda *args, **kwargs: None)
        right = restored.update(b, craft, profile, [], lambda *args, **kwargs: None)
        cmd, diag = original.command(left, craft, profile, 300000.)
        cmd2, diag2 = restored.command(right, craft, profile, 300000.)
        assert cmd == cmd2 and diag == diag2
        args = (.1, profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"], 300000., 7500.)
        a, _ = advance_plant(a, craft, cmd, *args)
        b, _ = advance_plant(b, craft, cmd2, *args)
        assert a == b and original.to_dict() == restored.to_dict()
    restored.entry_prepared["trim_angles_rad"][0] = .5
    assert original.entry_prepared["trim_angles_rad"][0] == .01
