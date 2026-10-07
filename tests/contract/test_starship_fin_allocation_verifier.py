"""Independent moment-priority arithmetic, opt-in scope and finite bounds."""
from copy import deepcopy
from dataclasses import asdict
import inspect
import json
from pathlib import Path

import pytest

from src.runtime import starship_booster_recovery_verifier as checker

ROOT = Path(__file__).resolve().parents[2]
PRIORITY = "finite_moment_priority_fins_v1"


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(scope="module")
def configured():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              40000., time_s=180.)
    up, east, _ = env.local_frame(point)
    q = dyn.quaternion_multiply(_attitude(up, east), dyn.axis_angle((0., 1., 0.), .45))
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.scale(up, -1000.)), q,
        (0., 0., 0.), 200000., tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    return profile, body, state


def allocation_records(configured, policy):
    from src.runtime import starship_fin_allocation as producer, starship_sixdof as dyn
    from src.runtime.starship_sixdof_booster import _sample_booster
    profile, body, state = configured
    observed = dyn.observe(state, body)
    trim = [0., 0., 0., .2, -.1, .15]
    angles, remaining, receipt = producer.allocate_fins(state, body, [0., 200000., -30000.], observed,
        profile, interval_s=.1, trim_angles_rad=trim, policy_id=policy)
    command = dyn.Command6DOF(tuple(dyn.EngineCommand(False, 0.) for _ in body.engines), angles)
    navigation = {"development_fin_allocation": receipt, "requested_torque_body_nm": remaining.tolist(),
                  "predicted_trim_flap_angles_rad": trim}
    checkpoint = {"time_s": state.time_s, "phase": "recovery_entry_coast", "state": asdict(state),
                  "command": asdict(command), "navigation": navigation}
    sample = _sample_booster(state, body, "recovery_entry_coast", command, navigation)
    return saved(checkpoint), saved(sample)


@pytest.fixture(scope="module")
def priority_record(configured):
    return allocation_records(configured, PRIORITY)


def verify(checkpoint, sample, profile, expected=PRIORITY):
    checker._fin_allocation(checkpoint, sample, profile, True, .5, expected_policy=expected)


def test_moment_priority_receipt_requires_explicit_expected_policy(configured, priority_record):
    profile, _, _ = configured
    point, sample = priority_record
    verify(point, sample, profile)
    with pytest.raises(checker._Invalid):
        checker._fin_allocation(point, sample, profile, True, .5)
    with pytest.raises(checker._Invalid):
        verify(point, sample, profile, "unknown")


def test_legacy_default_contract_stays_unchanged(configured):
    profile, _, _ = configured
    point, sample = allocation_records(configured, "finite_regularized_fins_v1")
    checker._fin_allocation(point, sample, profile, True, .5)
    with pytest.raises(checker._Invalid):
        verify(point, sample, profile)


@pytest.mark.parametrize("field", ("primary_optimum_delta_rad", "primary_optimum_objective", "primary_objective",
    "primary_half_objective_gradient", "secondary_trim_objective", "secondary_image_residual",
    "allocation_priority", "maximum_secondary_faces", "command_angles_rad", "production_policy_admitted"))
def test_changed_priority_receipt_is_rejected(configured, priority_record, field):
    profile, _, _ = configured
    point, sample = deepcopy(priority_record)
    receipt = point["navigation"]["development_fin_allocation"]
    if field == "allocation_priority":
        receipt[field] = "weighted_tradeoff"
    elif field == "production_policy_admitted":
        receipt[field] = True
    elif isinstance(receipt[field], list):
        receipt[field][0] += .1
    else:
        receipt[field] += 1.
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(checker._Invalid):
        verify(point, sample, profile)


@pytest.mark.parametrize("matrix,image,prior,lower,upper,expected,cost", (
    ([[1., 1., 0.], [0., 0., 0.], [0., 0., 0.]], [.1, 0., 0.], [0., 0., .2],
     [-.5]*3, [.5]*3, [.05, .05, .2], .005),
    ([[1., 1., 0.], [0., 0., 0.], [0., 0., 0.]], [.6, 0., 0.], [0., 0., .3],
     [0., 0., -.5], [.1, 1., .5], [.1, .5, .3], .26),
    ([[1., 1., 0.], [2., 2., 0.], [0., 0., 0.]], [.1, .2, 0.], [0., 0., .2],
     [-.5]*3, [.5]*3, [.05, .05, .2], .005),
    ([[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]], [.1, -.1, .2], [0., 0., 0.],
     [-.5]*3, [.5]*3, [.1, -.1, .2], .06),
))
def test_independent_secondary_projection_handles_nullspace_rank_and_active_faces(
        matrix, image, prior, lower, upper, expected, cost):
    result, actual_cost = checker._closest_trim_secondary(matrix, image, lower, upper, prior, 1.)
    assert result == pytest.approx(expected, abs=1e-9)
    assert actual_cost == pytest.approx(cost, abs=1e-9)


def test_inconsistent_primary_image_has_no_trim_projection():
    with pytest.raises(checker._Invalid):
        checker._closest_trim_secondary([[1., 1.], [2., 2.], [0., 0.]], [.1, .3, 0.],
                                         [-.5, -.5], [.5, .5], [0., 0.], 1.)


def test_checker_keeps_elementary_independence_from_producer_and_integrator():
    source = inspect.getsource(checker)
    for forbidden in ("starship_fin_allocation import", "starship_sixdof import", "import numpy", "scipy"):
        assert forbidden not in source
