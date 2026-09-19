from dataclasses import replace

import pytest

from src.runtime.grasp_recovery import GraspEvidence, GraspRecovery
from src.runtime.primitive_repair_adapter import RobotObservation


def observation(t=0.0, forces=(1.0, 0.0), held=False, **kw):
    g = GraspEvidence(
        t,
        forces,
        (0.002, 0.0, 0.0),
        (0.0, 0.0, -0.1),
        (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        True,
    )
    return replace(
        RobotObservation((0.0, 0.0, 0.0), (0.0, 0.0, 0.1), held, False, True, (False,), 0.03, g),
        **kw,
    )


def test_contact_recenter_and_close_only_share_verifier():
    o = observation()
    assert GraspRecovery(o, recenter=True).update(o) == ("running", (0.0005, 0.0, 0.0))
    assert GraspRecovery(o, recenter=False).update(o) == ("running", (0.0, 0.0, 0.0))
    for recenter in (True, False):
        r = GraspRecovery(o, recenter=recenter)
        for t in (0.0, 0.1, 0.2, 0.3, 0.4):
            assert r.update(observation(t, (1.0, 1.0), True))[0] == "running"
        assert r.update(observation(0.5, (1.0, 1.0), True))[0] == "qualified"


def test_force_without_contact_and_contact_without_force_do_not_qualify():
    for forces, held in [((1.0, 0.0), True), ((1.0, 1.0), False)]:
        r = GraspRecovery(observation(), recenter=False)
        for t in (0.0, 0.05, 0.1):
            assert r.update(observation(t, forces, held))[0] == "running"


def test_slip_resets_qualification_window():
    r = GraspRecovery(observation(), recenter=False)
    r.update(observation(0.0, (1.0, 1.0), True))
    o = observation(0.05, (1.0, 1.0), True)
    o = replace(o, grasp=replace(o.grasp, object_in_hand_m=(0.002, 0.0, -0.1)))
    assert r.update(o)[0] == "running"
    for t in (0.15, 0.25, 0.35, 0.45):
        assert r.update(replace(o, grasp=replace(o.grasp, time_seconds=t)))[0] == "running"
    assert r.update(replace(o, grasp=replace(o.grasp, time_seconds=0.55)))[0] == "qualified"


def test_missing_ticks_do_not_count_as_stability():
    o = observation(0.0, (1.0, 1.0), True)
    r = GraspRecovery(o, recenter=False)
    r.update(o)
    assert r.update(observation(0.5, (1.0, 1.0), True))[0] == "grasp_evidence_gap"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"grasp": None}, "grasp_evidence_missing"),
        ({"preserved": False}, "grasp_preservation_or_tilt_stop"),
        ({"hand_m": (0.006, 0.0, 0.1)}, "grasp_motion_limit"),
        ({"object_m": (0.006, 0.0, 0.0)}, "grasp_motion_limit"),
    ],
)
def test_missing_evidence_and_limits_fail_closed(mutation, reason):
    o = observation()
    assert GraspRecovery(o, recenter=True).update(replace(o, **mutation))[0] == reason


def test_no_contact_stale_and_bad_geometry_stop():
    o = observation(forces=(0.0, 0.0))
    assert GraspRecovery(o, recenter=True).update(o)[0] == "grasp_no_loaded_contact"
    o = observation()
    r = GraspRecovery(o, recenter=True)
    r.update(o)
    assert r.update(o)[0] == "grasp_evidence_stale"
    with pytest.raises(ValueError, match="rotation"):
        replace(o.grasp, object_in_hand_rotation=(0.0,) * 9).validate()
    o = replace(o, grasp=replace(o.grasp, centering_error_m=(0.006, 0.0, 0.0)))
    assert GraspRecovery(o, recenter=True).update(o)[0] == "grasp_centering_out_of_bounds"
