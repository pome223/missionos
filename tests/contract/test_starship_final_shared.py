"""Final helpers preserve typed declarations without running a plant/provider."""
from hashlib import sha256
import json
import socket
import sys
from types import SimpleNamespace

import pytest

from src.runtime import starship_final_shared as final


@pytest.fixture(autouse=True)
def no_physics_or_network(monkeypatch):
    from src.runtime import starship_physics, starship_sixdof, starship_sixdof_mission

    def forbidden(*_args, **_kwargs):
        raise AssertionError("helper_must_not_integrate_a_plant_or_call_a_provider")

    for owner, name in ((starship_physics, "step"), (starship_sixdof, "step"),
        (starship_sixdof_mission, "simulate"), (starship_sixdof_mission, "simulate_concurrent")):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def ascent_record(outages=False):
    return {"policy": "state_insertion_v7", "finite_mode": True,
        "stack_pitch_offset_deg": 0., "ship_pitch_offset_deg": 0.,
        "shutdowns": ([{"engine_name": "booster_main_32", "time_s": 76.},
            {"engine_name": "ship_main_3", "time_s": 437.}] if outages else []),
        "objectives": {"first_burn_perigee_m": -20000., "first_burn_apogee_m": 300000.,
            "insertion_altitude_m": 275000., "final_perigee_m": 220000., "minimum_apogee_headroom_m": 50000.},
        "control_options": {"first_burn_only_bias": True, "moving_reference_tracking": False},
        "insertion_options": {"maximum_restart_time_s": 4000., "maximum_burn_duration_s": 60.,
            "shutdown_settle_s": 5., "maximum_radial_speed_mps": 20.,
            "maximum_attitude_error_deg": 5., "maximum_body_rate_rad_s": .02}}


def braking_record():
    return {"schema": "missionos.booster_terminal_recovery_settings.v1",
        "policy_id": "availability_terminal_recovery_v1", "powered_terminal_alignment": False,
        "shutdown_tail_only": True, "low_pressure_reference_capture": True,
        "powered_reference_capture": True, "residual_authority_capture": True,
        "energy_terminal_descent": True, "planned_capture_impulse_compensation": True,
        "terminal_horizontal_braking": True}


@pytest.mark.parametrize("outages", [False, True])
def test_registered_ascent_has_all_nested_types_and_lossless_record(outages):
    from src.runtime.starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
        AscentScreenSettings, AscentShutdown, StateInsertionOptions)
    from src.runtime.starship_state_mission import ExperimentalStateMissionAscentSettings
    record = ascent_record(outages)
    before = final.wire(record)
    settings = final.ascent_settings(record)
    assert type(settings) is ExperimentalStateMissionAscentSettings
    assert type(settings.ascent) is AscentScreenSettings
    assert type(settings.ascent.objectives) is AscentCoastOrbitObjectives
    assert type(settings.ascent.control_options) is AscentControlOptions
    assert type(settings.ascent.insertion_options) is StateInsertionOptions
    assert all(type(row) is AscentShutdown for row in settings.ascent.shutdowns)
    assert tuple(row.engine_name for row in settings.ascent.shutdowns) == (
        ("booster_main_32", "ship_main_3") if outages else ())
    assert final.wire(settings.ascent.record()) == before == final.wire(record)
    assert settings.record()["mission_authority_granted"] is False
    assert settings.record()["qualified_return"] is False


@pytest.mark.parametrize("mutation", ["wrapper", "unknown", "missing", "policy", "shutdown_type",
    "shutdown_unknown", "shutdown_timing", "objective_unknown", "objective_changed", "control_missing",
    "control_bool", "insertion_unknown", "insertion_missing", "insertion_changed", "nonfinite", "offset"])
def test_ascent_rejects_unregistered_or_incomplete_declarations(mutation):
    row = ascent_record(True)
    if mutation == "wrapper":
        row = {"ascent": row}
    elif mutation == "unknown":
        row["qualified_return"] = True
    elif mutation == "missing":
        del row["shutdowns"]
    elif mutation == "policy":
        row["policy"] = "coast_orbit_v5"
    elif mutation == "shutdown_type":
        row["shutdowns"] = tuple(row["shutdowns"])
    elif mutation == "shutdown_unknown":
        row["shutdowns"][0]["injected"] = True
    elif mutation == "shutdown_timing":
        row["shutdowns"][0]["time_s"] = 77.
    elif mutation == "objective_unknown":
        row["objectives"]["allow_relaxation"] = True
    elif mutation == "objective_changed":
        row["objectives"]["final_perigee_m"] = 210000.
    elif mutation == "control_missing":
        del row["control_options"]["moving_reference_tracking"]
    elif mutation == "control_bool":
        row["control_options"]["first_burn_only_bias"] = 1
    elif mutation == "insertion_unknown":
        row["insertion_options"]["already_verified"] = True
    elif mutation == "insertion_missing":
        del row["insertion_options"]["maximum_body_rate_rad_s"]
    elif mutation == "insertion_changed":
        row["insertion_options"]["shutdown_settle_s"] = 4.
    elif mutation == "nonfinite":
        row["ship_pitch_offset_deg"] = float("nan")
    elif mutation == "offset":
        row["stack_pitch_offset_deg"] = -5.
    with pytest.raises(ValueError):
        final.ascent_settings(row)


