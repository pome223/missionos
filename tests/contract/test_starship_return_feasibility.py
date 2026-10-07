"""Fresh quantitative margins and an independent verifier, not flight fixtures."""
from copy import deepcopy
from dataclasses import replace
import inspect
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_return_feasibility as tool, starship_return_feasibility_verifier as independent
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle


def example(fuel=100000.):
    profile = json.loads((Path(__file__).parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, payload_count=0)
    radius = 6378137.+300000.
    state = dyn.State6DOF(1000., (radius, 0., 0.), (0., math.sqrt(3.986004418e14/radius), 0.),
        (math.sqrt(.5), -math.sqrt(.5), 0., 0.), (.001, 0., 0.), fuel,
        tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    observed = tool.evaluate(profile, state, 26, fuel)
    certificate = {"qualification_complete": True, "normalized_profile_sha256": observed["inputs"]["normalized_profile_sha256"],
        "backend": observed["inputs"]["backend"], "maximum_return_consumption_kg": 50000.,
        "maximum_reference_deorbit_fuel_kg": 10000.}
    proof = {**tool.calculate(observed["inputs"], certificate), "certificate_sha256": "explicit_math_fixture"}
    return profile, state, certificate, proof


def checked(certificate, proof):
    return independent.verify(proof, certificate, "explicit_math_fixture", time_s=proof["time_s"],
        fuel_kg=proof["inputs"]["fuel_observed_kg"], released_count=26)


def test_return_fuel_margin_is_recomputed_independently():
    _, _, certificate, proof = example()
    assert proof["return_fuel_margin_kg"] == 20900.
    assert proof["return_admitted"] is True
    assert checked(certificate, proof)["passed"] is True


def test_insufficient_return_fuel_can_only_admit_bounded_coast():
    _, _, certificate, proof = example(24000.)
    assert proof["return_admitted"] is False
    assert proof["bounded_coast_admitted"] is True
    assert proof["maximum_coast_s"] == 30.
    assert checked(certificate, proof)["passed"] is True
    _, _, certificate, proof = example(1000.)
    assert proof["bounded_coast_admitted"] is False


@pytest.mark.parametrize("mutation", ("clock", "fuel_margin", "orbit", "pose", "inventory", "certificate", "claim"))
def test_margins_cannot_be_relabelled_as_fresh_or_certified(mutation):
    _, _, certificate, proof = example()
    changed = deepcopy(proof)
    if mutation == "clock":
        changed["time_s"] += 1
    if mutation == "fuel_margin":
        changed["return_fuel_margin_kg"] += 1
    if mutation == "orbit":
        changed["inputs"]["perigee_altitude_m"] += 1000
    if mutation == "pose":
        changed["inputs"]["coast_axis_error_deg"] += 1
    if mutation == "inventory":
        changed["inputs"]["retained_count"] = 1
    if mutation == "certificate":
        changed["certificate_sha256"] = "other"
    if mutation == "claim":
        changed["physical_recovery_certified"] = True
    assert checked(certificate, changed)["passed"] is False


def test_independent_guard_imports_no_optimizer_or_simulator():
    source = inspect.getsource(independent)
    assert "from ." not in source
    assert "import numpy" not in source
    assert "simulate(" not in source


def test_negative_propulsion_margin_is_not_admitted():
    profile, state, certificate, _ = example()
    engines = list(state.engine_states)
    engines[0] = replace(engines[0], available=False)
    value = tool.evaluate(profile, replace(state, engine_states=tuple(engines)), 26, 100000.)
    proof = {**tool.calculate(value["inputs"], certificate), "certificate_sha256": "explicit_math_fixture"}
    assert proof["return_admitted"] is False
    assert checked(certificate, proof)["passed"] is True
