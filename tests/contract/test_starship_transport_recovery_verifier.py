"""Opt-in request-law arithmetic fixtures; no physical trajectory integration."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_constrained_recovery_verifier as checker
from src.runtime import starship_actual_recovery_planning_verifier as context_checker
from src.runtime.starship_booster_recovery_verifier import _Invalid

F = runpy.run_path(str(Path(__file__).with_name("test_starship_boostback_transport_prepare.py")))


def setup():
    state, profile, catch, seed, reference, body, up, east = F["fixture"]()
    from src.runtime.starship_booster_catch import configuration

    return state, profile, configuration(catch), seed, reference, body, up, east


def saved(value):
    return F["shooting"]._saved(value)


def prepare_point(*, tail=False):
    from src.runtime.starship_booster_control import control_with_measured_tvc

    state, profile, catch, seed, reference, body, up, east = setup()
    previous = {
        "time_s": state.time_s,
        "navigation": {"target_q_body_to_eci": list(state.q_body_to_eci)},
    }
    state = replace(state, time_s=100.1)
    target = reference.target(
        up,
        east,
        state.q_body_to_eci,
        time_s=state.time_s,
        force_bridge=True,
        defer_geographic_reacquisition=True,
    )
    command, nav = control_with_measured_tvc(
        state, body, target, 0.0 if tail else 0.4, 0 if tail else 3, profile
    )
    nav["prepare_attitude_reference"] = deepcopy(reference.diagnostics)
    if tail:
        nav["prepare_shutdown_tail"] = {
            "started_at_s": 100.0,
            "duration_s": 4 * profile["actuators"]["throttle_tau_s"],
            "elapsed_s": 0.1,
            "main_commands_off": True,
            "actual_throttle_assigned": False,
        }
    phase = "recovery_entry_shutdown_tail" if tail else "recovery_powered_entry_prepare"
    point = saved(
        {
            "time_s": state.time_s,
            "state": asdict(state),
            "phase": phase,
            "command": asdict(command),
            "navigation": nav,
        }
    )
    events = [{"event": "powered_coast_attitude_prepared", "time_s": 100.0}]
    return point, saved(previous), events, profile


def test_default_and_opted_in_physical_configuration_are_independently_frozen():
    from src.runtime.starship_constrained_recovery import physical_guidance_configuration

    assert (
        physical_guidance_configuration(False) == checker._CONFIGURATIONS[checker._TRACKING_POLICY]
    )
    assert (
        physical_guidance_configuration(True) == checker._CONFIGURATIONS[checker._TRANSPORT_POLICY]
    )
    assert "transport_prepare_roll" not in checker._CONFIGURATIONS[checker._TRACKING_POLICY]


@pytest.mark.parametrize("tail", (False, True))
def test_parallel_transport_request_has_up_axis_zero_geographic_roll_and_finite_off_commands(tail):
    point, previous, events, profile = prepare_point(tail=tail)
    checker._transport_preparation(point, profile, checker._TRANSPORT_POLICY, previous, events)
    assert any(engine["throttle"] > 0 for engine in point["state"]["engine_states"][:3])
    if tail:
        assert all(engine["throttle"] == 0 for engine in point["command"]["engines"][:33])


@pytest.mark.parametrize(
    "mutation", ("policy", "roll", "mode", "axis", "clock", "tau", "throttle", "state_assign")
)
def test_transport_law_cannot_be_switched_or_disguised_as_achieved_shutdown(mutation):
    point, previous, events, profile = prepare_point(tail=True)
    policy = checker._TRANSPORT_POLICY
    if mutation == "policy":
        policy = checker._TRACKING_POLICY
    elif mutation == "roll":
        point["navigation"]["prepare_attitude_reference"]["roll_step_rad"] = 0.01
    elif mutation == "mode":
        point["navigation"]["prepare_attitude_reference"]["mode"] = "legacy_geographic"
    elif mutation == "axis":
        point["navigation"]["target_q_body_to_eci"][0] += 0.01
    elif mutation == "clock":
        point["navigation"]["prepare_attitude_reference"]["elapsed_s"] += 0.1
    elif mutation == "tau":
        point["navigation"]["prepare_shutdown_tail"]["duration_s"] *= 2
    elif mutation == "throttle":
        point["command"]["engines"][0].update(enabled=True, throttle=0.4)
    else:
        point["navigation"]["prepare_shutdown_tail"]["actual_throttle_assigned"] = True
    with pytest.raises(_Invalid):
        checker._transport_preparation(point, profile, policy, previous, events)


def test_short4_receipt_is_versioned_and_default3_contract_stays_unchanged(monkeypatch):
    shooting = F["shooting"]
    state, profile, catch, seed, reference, _, _ = F["_stub_loop"](monkeypatch)
    original = shooting._forecast_boostback
    monkeypatch.setattr(
        shooting,
        "_forecast_boostback",
        lambda *args, **kwargs: original(*args, **{**kwargs, "duration_s": 0.3}),
    )
    plan = shooting.refine_boostback_plan(
        state, profile, catch, seed, reference=reference, development_transport_prepare_roll=True
    )
    checker._short_prediction(
        plan, saved(asdict(state)), profile, catch, policy=checker._TRANSPORT_POLICY
    )
    assert plan["actual_dynamics_prediction"]["schema"] == shooting.TRANSPORT_FORECAST_SCHEMA
    with pytest.raises(_Invalid):
        checker._short_prediction(
            plan, saved(asdict(state)), profile, catch, policy=checker._TRACKING_POLICY
        )
    legacy = shooting.refine_boostback_plan(state, profile, catch, seed, reference=reference)
    checker._short_prediction(
        legacy, saved(asdict(state)), profile, catch, policy=checker._TRACKING_POLICY
    )


def test_short4_complete_tail_retains_actual_final_pose_and_four_tau_gate(monkeypatch):
    state, profile, catch, seed, reference, _, _ = F["_stub_loop"](monkeypatch)
    profile["actuators"]["throttle_tau_s"] = 0.2
    forecast = F["shooting"]._forecast_boostback(
        state,
        profile,
        catch,
        seed,
        reference=reference,
        duration_s=1.0,
        development_transport_prepare_roll=True,
    )
    checker._short_preparation_events(forecast, seed, saved(asdict(state)), profile, transport=True)
    assert forecast["duration_s"] == pytest.approx(0.8)
    for mutation in ("time", "rate", "state", "defer"):
        changed = deepcopy(forecast)
        event = changed["events"][-1]
        if mutation == "time":
            event["minimum_tail_s"] = 0.7
        elif mutation == "rate":
            event["actual_body_rate_rad_s"] = 0.003
        elif mutation == "state":
            event["state"]["propellant_kg"] -= 1
        else:
            event["geographic_roll_deferred"] = False
        with pytest.raises(_Invalid):
            checker._short_preparation_events(
                changed, seed, saved(asdict(state)), profile, transport=True
            )


def context2():
    from src.runtime import starship_actual_recovery_shooting as shooting
    from src.runtime.starship_constrained_recovery import physical_guidance_configuration

    state, profile, catch, seed, reference, _, _, _ = setup()
    config = physical_guidance_configuration(True)
    capsule = shooting.capture_context(
        state,
        profile,
        catch,
        config,
        phase="recovery_entry_shutdown_tail",
        start_time_s=99.0,
        deadline_s=1200.0,
        plan=seed,
        refreshed=True,
        burn_start_s=None,
        settle_start_s=99.5,
        previous_entry_axis_enu=None,
        entry_pretrim_prepared_at_s=None,
        next_preview_s=99.0,
        braking_preview=None,
        prior_command_reference={"quaternion": list(state.q_body_to_eci), "time_s": 100.0},
        conditioned_reference=reference,
        reference_tracker=None,
        prepare_tail_start_s=99.9,
    )
    return capsule, state, profile, catch, config


def test_context2_carries_exact_physical_law_and_tail_clock():
    capsule, state, profile, catch, config = context2()
    context_checker._validate_context(capsule, saved(asdict(state)), profile, catch, config)
    assert capsule["schema"] == context_checker.TRANSPORT_CONTEXT_SCHEMA
    assert capsule["physical_guidance_configuration"] == config


@pytest.mark.parametrize("mutation", ("schema", "config", "clock", "missing_tail", "v1_extra"))
def test_context2_cannot_restore_under_legacy_law_or_fabricated_tail_history(mutation):
    capsule, state, profile, catch, config = context2()
    if mutation == "schema":
        capsule["schema"] = context_checker.CONTEXT_SCHEMA
    elif mutation == "config":
        capsule["physical_guidance_configuration"]["powered_prepare_reference"][
            "shutdown_tail_tau_multiplier"
        ] = 3.0
    elif mutation == "clock":
        capsule["context"]["prepare_tail_start_s"] = state.time_s + 1
    elif mutation == "missing_tail":
        capsule["context"]["prepare_tail_start_s"] = None
    else:
        capsule["schema"] = context_checker.CONTEXT_SCHEMA
        del capsule["physical_guidance_configuration"]
        config = checker._CONFIGURATIONS[checker._TRACKING_POLICY]
        capsule["guidance_configuration_sha256"] = context_checker._digest(config)
    with pytest.raises(_Invalid):
        context_checker._validate_context(capsule, saved(asdict(state)), profile, catch, config)


@pytest.mark.parametrize("mutation", (None, "early", "rate", "tilt", "assignment", "old_policy"))
def test_main_tail_completion_preserves_final_measured_gate(monkeypatch, mutation):
    state, profile, catch, _, _, _, _, east = setup()
    begin = saved(asdict(state))
    final = saved(asdict(replace(state, time_s=101.4)))
    if mutation == "early":
        final["time_s"] = 101.3
    elif mutation == "rate":
        final["omega_body_rad_s"] = [0.003, 0.0, 0.0]
    elif mutation == "tilt":
        from src.runtime import starship_sixdof as dyn

        final["q_body_to_eci"] = saved(
            dyn.quaternion_multiply(dyn.axis_angle(east, 0.12), state.q_body_to_eci)
        )
    policy = checker._TRACKING_POLICY if mutation == "old_policy" else checker._TRANSPORT_POLICY
    event = {
        "event": "powered_coast_shutdown_tail_complete",
        "time_s": final["time_s"],
        "state": final,
        "duration_s": 1.4,
        "actual_tilt_deg": checker._local_tilt(final),
        "actual_body_rate_rad_s": checker._norm(final["omega_body_rad_s"]),
        "actual_throttle_assigned": mutation == "assignment",
    }
    events = [
        {"event": "constrained_return_start", "time_s": 100.0, "state": begin},
        {"event": "constrained_boostback_plan", "time_s": 100.0, "state": begin},
        {"event": "constrained_boostback_cutoff", "time_s": 100.0, "state": begin},
        {
            "event": "powered_coast_attitude_prepared",
            "time_s": 100.0,
            "state": begin,
            "tilt_deg": checker._local_tilt(begin),
        },
        event,
        {"event": "time_limit", "time_s": final["time_s"], "state": final},
    ]
    checkpoints = [
        {"time_s": 100.0, "state": begin, "phase": "recovery_entry_shutdown_tail"},
        {"time_s": final["time_s"], "state": final, "phase": "recovery_entry_coast"},
    ]
    # Isolate this event's physical predicate from separately tested plan,
    # cutoff and integration checks; these states are declarative fixtures.
    monkeypatch.setattr(checker, "_planning", lambda *args: None)
    monkeypatch.setattr(checker, "_cutoff_evidence", lambda *args: None)
    if mutation is None:
        checker._events(
            {"events": events},
            {"policy_id": policy},
            {"termination": "time_limit"},
            checkpoints,
            profile,
            catch,
            100.0,
            final["time_s"],
        )
    else:
        with pytest.raises(_Invalid):
            checker._events(
                {"events": events},
                {"policy_id": policy},
                {"termination": "time_limit"},
                checkpoints,
                profile,
                catch,
                100.0,
                final["time_s"],
            )
