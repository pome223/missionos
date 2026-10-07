"""Short producer receipts and independent causal-request tamper checks.

These are initialized arithmetic fixtures, never launch/return evidence.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path

import pytest

from src.runtime import starship_reference_tracking_verifier as checker
from src.runtime import starship_constrained_recovery_verifier as recovery
from src.runtime.starship_booster_recovery_verifier import _Invalid

ROOT = Path(__file__).resolve().parents[2]


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(scope="module")
def configured():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, "booster")
    origin = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 30000., time_s=180.)
    up, east, north = env.local_frame(origin)
    anchor = tuple(float(x) for x in _attitude(up, east))
    actual = tuple(float(x) for x in _attitude(env.unit(env.add(up, env.scale(north, .1))), east))
    goal = tuple(float(x) for x in _attitude(env.unit(env.add(up, env.scale(east, .4))), north))
    state = dyn.State6DOF(180., origin.r, env.add(origin.v, env.scale(up, -600.)), actual,
        (.02, -.03, .004), 200000., tuple(dyn.EngineState() for _ in body.engines),
        tuple(0. for _ in body.aero_panels))
    return profile, body, state, anchor, goal


def record(configured, *, low_q=False, landing=False, prior=None, tracker=None, state=None, historyless=False):
    from src.runtime import starship_physics as env
    from src.runtime.starship_reference_tracking import ReferenceTracker
    from src.runtime.starship_booster_control import control_coast_stopping_distance, control_with_measured_tvc
    from src.runtime.starship_sixdof_booster import _sample_booster
    profile, body, original, anchor, goal = configured
    state = original if state is None else state
    if low_q:
        origin = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 100000., time_s=state.time_s)
        up, _, _ = env.local_frame(origin)
        state = replace(state, r_eci_m=origin.r, v_eci_mps=env.add(origin.v, env.scale(up, -600.)))
    if prior is None and not historyless:
        prior = {"time_s": state.time_s-.1, "phase": "recovery_powered_entry_prepare", "command": {"held": True},
                 "navigation": {"target_q_body_to_eci": list(anchor)}}
    if tracker is None:
        tracker = (ReferenceTracker(profile, state.q_body_to_eci, state.time_s) if historyless else
                   ReferenceTracker(profile, prior["navigation"]["target_q_body_to_eci"], prior["time_s"]))
    target, reference = tracker.update(goal, state.q_body_to_eci, state.omega_body_rad_s, state.time_s)
    if low_q and not landing:
        command, nav = control_coast_stopping_distance(state, body, target, profile, control_interval_s=.1)
    else:
        command, nav = control_with_measured_tvc(state, body, target, .4 if landing else 0., 3 if landing else 0,
            profile, use_flaps=True, development_fin_allocation=True, control_interval_s=.1,
            reference_rate_body_rad_s=reference["reference_rate_body_rad_s"],
            reference_acceleration_body_rad_s=reference["reference_acceleration_body_rad_s"])
    nav["reference_tracking"] = reference["receipt"]
    phase = "recovery_landing_13" if landing else "recovery_entry_coast"
    point = {"time_s": state.time_s, "phase": phase, "state": asdict(state), "command": asdict(command), "navigation": nav}
    sample = _sample_booster(state, body, phase, command, nav)
    return saved(point), saved(sample), saved(prior), tracker, state, command


def verify(point, sample, profile, prior, *, checkpoint_index=None):
    return checker.verify_reference_checkpoint(point, sample, profile, previous_checkpoint=prior,
                                              checkpoint_index=checkpoint_index)


@pytest.mark.parametrize("low_q,landing", ((False, False), (True, False), (True, True), (False, True)))
def test_scoped_generator_and_tracking_feedback(configured, low_q, landing):
    profile = configured[0]
    point, sample, prior, *_ = record(configured, low_q=low_q, landing=landing)
    # The first tracking request must occur in coast, even if an initialized
    # controller-only landing fixture uses the same nonzero reference fields.
    if landing:
        prior["phase"] = "recovery_entry_coast"
        initial, first_sample, anchor, tracker, state, command = record(configured, low_q=low_q)
        from src.runtime import starship_sixdof as dyn
        assert verify(initial, first_sample, profile, anchor)
        state = dyn.step(state, configured[1], command, .1)
        point, sample, prior, *_ = record(configured, low_q=low_q, landing=True, prior=initial, tracker=tracker, state=state)
    result = verify(point, sample, profile, prior)
    assert result["request_is_execution"] is False
    assert result["actual_state_assigned"] is False
    assert ("reference_tracking_control" in point["navigation"]) is (landing or not low_q)
    assert result["requested_q_body_to_eci"] != result["raw_goal_q_body_to_eci"]


def test_low_q_then_high_q_history_is_continuous(configured):
    profile, body = configured[:2]
    first, sample, prior, tracker, state, command = record(configured, low_q=True)
    assert verify(first, sample, profile, prior)
    from src.runtime import starship_sixdof as dyn
    state = dyn.step(state, body, command, .1)
    # Change only the fixture's altitude for this arithmetic scope test. It is
    # not presented to the trajectory continuity checker as integrated flight.
    state = replace(state, r_eci_m=configured[2].r_eci_m, v_eci_mps=configured[2].v_eci_mps)
    second, sample, prior, *_ = record(configured, prior=first, tracker=tracker, state=state)
    item = verify(second, sample, profile, prior)
    assert item["first_update"] is False
    assert item["previous_requested_q_body_to_eci"] == first["navigation"]["reference_tracking"]["requested_q_body_to_eci"]


@pytest.mark.parametrize("mutation", ("previous_q", "previous_rate", "previous_time", "first", "same_time",
    "raw_goal", "goal_error", "unbounded_rate", "rate_limited", "rate", "acceleration", "request_q",
    "actual_q", "actual_rate", "body_rate", "body_acceleration", "rate_limit", "acceleration_limit",
    "rate_clipped", "acceleration_clipped", "nav_target", "state_assignment", "execution", "missing_prior",
    "previous_goal", "previous_goal_time", "goal_rotation", "unbounded_goal_rate", "goal_rate", "goal_rate_clipped",
    "closing_rate", "closing_bound", "propagated_request", "closing_error"))
def test_causal_generator_tampering_is_rejected(configured, mutation):
    profile = configured[0]
    point, sample, prior, *_ = record(configured)
    item = point["navigation"]["reference_tracking"]
    fields = {"previous_q": "previous_requested_q_body_to_eci", "previous_rate": "previous_reference_rate_eci_rad_s",
        "raw_goal": "raw_goal_q_body_to_eci", "goal_error": "goal_error_rotation_eci_rad",
        "unbounded_rate": "unbounded_reference_rate_eci_rad_s", "rate_limited": "rate_limited_reference_rate_eci_rad_s",
        "rate": "reference_rate_eci_rad_s", "acceleration": "reference_acceleration_eci_rad_s2",
        "request_q": "requested_q_body_to_eci", "actual_q": "actual_q_body_to_eci",
        "actual_rate": "actual_body_rate_rad_s", "body_rate": "reference_rate_body_rad_s",
        "body_acceleration": "reference_acceleration_body_rad_s2", "previous_goal": "previous_raw_goal_q_body_to_eci",
        "goal_rotation": "raw_goal_rotation_eci_rad", "unbounded_goal_rate": "unbounded_goal_rate_eci_rad_s",
        "goal_rate": "goal_rate_eci_rad_s", "closing_rate": "closing_reference_rate_eci_rad_s",
        "propagated_request": "goal_rate_propagated_request_q_body_to_eci", "closing_error": "closing_error_rotation_eci_rad"}
    if mutation in fields:
        item[fields[mutation]][0] += .01
    elif mutation == "previous_time":
        item["previous_time_s"] -= .1
    elif mutation == "previous_goal_time":
        item["previous_raw_goal_time_s"] += .1
    elif mutation == "closing_bound":
        item["closing_rate_bound_rad_s"] += .01
    elif mutation == "first":
        item["first_update"] = False
    elif mutation == "same_time":
        item["initialized_at_current_time"] = True
    elif mutation in ("rate_limit", "acceleration_limit"):
        item["maximum_reference_rate_rad_s" if mutation == "rate_limit" else "maximum_reference_acceleration_rad_s2"] *= 2
    elif mutation in ("rate_clipped", "acceleration_clipped", "goal_rate_clipped"):
        item[mutation] = not item[mutation]
    elif mutation == "nav_target":
        point["navigation"]["target_q_body_to_eci"] = item["raw_goal_q_body_to_eci"]
    elif mutation == "state_assignment":
        item["actual_state_assigned"] = True
    elif mutation == "execution":
        item["request_is_execution"] = True
    else:
        prior = None
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        verify(point, sample, profile, prior)


@pytest.mark.parametrize("field", ("reference_rate_body_rad_s", "reference_acceleration_body_rad_s",
    "actual_body_rate_rad_s", "transport_term_body_rad_s2", "raw_requested_acceleration_body_rad_s2",
    "limited_requested_acceleration_body_rad_s2", "requested_rigid_body_torque_body_nm",
    "actual_aero_torque_body_nm", "pre_fin_torque_body_nm"))
def test_existing_gain_tracking_algebra_tampering_is_rejected(configured, field):
    profile = configured[0]
    point, sample, prior, *_ = record(configured)
    item = point["navigation"]["reference_tracking_control"]
    item[field][0] += 1.
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        verify(point, sample, profile, prior)


@pytest.mark.parametrize("mutation", ("missing", "clipped", "rate_ceiling", "limit", "inertia", "prefin_demand", "lowq_ff"))
def test_feedback_scope_and_actual_inertia_are_checked(configured, mutation):
    profile = configured[0]
    point, sample, prior, *_ = record(configured, low_q=mutation == "lowq_ff")
    nav = point["navigation"]
    if mutation == "lowq_ff":
        nav["reference_tracking_control"] = {}
    elif mutation == "missing":
        del nav["reference_tracking_control"]
    elif mutation == "inertia":
        sample["inertia_kg_m2"][0][0] += 1.
    elif mutation == "prefin_demand":
        nav["development_fin_allocation"]["requested_increment_torque_body_nm"][0] += 1.
    elif mutation == "clipped":
        nav["reference_tracking_control"]["acceleration_clipped"] = not nav["reference_tracking_control"]["acceleration_clipped"]
    else:
        nav["reference_tracking_control"]["rate_ceiling_rad_s" if mutation == "rate_ceiling" else "component_acceleration_limit_rad_s2"] *= 2.
    sample["controller"] = deepcopy(nav)
    with pytest.raises(_Invalid):
        verify(point, sample, profile, prior)


@pytest.mark.parametrize("policy", tuple(policy for policy in recovery._CONFIGURATIONS
    if policy not in (recovery._TRACKING_POLICY, recovery._ACTUAL_POLICY, recovery._TRANSPORT_POLICY,
                      recovery._ACTUAL_TRANSPORT_POLICY)))
def test_old_policy_cannot_opt_into_new_control_by_attaching_receipt(configured, policy):
    profile = configured[0]
    point, sample, prior, *_ = record(configured)
    with pytest.raises(_Invalid):
        recovery._reference_protocol(point, sample, profile, policy, prior)


def test_terminal_and_out_of_scope_cannot_claim_another_command(configured):
    point, sample, prior, *_ = record(configured)
    for phase, command in (("recovery_boostback_burn", point["command"]), ("recovery_entry_coast", None)):
        changed = deepcopy(point)
        changed.update(phase=phase, command=command)
        with pytest.raises(_Invalid):
            verify(changed, sample, configured[0], prior)


def test_checker_has_no_producer_dynamics_numpy_or_scipy_imports():
    source = inspect.getsource(checker)
    for name in ("starship_reference_tracking import", "starship_sixdof import", "starship_sixdof_mission import",
                 "starship_booster_control import", "import numpy", "import scipy"):
        assert name not in source


def test_shortest_half_turn_reference_is_invariant_under_goal_quaternion_sign(configured):
    import math
    from src.runtime import starship_sixdof as dyn
    profile, body, state, anchor, _ = configured
    # Exact pi hemisphere selection is part of the declared request convention.
    goal = dyn.quaternion_multiply((math.cos(math.pi/2), -1., 0., 0.), anchor)
    results = []
    for sign in (1, -1):
        setup = profile, body, state, anchor, tuple(sign*x for x in goal)
        point, sample, prior, *_ = record(setup)
        results.append(verify(point, sample, profile, prior)["requested_q_body_to_eci"])
    assert results[0] == pytest.approx(results[1], abs=1e-10)


def test_reference_at_rest_retains_anchor_without_invented_rate(configured):
    profile, body, state, anchor, _ = configured
    point, sample, prior, *_ = record((profile, body, state, anchor, anchor))
    item = verify(point, sample, profile, prior)
    assert item["reference_rate_eci_rad_s"] == [0., 0., 0.]
    assert item["reference_acceleration_eci_rad_s2"] == [0., 0., 0.]


def test_governed_trim_axis_must_follow_actual_requested_frame(configured):
    profile = configured[0]
    point, sample, prior, *_ = record(configured)
    requested = point["navigation"]["reference_tracking"]["requested_q_body_to_eci"]
    body_up = recovery._rotate(requested, [0., 0., 1.])
    axis = [sum(a*b for a, b in zip(body_up, basis)) for basis in recovery._local_axes(point["state"])]
    point["navigation"]["governed_entry_axis_enu"] = axis
    sample["controller"] = deepcopy(point["navigation"])
    recovery._reference_protocol(point, sample, profile, recovery._TRACKING_POLICY, prior)
    point["navigation"]["governed_entry_axis_enu"][0] += .01
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        recovery._reference_protocol(point, sample, profile, recovery._TRACKING_POLICY, prior)


def test_quaternion_history_cannot_skip_an_adjacent_tracking_checkpoint(configured):
    from src.runtime import starship_sixdof as dyn
    profile, body = configured[:2]
    first, sample, anchor, tracker, state, command = record(configured)
    assert verify(first, sample, profile, anchor)
    state = dyn.step(state, body, command, .1)
    second, sample, prior, tracker, state, command = record(configured, state=state, prior=first, tracker=tracker)
    assert verify(second, sample, profile, prior)
    state = dyn.step(state, body, command, .1)
    third, sample, _, *_ = record(configured, state=state, prior=second, tracker=tracker)
    with pytest.raises(_Invalid):
        verify(third, sample, profile, first)


@pytest.mark.parametrize("section", ("reference_tracking", "reference_tracking_control"))
def test_bounded_reference_never_certifies_nonlinear_plant_or_mission(configured, section):
    point, sample, prior, *_ = record(configured)
    point["navigation"][section]["nonlinear_plant_certified"] = True
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        verify(point, sample, configured[0], prior)


@pytest.mark.parametrize("landing", (False, True))
def test_first_historyless_fixture_keeps_actual_pose_at_same_clock(configured, landing):
    point, sample, prior, tracker, state, command = record(configured, landing=landing, historyless=True)
    item = verify(point, sample, configured[0], prior, checkpoint_index=0)
    assert item["requested_q_body_to_eci"] == pytest.approx(point["state"]["q_body_to_eci"], abs=1e-10)
    assert item["previous_requested_q_body_to_eci"] == pytest.approx(point["state"]["q_body_to_eci"], abs=1e-10)
    assert item["previous_raw_goal_q_body_to_eci"] == pytest.approx(point["state"]["q_body_to_eci"], abs=1e-10)
    assert item["interval_s"] == 0 and item["initialized_at_current_time"] is True
    assert item["reference_rate_eci_rad_s"] == item["reference_acceleration_eci_rad_s2"] == [0., 0., 0.]
    assert item["actual_body_rate_rad_s"] != item["reference_rate_body_rad_s"]
    assert item["actual_state_assigned"] is False
    from src.runtime import starship_sixdof as dyn
    state = dyn.step(state, configured[1], command, .1)
    second, sample, _, *_ = record(configured, landing=landing, prior=point, tracker=tracker, state=state)
    assert verify(second, sample, configured[0], point, checkpoint_index=1)["first_update"] is False


@pytest.mark.parametrize("mutation", ("index", "boolean_index", "unspecified_index", "past_time", "initial_rate", "pose", "zero_dt"))
def test_historyless_fallback_cannot_invent_history_or_replace_observed_pose(configured, mutation):
    point, sample, prior, *_ = record(configured, landing=True, historyless=True)
    item, index = point["navigation"]["reference_tracking"], 0
    if mutation == "index":
        index = 1
    elif mutation == "boolean_index":
        index = False
    elif mutation == "unspecified_index":
        index = None
    elif mutation == "past_time":
        item["previous_time_s"] -= .1
        item["previous_raw_goal_time_s"] -= .1
    elif mutation == "initial_rate":
        item["previous_reference_rate_eci_rad_s"][0] = .01
    elif mutation == "pose":
        item["previous_requested_q_body_to_eci"] = item["raw_goal_q_body_to_eci"]
        item["previous_raw_goal_q_body_to_eci"] = item["raw_goal_q_body_to_eci"]
    else:
        item["interval_s"] = .1
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        verify(point, sample, configured[0], prior, checkpoint_index=index)
