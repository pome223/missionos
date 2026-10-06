"""Frozen-probe arithmetic and observed v7 preparation; no full-return trials."""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path

import pytest

from src.runtime import starship_entry_pretrim_verifier as verifier
from src.runtime import starship_constrained_recovery_verifier as recovery_checker
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
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 70000., time_s=180.)
    up, east, _ = env.local_frame(point)
    q = _attitude(up, east)
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.scale(up, -600.)), q,
        dyn.inverse_rotate(q, (0., 0., env.EARTH_ROTATION_RAD_S)), 200000.,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    return profile, body, state


def record(configured, trim=None, state=None, reference_kind="governed_current_flow_trim"):
    from src.runtime.starship_booster_control import control_coast_stopping_distance
    from src.runtime.starship_entry_pretrim import prepare_entry_fins
    from src.runtime.starship_sixdof_booster import _sample_booster
    profile, body, original = configured
    state = original if state is None else state
    trim = [0., 0., 0., .01, -.01, .005] if trim is None else trim
    base, diagnostic = control_coast_stopping_distance(state, body, state.q_body_to_eci, profile, control_interval_s=.1)
    command, receipt = prepare_entry_fins(state, body, base, diagnostic, profile, trim, interval_s=.1)
    nav = {**diagnostic, "entry_pretrim": receipt, "predicted_trim_flap_angles_rad": trim,
           "governed_entry_axis_enu": [0., 0., 1.], "entry_pretrim_reference_kind": reference_kind,
           "entry_pretrim_prepared_at_s": state.time_s if receipt["prepared"] and reference_kind == "governed_current_flow_trim" else None}
    point = {"time_s": state.time_s, "state": asdict(state), "phase": "recovery_entry_coast", "command": asdict(command), "navigation": nav}
    sample = _sample_booster(state, body, "recovery_entry_coast", command, nav)
    return saved(point), saved(sample), command


def test_accepted_finite_request_is_not_predicted_readiness(configured):
    profile, _, _ = configured
    point, sample, _ = record(configured)
    assert point["navigation"]["entry_pretrim"]["status"] == "accepted"
    assert verifier.verify_pretrim_checkpoint(point, sample, profile,
        expected_trim_angles=point["navigation"]["predicted_trim_flap_angles_rad"], remaining_s=.5) is False
    latch, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)
    assert latch is None and selected == "finite_regularized_fins_v1"


def test_real_finite_motion_is_required_before_measured_latch(configured):
    from src.runtime import starship_sixdof as dyn
    profile, body, state = configured
    point, sample, command = record(configured)
    for _ in range(8):
        state = dyn.step(state, body, command, .1)
        point, sample, command = record(configured, state=state)
    assert point["navigation"]["entry_pretrim"]["prepared"] is True
    assert verifier.verify_pretrim_checkpoint(point, sample, profile,
        expected_trim_angles=point["navigation"]["predicted_trim_flap_angles_rad"], remaining_s=.5)
    latch, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)
    assert latch == state.time_s and selected == "finite_moment_priority_fins_v1"
    assert point["state"]["flap_angles_rad"] != [0.]*6


def test_future_reference_can_prepare_commands_but_never_current_flow_latch(configured):
    profile, _, state = configured
    state = replace(state, flap_angles_rad=(0., 0., 0., .01, -.01, .005))
    point, sample, _ = record(configured, state=state)
    # Remove the governed-current-flow provenance, without altering physical
    # fins or the accepted helper. The driver's descriptor is now future-only.
    del point["navigation"]["governed_entry_axis_enu"]
    point["navigation"]["entry_pretrim_reference_kind"] = "future_descending_entry_prediction"
    point["navigation"]["entry_pretrim_prepared_at_s"] = None
    sample["controller"] = deepcopy(point["navigation"])
    latch, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, 179., .5)
    assert latch is None and selected == "finite_regularized_fins_v1"


@pytest.mark.parametrize("mutation", ("prepared", "pressure", "interval", "actual", "target", "probe_endpoint",
    "probe_fraction", "headroom", "demand", "capacity", "groups", "counter", "authority", "latch"))
