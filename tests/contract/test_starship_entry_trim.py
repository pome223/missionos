"""Wind-relative requested frames and finite entry prepositioning."""
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_sixdof as dyn, starship_physics as env
from src.runtime.starship_entry_trim import frozen_surface_model, prepare_entry_trim, entry_preferred
from src.runtime.starship_sixdof_mission import vehicle, _attitude, control, simulate
from src.runtime.starship_sixdof_verifier import verify_study


@pytest.fixture
def configured():
    profile = json.loads((Path(__file__).parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, payload_count=15)
    point = env.surface_state(25.9, -97.1, 120000., 100000., time_s=4000.)
    up, east, north = env.local_frame(point)
    flow = east
    axis = env.add(env.scale(flow, math.cos(math.radians(70.))), env.scale(up, math.sin(math.radians(70.))))
    nominal = _attitude(axis, env.scale(north, -1))
    state = dyn.State6DOF(point.time_s, point.r, env.add(point.v, env.scale(east, 7000.)),
        nominal, (0., 0., 0.), 100000., tuple(dyn.EngineState() for _ in body.engines),
        tuple(0. for _ in body.aero_panels))
    return profile, body, state, axis, flow, nominal


def test_vectorized_load_and_jacobian_match_original_plant(configured):
    _, body, state, *_ = configured
    observed = dyn.observe(state, body)
    model = frozen_surface_model(body, observed, [3, 4, 5, 6])
    angles = np.radians([31., -45., 12., -25.])
    moment, jac = model(angles, derivative=True)
    original = dyn.observe(replace(state, flap_angles_rad=(0., 0., 0., *map(float, angles))), body)
    assert moment == pytest.approx(original["aero_torque_body_nm"], abs=1e-7, rel=1e-11)
    for j in range(4):
        lo, hi = angles.copy(), angles.copy()
        lo[j] -= 1e-6
        hi[j] += 1e-6
        assert jac[:, j] == pytest.approx((model(hi)-model(lo))/2e-6, abs=1e-3, rel=1e-6)


def test_trim_preserves_requested_axis_and_does_not_assign_state(configured):
    pytest.importorskip("scipy")
    _, body, state, axis, flow, nominal = configured
    before = asdict(state)
    prepared = prepare_entry_trim(state, body, axis, flow, nominal)
    assert prepared["status"] == "prepared"
    q, _ = entry_preferred(axis, flow, nominal, prepared)
    assert dyn.rotate(q, (0., 0., 1.)) == pytest.approx(axis, abs=1e-12)
    predicted = dyn.observe(replace(state, q_body_to_eci=q,
        flap_angles_rad=tuple(prepared["trim_angles_rad"])), body)
    assert np.linalg.norm(predicted["aero_torque_body_nm"])/predicted["dynamic_pressure_pa"] <= 1e-4
    assert asdict(state) == before
    assert prepared["trim_is_execution"] is False


def test_prepositioning_still_consumes_time_and_uses_real_actuator_lag(configured):
    profile, body, state, *_ = configured
    # A bounded actuator request, not a static root or flight-success fixture.
    angles = (0., 0., 0., .5, .5, 0., 0.)
    before = asdict(state)
    command, diagnostic = control(state, body, state.q_body_to_eci, 0., 0, profile, use_flaps=True,
        development_fin_allocation=True, development_entry_preposition=True, trim_angles_rad=angles)
    following = dyn.step(state, body, command, .1)
    receipt = diagnostic["development_fin_allocation"]
    assert receipt["mode"] == "low_pressure_preposition"
    assert following.flap_angles_rad[3:] == pytest.approx(receipt["predicted_endpoint_angles_rad"], abs=1e-5)
    assert following.time_s > state.time_s
    assert following.flap_angles_rad != angles
    assert asdict(state) == before


def test_wind_trim_scope_is_rejected_by_default_verification(configured):
    profile, *_ = configured
    scope = {"release_limit": 26, "bounded_ship_flaps": True, "application": "wind_trim_state_return_v2"}
    run = simulate(profile, duration_s=.2, return_policy="trimmed_state_terminal_v4",
                   _development_return_qualification=scope)
    study = json.loads(json.dumps({"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
        "runs": [run], "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False}}))
    assert verify_study(study)["passed"] is False
    assert verify_study(study, expected_development_return_qualification=scope)["passed"] is True
    with pytest.raises(ValueError, match="invalid_development"):
        simulate(profile, duration_s=.2, return_policy="mass_state_terminal_v3",
                 _development_return_qualification=scope)