@pytest.mark.parametrize("record", [{"schema": "missionos.booster_terminal_recovery_settings.v1",
    "policy_id": "availability_terminal_recovery_v1"}, braking_record()])
def test_booster_control_preserves_the_sparse_production_record(record):
    from src.runtime.starship_booster_terminal_control import BoosterTerminalRecoverySettings
    before = final.wire(record)
    settings = final.booster_control(record)
    assert type(settings) is BoosterTerminalRecoverySettings
    assert final.wire(settings.record()) == before == final.wire(record)


@pytest.mark.parametrize("mutation", ["schema", "policy", "missing", "extra", "bool", "dependency", "default"])
def test_booster_control_rejects_noncanonical_and_unvalidated_records(mutation):
    row = braking_record()
    if mutation == "schema":
        row["schema"] = "old"
    elif mutation == "policy":
        row["policy_id"] = "different"
    elif mutation == "missing":
        del row["policy_id"]
    elif mutation == "extra":
        row["already_landed"] = True
    elif mutation == "bool":
        row["terminal_horizontal_braking"] = 1
    elif mutation == "dependency":
        del row["planned_capture_impulse_compensation"]
    elif mutation == "default":
        row = {"schema": row["schema"], "policy_id": row["policy_id"], "shutdown_tail_only": False}
    with pytest.raises(ValueError):
        final.booster_control(row)


@pytest.fixture
def model_files(tmp_path, monkeypatch):
    root = final.ROOT
    for name in (final.PROFILE, final.SITES, final.GOAL):
        target = tmp_path/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root/name).read_bytes())
    monkeypatch.setattr(final, "ROOT", tmp_path)
    return tmp_path, final.read(tmp_path/final.PROFILE)


def test_model_sites_and_goal_are_typed_launch_bound_and_lossless(model_files):
    root, profile = model_files
    from src.runtime.starship_return_sites import ReturnSites
    from src.runtime.starship_splashdown import SplashdownGoal
    sites, goal = final.sites_and_goal(profile)
    assert type(sites) is ReturnSites and type(goal) is SplashdownGoal
    assert final.wire(sites.to_dict()) == final.wire(final.read(root/final.SITES))
    assert final.wire(goal.to_dict()) == final.wire(final.read(root/final.GOAL))
    assert goal.reference_site_sha256 == sites.divert.sha256
    assert goal.to_dict()["physical_execution"] is False
    assert sites.capture.latitude_deg == profile["launch"]["latitude_deg"]


@pytest.mark.parametrize("mutation", ["extra", "claim", "ordering", "profile", "reference", "missing"])
def test_sites_loader_rejects_lost_metadata_or_unbound_geometry(model_files, mutation):
    root, profile = model_files
    catalog, goal = final.read(root/final.SITES), final.read(root/final.GOAL)
    if mutation == "extra":
        catalog["already_approved"] = True
    elif mutation == "claim":
        catalog["safe_landing_area_verified"] = True
    elif mutation == "ordering":
        catalog["sites"].reverse()
    elif mutation == "profile":
        profile["launch"]["longitude_deg"] += .1
    elif mutation == "reference":
        goal["reference_site_sha256"] = "0"*64
    elif mutation == "missing":
        del catalog["coordinate_datum"]
    (root/final.SITES).write_text(json.dumps(catalog))
    (root/final.GOAL).write_text(json.dumps(goal))
    with pytest.raises(ValueError):
        final.sites_and_goal(profile)


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "/tmp/absolute", "src/../file",
    "./src/file", "src//file", "src/file/", "src\\file", "src/\x00file"])
def test_only_canonical_confined_relative_paths_are_admitted(tmp_path, monkeypatch, name):
    monkeypatch.setattr(final, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        final.relative(name)


def test_relative_path_rejects_symlink_leaf_and_ancestors(tmp_path, monkeypatch):
    monkeypatch.setattr(final, "ROOT", tmp_path)
    (tmp_path/"real").mkdir()
    (tmp_path/"real"/"file.json").write_text("{}")
    (tmp_path/"alias").symlink_to(tmp_path/"real", target_is_directory=True)
    (tmp_path/"file.json").symlink_to(tmp_path/"real"/"file.json")
    for name in ("alias/file.json", "file.json"):
        with pytest.raises(ValueError, match="symlink_or_path_escape"):
            final.relative(name)
    assert final.relative("real/file.json") == tmp_path/"real"/"file.json"


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}'])
def test_json_cannot_hide_duplicates_or_nonfinite_values(raw):
    with pytest.raises(ValueError):
        final.parse(raw)