def test_pretrim_record_mutations_are_rejected(configured, mutation):
    profile, _, _ = configured
    point, sample, _ = record(configured)
    item = point["navigation"]["entry_pretrim"]
    if mutation == "prepared":
        item["prepared"] = True
    elif mutation == "pressure":
        item["dynamic_pressure_pa"] += 1.
    elif mutation == "interval":
        item["interval_s"] = .25
    elif mutation == "actual":
        item["actual_flap_angles_rad"][-1] += .1
    elif mutation == "target":
        item["declared_trim_angles_rad"][-1] += .1
    elif mutation == "probe_endpoint":
        item["probes"][0]["predicted_flap_angles_rad"][-1] += .1
    elif mutation == "probe_fraction":
        item["probes"][0]["fraction"] = .1
    elif mutation == "headroom":
        item["probes"][0]["rcs_headroom_margin_nm"][0] += 1.
    elif mutation == "demand":
        item["base_rcs_torque_demand_body_nm"][0] += 1.
    elif mutation == "capacity":
        item["rcs_signed_capacity_nm"][0][0] += 1.
    elif mutation == "groups":
        item["rcs_groups"][0]["available_indices"] = []
    elif mutation == "counter":
        item["probe_attempted_count"] += 1
    elif mutation == "authority":
        item["arrival_admitted"] = True
    else:
        point["navigation"]["entry_pretrim_prepared_at_s"] = point["time_s"]
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)


def test_single_missing_rcs_mate_is_a_real_deferred_receipt(configured):
    profile, _, state = configured
    engines = list(state.engine_states)
    engines[37] = replace(engines[37], available=False)
    state = replace(state, engine_states=tuple(engines))
    point, sample, _ = record(configured, state=state)
    item = point["navigation"]["entry_pretrim"]
    assert item["reason"] == "unsupported_or_unbalanced_rcs_group" and item["prepared"] is False
    assert item["probe_attempted_count"] == 0
    assert verifier.verify_pretrim_checkpoint(point, sample, profile,
        expected_trim_angles=point["navigation"]["predicted_trim_flap_angles_rad"], remaining_s=.5) is False


def test_high_q_priority_requires_prior_proven_latch_and_missing_preparation_uses_legacy(configured):
    profile, _, state = configured
    point, sample, _ = record(configured, trim=[0.]*6)
    assert point["navigation"]["entry_pretrim"]["prepared"]
    latch, _ = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)
    point = deepcopy(point)
    point["state"]["time_s"] += .1
    point["time_s"] = point["state"]["time_s"]
    del point["navigation"]["entry_pretrim"]
    point["navigation"]["entry_pretrim_prepared_at_s"] = latch
    sample = deepcopy(sample)
    sample.update(time_s=point["time_s"], dynamic_pressure_pa=101., controller=deepcopy(point["navigation"]))
    carried, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, latch, .5)
    assert carried == latch and selected == "finite_moment_priority_fins_v1"
    with pytest.raises(_Invalid):
        recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)
    point["navigation"]["entry_pretrim_prepared_at_s"] = None
    _, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)
    assert selected == "finite_regularized_fins_v1"


def test_latest_low_q_observation_clears_older_preparation_and_rejects_stale_timestamp(configured):
    profile, _, _ = configured
    point, sample, _ = record(configured)
    latch, selected = recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, 179., .5)
    assert latch is None and selected == "finite_regularized_fins_v1"
    point["navigation"]["entry_pretrim_prepared_at_s"] = 179.
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, 179., .5)


def test_missing_driver_reference_cannot_be_replaced_by_unattached_accepted_goal(configured):
    profile, _, _ = configured
    point, sample, _ = record(configured)
    del point["navigation"]["predicted_trim_flap_angles_rad"]
    point["navigation"]["entry_pretrim_reference_kind"] = "missing_reference"
    sample["controller"] = deepcopy(point["navigation"])
    with pytest.raises(_Invalid):
        recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._PRETRIM_POLICY, None, .5)


def test_zero_signed_authority_with_both_mates_unavailable_is_deferred(configured):
    profile, _, state = configured
    engines = list(state.engine_states)
    for index in (37, 38):
        engines[index] = replace(engines[index], available=False)
    point, sample, _ = record(configured, state=replace(state, engine_states=tuple(engines)))
    assert point["navigation"]["entry_pretrim"]["reason"] == "unavailable_bidirectional_rcs"
    assert verifier.verify_pretrim_checkpoint(point, sample, profile,
        expected_trim_angles=point["navigation"]["predicted_trim_flap_angles_rad"], remaining_s=.5) is False


def test_prepared_flag_never_escapes_v7_scope(configured):
    profile, _, _ = configured
    point, sample, _ = record(configured)
    with pytest.raises(_Invalid):
        recovery_checker._pretrim_protocol(point, sample, profile, recovery_checker._MOMENT_POLICY, None, .5)


def test_pretrim_checker_imports_no_producer_or_integrator():
    source = inspect.getsource(verifier)
    for forbidden in ("starship_entry_pretrim import", "starship_sixdof import", "starship_fin_allocation import", "import numpy", "scipy"):
        assert forbidden not in source
