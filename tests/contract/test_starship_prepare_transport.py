"""Pure target geometry / context contracts; no physical integration here."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import configuration
from src.runtime.starship_constrained_recovery import CONFIG, physical_guidance_configuration
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def distance(a, b):
    return 2*math.acos(min(1., abs(sum(x*y for x, y in zip(a, b)))))


def source_axis(epsilon=0.):
    angle = math.radians(107.)
    raw = (-math.sin(angle), epsilon, math.cos(angle))
    norm = math.hypot(*raw)
    return tuple(value/norm for value in raw)


def fixture(transported):
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 120000., time_s=100.)
    up, east, _ = env.local_frame(point)
    q = tuple(float(x) for x in _attitude(up, east))
    state = dyn.State6DOF(100., point.r, point.v, q, (0., 0., 0.), 60000.,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    frame = ConditionedGeographicFrame(q, 100., maximum_roll_rate_rad_s=.03/.28)
    cfg = physical_guidance_configuration(transported)
    snapshot = shooting.capture_context(state, profile, catch, cfg, phase="recovery_powered_entry_prepare",
        start_time_s=90., deadline_s=1290.,
        plan={"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., 0.],
              "bank_heading_enu": [1., 0., 0.], "bank_sign": -1, "prediction_is_execution": False},
        refreshed=True, burn_start_s=92., settle_start_s=95., previous_entry_axis_enu=None,
        entry_pretrim_prepared_at_s=None, next_preview_s=90., braking_preview=None,
        prior_command_reference={"quaternion": list(q), "time_s": 99.9}, conditioned_reference=frame,
        reference_tracker=None, **({"prepare_tail_start_s": None} if transported else {}))
    return profile, catch, cfg, snapshot


def test_default_and_explicit_false_geographic_behavior_remain_bit_identical():
    before = _attitude(source_axis(), (1., 0., 0.))
    a = ConditionedGeographicFrame(before, 0., maximum_roll_rate_rad_s=.03/.28)
    b = deepcopy(a)
    preferred = _attitude((0., 0., 1.), (1., 0., 0.))
    qa = a.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=.1)
    qb = b.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=.1, defer_geographic_reacquisition=False)
    assert qa == qb == preferred
    assert a.diagnostics == b.diagnostics
    assert "geographic_roll_deferred" not in a.diagnostics


@pytest.mark.parametrize("epsilon", [-.002, 0., .002])
def test_transport_up_uses_minimal_axis_turn_without_near_pi_geographic_frame_flip(epsilon):
    axis = source_axis(epsilon)
    before = _attitude(axis, (1., 0., 0.))
    preferred = _attitude((0., 0., 1.), (1., 0., 0.))
    frame = ConditionedGeographicFrame(before, 0., maximum_roll_rate_rad_s=.03/.28)
    target = frame.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=.1,
        force_bridge=True, defer_geographic_reacquisition=True)
    assert dyn.rotate(target, (0., 0., 1.)) == pytest.approx((0., 0., 1.), abs=1e-12)
    assert distance(before, target) == pytest.approx(math.acos(axis[2]), abs=1e-12)
    assert distance(before, preferred) > math.radians(170.)
    assert frame.diagnostics["roll_step_rad"] == 0.
    assert frame.diagnostics["mode"] == "transport_deferred_geographic_roll"
    assert frame.diagnostics["geographic_roll_deferred"] is True
    assert frame.bridging is True


def test_deferred_roll_holds_across_command_off_tail_then_reacquires_under_existing_bound():
    before = _attitude(source_axis(), (1., 0., 0.))
    frame = ConditionedGeographicFrame(before, 0., maximum_roll_rate_rad_s=.03/.28)
    preferred = _attitude((0., 0., 1.), (1., 0., 0.))
    target = frame.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=.1,
        force_bridge=True, defer_geographic_reacquisition=True)
    transported = target
    for index in range(2, 17):
        target = frame.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=index*.1,
            force_bridge=True, defer_geographic_reacquisition=True)
        assert distance(target, transported) < 1e-7
        assert frame.diagnostics["roll_step_rad"] == 0.
        assert type(frame.diagnostics["antipodal_fallback"]) is bool
        json.dumps(frame.diagnostics, allow_nan=False)
    target = frame.target((0., 0., 1.), (1., 0., 0.), preferred, time_s=1.7)
    assert frame.diagnostics["mode"] == "bounded_roll_reacquisition"
    assert abs(frame.diagnostics["roll_step_rad"]) <= .03/.28*.1+1e-12
    assert distance(target, transported) <= .03/.28*.1+1e-12
    assert dyn.rotate(target, (0., 0., 1.)) == pytest.approx((0., 0., 1.), abs=1e-12)


@pytest.mark.parametrize("value,bridge", [(True, False), (1, True), (None, True)])
def test_bad_defer_mode_rejected_before_mutating_reference(value, bridge):
    frame = ConditionedGeographicFrame((1., 0., 0., 0.), 0., maximum_roll_rate_rad_s=.1)
    with pytest.raises(ValueError, match="explicit forced bridge"):
        frame.target((0., 0., 1.), (1., 0., 0.), (1., 0., 0., 0.), time_s=.1,
            force_bridge=bridge, defer_geographic_reacquisition=value)
    assert frame.frame.quaternion == (1., 0., 0., 0.)
    assert frame.time_s == 0.


def test_old_context_and_configuration_shape_remain_default_v1():
    profile, catch, cfg, snapshot = fixture(False)
    assert cfg == CONFIG
    assert snapshot["schema"] == shooting.CONTEXT_SCHEMA
    assert "physical_guidance_configuration" not in snapshot
    assert "prepare_tail_start_s" not in snapshot["context"]
    shooting.validate_context(snapshot, profile, catch, cfg)


def test_transport_context_v2_retains_physical_configuration_and_original_cut_clock():
    profile, catch, cfg, snapshot = fixture(True)
    assert snapshot["schema"] == shooting.TRANSPORT_CONTEXT_SCHEMA
    assert snapshot["physical_guidance_configuration"] == cfg
    assert snapshot["guidance_configuration_sha256"] == shooting.digest(cfg)
    snapshot["context"].update(phase="recovery_entry_shutdown_tail", prepare_tail_start_s=99.)
    shooting.validate_context(snapshot, profile, catch, cfg)
    assert snapshot["context"]["settle_start_s"] == 95.
    assert shooting.physical_configuration_from_context(snapshot)["transport_prepare_roll"] is True
    with pytest.raises(ValueError):
        shooting.validate_context(snapshot, profile, catch, physical_guidance_configuration(False))
    missing = deepcopy(snapshot)
    missing["context"]["prepare_tail_start_s"] = None
    with pytest.raises(ValueError, match="original_cut_and_ready"):
        shooting.validate_context(missing, profile, catch, cfg)


def test_v2_capsule_cannot_substitute_angular_gains_or_an_unknown_physical_law():
    profile, catch, cfg, snapshot = fixture(True)
    snapshot["physical_guidance_configuration"]["rate_settle_limit_rad_s"] = .1
    snapshot["guidance_configuration_sha256"] = shooting.digest(snapshot["physical_guidance_configuration"])
    with pytest.raises(ValueError, match="physical_configuration"):
        shooting.validate_context(snapshot, profile, catch, cfg)


def test_new_optimizer_requires_matching_v10_baseline_inputs():
    profile, catch, _, snapshot = fixture(True)
    calls = []
    with pytest.raises(ValueError, match="matching_v10_reference_inputs"):
        shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
            artifact_sink=lambda *args: calls.append(args))
    assert not calls


def test_mixed_new_transport_and_old_optimizer_fails_before_any_dynamics(monkeypatch):
    profile, catch, _, snapshot = fixture(True)
    from src.runtime.starship_constrained_recovery import simulate_constrained_recovery
    monkeypatch.setattr(dyn, "observe", lambda *args, **kwargs: pytest.fail("invalid mixed mode must not observe dynamics"))
    with pytest.raises(ValueError, match="matching_v10_reference_inputs"):
        simulate_constrained_recovery(profile, snapshot["state"], catch, development_actual_planning=True,
            development_transport_prepare_roll=True, actual_planning_artifact_sink=lambda *args: None)