@pytest.mark.parametrize("maximum", [True, 0, -1, 1.5])
def test_input_budget_is_a_positive_integer(tmp_path, maximum):
    path = tmp_path/"input.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="positive_input_budget"):
        final.read(path, maximum)


def test_input_size_symlink_and_exclusive_output_preserve_existing_record(tmp_path):
    path = tmp_path/"input.json"
    path.write_bytes(b'{"x":1}\n')
    assert final.read(path, len(path.read_bytes())) == {"x": 1}
    with pytest.raises(ValueError, match="oversized"):
        final.read(path, len(path.read_bytes())-1)
    alias = tmp_path/"alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        final.read(alias)
    out = tmp_path/"saved.json"
    actual_hash = final.save(out, {"result": "not_execution"})
    raw = out.read_bytes()
    assert actual_hash == sha256(raw).hexdigest()
    assert final.read(out) == {"result": "not_execution"}
    with pytest.raises(FileExistsError):
        final.save(out, {"replaced": True})
    assert out.read_bytes() == raw


def test_manifest_is_sorted_exact_and_detects_source_and_extra_input_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(final, "ROOT", tmp_path)
    names = ("src/runtime/starship_final_shared.py", "src/gateway/starship_live.py",
        "packages/missionos-core/src/missionos_core/__init__.py", "src/intelligence/space_ops_investigator.py",
        "src/gateway/server.py", "src/config/settings.py", "src/agents/model_config.py", "examples/input.json")
    for index, name in enumerate(names):
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content {index}\n".encode())
    manifest = final.source_manifest(extra=["examples/input.json"])
    assert list(manifest) == sorted(names)
    assert manifest == {name: sha256((tmp_path/name).read_bytes()).hexdigest() for name in sorted(names)}
    final.check_manifest(manifest)
    for name in (names[0], names[-1]):
        original = (tmp_path/name).read_bytes()
        (tmp_path/name).write_bytes(original+b"changed")
        with pytest.raises(ValueError, match="source_or_input_changed"):
            final.check_manifest(manifest)
        (tmp_path/name).write_bytes(original)
    final.check_manifest(manifest)


@pytest.mark.parametrize("manifest", [{}, [], {"src/file": "0"*63}, {"src/file": "G"*64}, {"src/file": True}])
def test_invalid_manifest_never_becomes_a_source_binding(manifest):
    with pytest.raises(ValueError):
        final.check_manifest(manifest)


def test_source_imports_never_load_historical_runners():
    before = set(sys.modules)
    final.ascent_settings(ascent_record(True))
    final.booster_control(braking_record())
    assert not any(name.startswith("scripts.run_starship_") for name in set(sys.modules)-before)


@pytest.mark.parametrize("foreign", [False, True])
def test_environment_requires_all_runtime_namespace_paths_in_current_checkout(tmp_path, monkeypatch, foreign):
    monkeypatch.setattr(final, "ROOT", tmp_path)
    binary = tmp_path/"python"
    binary.write_bytes(b"interpreter bytes")
    monkeypatch.setattr(final.sys, "executable", str(binary))
    for name in ("src", "scripts", "missionos_core"):
        paths = [str(tmp_path/name)]
        if foreign and name == "missionos_core":
            paths.append(str(tmp_path.parent/"old-worktree"/name))
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(__path__=paths))
    if foreign:
        with pytest.raises(ValueError, match="foreign_checkout_import"):
            final.environment()
    else:
        record = final.environment()
        assert record["python"] == sys.version
        assert record["executable_sha256"] == sha256(binary.read_bytes()).hexdigest()


def test_clean_commit_rejects_uncommitted_tree(monkeypatch):
    observed = []

    def command(args, **kwargs):
        observed.append((args, kwargs))
        return b"?? new-file\n"

    monkeypatch.setattr(final.subprocess, "check_output", command)
    with pytest.raises(ValueError, match="clean_committed_source"):
        final.clean_commit()
    assert len(observed) == 1


def test_clean_commit_returns_exact_head_after_status_check(monkeypatch):
    responses = iter((b"", "a"*40+"\n"))
    monkeypatch.setattr(final.subprocess, "check_output", lambda *_a, **_k: next(responses))
    assert final.clean_commit() == "a"*40
