"""Target-frame continuity checks independent of landing outcome."""
import math

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ParallelTransportFrame, ConditionedGeographicFrame
from src.runtime.starship_sixdof_mission import _attitude


def distance(a, b):
    return 2*math.acos(min(1., abs(sum(x*y for x, y in zip(a, b)))))


def test_crossing_fixed_roll_reference_preserves_frame_continuity():
    # The axis passes through -north; the former projection reverses its sign.
    angles = [-.02+i*.0001 for i in range(401)]
    axes = [(math.sin(a), -math.cos(a), 0.) for a in angles]
    legacy = [_attitude(axis, (0., -1., 0.)) for axis in axes]
    assert max(distance(a, b) for a, b in zip(legacy, legacy[1:])) > 3.
    frame = ParallelTransportFrame(legacy[0])
    targets = [frame.target(axis) for axis in axes]
    assert max(distance(a, b) for a, b in zip(targets, targets[1:])) < .000101
    for axis, target in zip(axes, targets):
        assert dyn.rotate(target, (0., 0., 1.)) == pytest.approx(axis, abs=1e-12)


def test_constant_axis_has_no_artificial_roll_and_needs_no_geographic_reference():
    initial = dyn.axis_angle((0., 0., 1.), 1.3)
    frame = ParallelTransportFrame(initial)
    for _ in range(30):
        assert frame.target((0., 0., 1.)) == pytest.approx(initial)
        assert frame.diagnostics["axis_step_rad"] == 0


def test_minimal_transport_does_not_add_twist_about_axis():
    frame = ParallelTransportFrame((1., 0., 0., 0.))
    angle = .37
    target = frame.target((math.sin(angle), 0., math.cos(angle)))
    assert target == pytest.approx(dyn.axis_angle((0., 1., 0.), angle))
    assert frame.diagnostics["axis_step_rad"] == pytest.approx(angle)
    assert not frame.diagnostics["antipodal_fallback"]


@pytest.mark.parametrize("epsilon", [1e-4, 1e-8, 1e-11])
def test_nearly_antipodal_axis_is_finite_and_tracks_actual_requested_axis(epsilon):
    frame = ParallelTransportFrame((1., 0., 0., 0.))
    axis = (math.sin(epsilon), 0., -math.cos(epsilon))
    target = frame.target(axis)
    assert dyn.rotate(target, (0., 0., 1.)) == pytest.approx(axis, abs=1e-12)
    assert not frame.diagnostics["antipodal_fallback"]


def test_exact_antipodal_command_exposes_ambiguity_without_claiming_smoothness():
    frame = ParallelTransportFrame((1., 0., 0., 0.))
    target = frame.target((0., 0., -1.))
    assert dyn.rotate(target, (1., 0., 0.)) == pytest.approx((1., 0., 0.))
    assert dyn.rotate(target, (0., 0., 1.)) == pytest.approx((0., 0., -1.))
    assert frame.diagnostics["axis_step_rad"] == pytest.approx(math.pi)
    assert frame.diagnostics["antipodal_fallback"]


def test_axis_round_trip_recovers_initial_frame_up_to_quaternion_sign():
    initial = _attitude((.1, .3, .7), (0., 1., 0.))
    frame = ParallelTransportFrame(initial)
    axes = [(math.sin(a), 0., math.cos(a)) for a in (.1, .3, .7, .3, .1)]
    for axis in axes:
        frame.target(axis)
    final = frame.target(dyn.rotate(initial, (0., 0., 1.)))
    assert distance(initial, final) < 1e-7


@pytest.mark.parametrize("axis", [(0., 0., 0.), (float("nan"), 0., 1.),
                                  (float("inf"), 0., 1.), (True, 0., 1.), (0., 1.)])
def test_invalid_direction_does_not_mutate_target(axis):
    frame = ParallelTransportFrame((1., 0., 0., 0.))
    with pytest.raises(ValueError):
        frame.target(axis)
    assert frame.quaternion == (1., 0., 0., 0.)


def test_conditioned_frame_preserves_legacy_target_when_projection_is_well_conditioned():
    initial = _attitude((0., 0., 1.), (0., 1., 0.))
    frame = ConditionedGeographicFrame(initial, 0., maximum_roll_rate_rad_s=.1)
    for t, a in enumerate((.1, .3, .7, .4, -.4), 1):
        axis = (math.sin(a), 0., math.cos(a))
        legacy = _attitude(axis, (0., 1., 0.))
        assert frame.target(axis, (0., 1., 0.), legacy, time_s=float(t)) == legacy
        assert frame.diagnostics["mode"] == "legacy_geographic"


def test_conditioned_frame_transports_through_singularity_then_reacquires_with_bounded_roll():
    angles = [-.2+i*.001 for i in range(401)]
    axes = [(math.sin(a), -math.cos(a), 0.) for a in angles]
    frame = ConditionedGeographicFrame(_attitude(axes[0], (0., -1., 0.)), 0., maximum_roll_rate_rad_s=.1)
    previous, modes = None, set()
    for index, axis in enumerate(axes):
        legacy = _attitude(axis, (0., -1., 0.))
        target = frame.target(axis, (0., -1., 0.), legacy, time_s=index*.01)
        modes.add(frame.diagnostics["mode"])
        if previous is not None:
            assert distance(previous, target) <= .002001
        assert abs(frame.diagnostics["roll_step_rad"]) <= .001+1e-12
        previous = target
    assert {"legacy_geographic", "transport_ill_conditioned", "bounded_roll_reacquisition"} <= modes
    legacy = _attitude(axes[-1], (0., -1., 0.))
    for index in range(1, 401):
        target = frame.target(axes[-1], (0., -1., 0.), legacy, time_s=4.+index*.1)
        assert abs(frame.diagnostics["roll_step_rad"]) <= .01+1e-12
    assert target == legacy
    assert not frame.bridging


def test_terminal_geographic_reference_reversal_does_not_inject_instant_roll():
    axis = (0., 0., 1.)
    initial = _attitude(axis, (0., -1., 0.))
    frame = ConditionedGeographicFrame(initial, 10., maximum_roll_rate_rad_s=.1)
    legacy = _attitude(axis, (0., 1., 0.))
    # An event transition at the same physical time has no roll-time budget.
    target = frame.target(axis, (0., 1., 0.), legacy, time_s=10., force_bridge=True)
    assert distance(target, initial) < 1e-12
    target = frame.target(axis, (0., 1., 0.), legacy, time_s=10.1)
    assert distance(target, initial) == pytest.approx(.01)


@pytest.mark.parametrize("time", [-1., float("nan"), float("inf")])
def test_conditioned_frame_rejects_bad_time_without_mutation(time):
    frame = ConditionedGeographicFrame((1., 0., 0., 0.), 0., maximum_roll_rate_rad_s=.1)
    with pytest.raises(ValueError, match="time"):
        frame.target((0., 0., 1.), (1., 0., 0.), (1., 0., 0., 0.), time_s=time)
    assert frame.frame.quaternion == (1., 0., 0., 0.)
