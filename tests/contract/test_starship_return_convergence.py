"""Diagnostic isolation, clock preservation and evidence-failure boundaries."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from scripts import probe_starship_return_convergence as probe


def test_profile_scaling_is_explicit_and_does_not_change_source():
    original = {"integration": {"powered_dt_s": .1, "coast_dt_s": .25, "sample_interval_s": 2.},
                "guidance": {"deorbit_max_alignment_deg": 5.}}
    before = deepcopy(original)
    for name, (factor, _) in probe.CASES.items():
        actual = probe.fixed_profile(original, name)
        assert actual["integration"] == {"powered_dt_s": .1*factor,
            "coast_dt_s": .25*factor, "sample_interval_s": 2.}
        assert actual["guidance"] == original["guidance"]
    assert original == before


@pytest.mark.parametrize("subdivisions", [1, 2, 4])
def test_rk4_refinement_retains_command_and_absolute_clock(monkeypatch, subdivisions):
    calls, trace = [], {}
    command, vehicle = object(), object()
    y = [0.]*14
    y[13] = 100.

    def original(values, craft, held, available, dt, gravity, j2, atmosphere, force, torque, gradient, time_s=0.):
        calls.append((craft, held, available, dt, time_s, force, torque))
        out = list(values)
        out[0] += dt
        return out

    monkeypatch.setattr(probe.dyn, "_rk4", original)
    original_update, original_command = probe.ReturnController.update, probe.ReturnController.command
    with probe.diagnostic_hooks(subdivisions, trace):
        out = probe.dyn._rk4(y, vehicle, command, (True,), .2, True, True, True,
                             (1., 2., 3.), (4., 5., 6.), True, 123.)
    assert out[0] == pytest.approx(.2)
    assert len(calls) == subdivisions
    assert all(c[0] is vehicle and c[1] is command for c in calls)
    assert [c[4] for c in calls] == pytest.approx([123.+i*.2/subdivisions for i in range(subdivisions)])
    assert [c[3] for c in calls] == pytest.approx([.2/subdivisions]*subdivisions)
    assert trace["rk4_calls"] == 1 and trace["rk4_substeps"] == subdivisions
    assert trace["command_ticks"] == 0
    assert probe.dyn._rk4 is original
    assert probe.ReturnController.update is original_update
    assert probe.ReturnController.command is original_command
    assert y[0] == 0.


def test_hooks_restore_on_timeout_and_refuse_unmodeled_fuel_crossing(monkeypatch):
    original_update, original_command = probe.ReturnController.update, probe.ReturnController.command

    def crossing(values, *args):
        out = list(values)
        out[13] = -1.
        return out

    monkeypatch.setattr(probe.dyn, "_rk4", crossing)
    with pytest.raises(ValueError, match="crossed_fuel_zero"):
        with probe.diagnostic_hooks(2, {}):
            probe.dyn._rk4([1.]*14, None, None, (), .1, True, True, True, (), (), True)
    assert probe.dyn._rk4 is crossing
    with pytest.raises(TimeoutError):
        with probe.diagnostic_hooks(4, {}):
            raise TimeoutError("deadline")
    assert probe.dyn._rk4 is crossing
    assert probe.ReturnController.update is original_update
    assert probe.ReturnController.command is original_command


@pytest.mark.parametrize("invalid", [True, 0, 3, 2.0])
def test_invalid_subdivision_rejected(invalid):
    with pytest.raises(ValueError):
        with probe.diagnostic_hooks(invalid, {}):
            pass


def test_exact_comparison_ignores_only_uncompared_runtime_metadata():
    source = {key: {} for key in ("origin", "opportunity_window", "return_controller", "final_state")}
    source.update(events=[], samples=[], return_time_s=100., outcome={"contact_receipt": None}, wall_time_s=3.)
    changed = deepcopy(source)
    changed["wall_time_s"] = 100.
    assert probe.compare(source, changed)["physical_exact"]
    changed["events"] = [{"time_s": 101., "event": "return_requested"}]
    assert not probe.compare(source, changed)["physical_exact"]


def test_coordinate_comparison_uses_each_contact_time():
    state = {"omega_body_rad_s": [0., 0., 0.]}
    contact = {"point_eci_m": [6378137., 0., 0.], "event_time_s": 100.,
        "q_body_to_eci": [math.sqrt(.5), 0., math.sqrt(.5), 0.],
        "surface_normal_eci": [1., 0., 0.], "surface_relative_speed_mps": 4., "propellant_kg": 30000.}
    a = {"final_state": state, "outcome": {"contact_receipt": contact}}
    b = deepcopy(a)
    b["outcome"]["contact_receipt"]["event_time_s"] += 10.
    delta = probe.compare(a, b)["contact_difference"]
    assert delta["eci_chord_m"] == 0
    assert delta["ecef_chord_m"] > 4000.
    assert delta["ecef_chord_m"] == delta["same_point_rotation_only_chord_m"]


def test_bounded_writer_counts_actual_serialized_size_and_preserves_file(tmp_path, monkeypatch):
    value = {"large": list(range(50))}
    actual = len((json.dumps(value, indent=2, allow_nan=False)+"\n").encode())
    monkeypatch.setitem(probe.LIMITS, "maximum_output_bytes", actual-1)
    with pytest.raises(ValueError, match="output_budget"):
        probe.write_bounded(tmp_path/"result.json", value, tmp_path)
    assert not (tmp_path/"result.json").exists()
    monkeypatch.setitem(probe.LIMITS, "maximum_output_bytes", actual*3)
    probe.write_bounded(tmp_path/"result.json", value, tmp_path)
    before = (tmp_path/"result.json").read_bytes()
    with pytest.raises(FileExistsError):
        probe.write_bounded(tmp_path/"result.json", {"overwritten": True}, tmp_path)
    assert (tmp_path/"result.json").read_bytes() == before


def test_cli_requires_opt_in_and_fresh_output_before_reading_inputs(tmp_path):
    argv = ["--reference-prediction", "missing.json", "--case", "baseline", "--expected-reference-sha256", "0"*64,
            "--expected-profile-sha256", "0"*64, "--output-dir", str(tmp_path/"new")]
    with pytest.raises(SystemExit) as error:
        probe.main(argv)
    assert error.value.code == 2 and not (tmp_path/"new").exists()
    argv[-1] = str(tmp_path)
    with pytest.raises(SystemExit) as error:
        probe.main(["--approve-simulation", *argv])
    assert error.value.code == 2


def test_cli_rejects_unfrozen_input_before_creating_output(tmp_path):
    reference, profile = tmp_path/"reference.json", tmp_path/"profile.json"
    reference.write_text("{}")
    profile.write_text("{}")
    with pytest.raises(SystemExit) as error:
        probe.main(["--approve-simulation", "--reference-prediction", str(reference),
            "--profile", str(profile), "--case", "baseline",
            "--expected-reference-sha256", "0"*64, "--expected-profile-sha256", "0"*64,
            "--output-dir", str(tmp_path/"case")])
    assert error.value.code == 2 and not (tmp_path/"case").exists()
