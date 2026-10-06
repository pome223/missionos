"""An independent DOP853 coast reference catches propagated-state corruption."""

from copy import deepcopy
import math

import pytest

pytest.importorskip("scipy")
import numpy as np

from scripts.check_starship_numerics import check_run, propagate_reference
from src.runtime.starship_physics import Control, State3D, Vehicle, observe, step


def test_dop853_independently_closes_analytic_two_body_circle():
    r, mu = 6_778_137.0, 3.986004418e14
    initial = [r, 0, 0, 0, math.sqrt(mu/r), 0]
    period = 2*math.pi*math.sqrt(r**3/mu)
    reference, _ = propagate_reference(initial, [0, period], mass_kg=1700,
                                        j2=False, atmosphere=False, rtol=1e-13)
    assert np.linalg.norm(reference[-1, :3]-initial[:3]) < 0.0001
    assert np.linalg.norm(reference[-1, 3:]-initial[3:]) < 1e-7


def _short_coast():
    r, mu = 6_778_137.0, 3.986004418e14
    state = State3D(12, (r, 0, 0), (0, math.sqrt(mu/r)*0.8, math.sqrt(mu/r)*0.6), 0)
    vehicle = Vehicle(1700, 20, 2.2, 0, 1, 0)
    trace = []
    for i in range(31):
        row = observe(state, vehicle)
        row.update({f"{axis}_m": state.r[j] for j, axis in enumerate("xyz")})
        row.update({f"v{axis}_mps": state.v[j] for j, axis in enumerate("xyz")})
        row.update({"phase": "passive_satellite_coast", "propellant_mass_kg": 0})
        trace.append(row)
        if i < 30:
            state = step(state, vehicle, Control(), 10)
    return {"scenario": "test", "profile": {"satellite_mass_kg": 1700},
            "traces": {"satellites": {"test-satellite": trace}}}


def test_independent_rhs_and_integrator_match_3d_j2_rotating_drag_coast():
    result = check_run(_short_coast())
    assert result["numerical_agreement_verified"]
    assert result["comparisons"][0]["sample_count"] == 31
    assert result["claim_boundary"]["starship_vehicle_validated"] is False


def test_crosscheck_detects_changed_recorded_velocity():
    run = deepcopy(_short_coast())
    run["traces"]["satellites"]["test-satellite"][10]["vz_mps"] += 2
    result = check_run(run)
    assert not result["numerical_agreement_verified"]
    assert result["comparisons"][0]["max_velocity_difference_mps"] > 1.9


def test_nonmonotonic_time_and_low_atmosphere_are_refused():
    with pytest.raises(ValueError, match="strictly increasing"):
        propagate_reference([6_778_137, 0, 0, 0, 7000, 0], [0, 0], mass_kg=1700)
    with pytest.raises(ValueError, match="86 km"):
        propagate_reference([6_388_137, 0, 0, 0, 7000, 0], [0, 1], mass_kg=1700)
