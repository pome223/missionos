"""Maintained final-mission configuration and persistence primitives.

These helpers do not import historical experiment runners, authorize work,
change physics, or re-pin a spent plan. File names and environments are checked
again by the fresh source-bound registration before an execution can start.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "examples/spaceflight/starship-sixdof-profile.json"
SITES = "examples/spaceflight/starship-return-sites-model-test.json"
GOAL = "examples/spaceflight/starship-splashdown-goal.json"


def require(value, reason):
    if not value:
        raise ValueError(reason)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(wire(value)).hexdigest()


def parse(raw):
    def pairs(values):
        result = dict(values)
        require(len(result) == len(values), "final_duplicate_json_key")
        return result
    result = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("final_nonfinite_json")))
    wire(result)
    return result


def relative(name):
    require(type(name) is str and name and "\x00" not in name and "\\" not in name,
        "final_relative_path_required")
    value = Path(name)
    require(value.parts and not value.is_absolute() and ".." not in value.parts
        and value.as_posix() == name, "final_relative_path_required")
    result = ROOT/name
    require(result.resolve() == result.absolute() and result.resolve().is_relative_to(ROOT.resolve()),
        "final_symlink_or_path_escape")
    return result


def read(path, maximum_bytes=32*2**20):
    require(type(maximum_bytes) is int and maximum_bytes > 0, "final_positive_input_budget_required")
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum_bytes,
        "final_input_missing_symlink_or_oversized")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        raw = stream.read(maximum_bytes+1)
    require(len(raw) <= maximum_bytes, "final_input_exceeds_budget")
    return parse(raw)


def save(path, value):
    raw = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()+b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return sha256(raw).hexdigest()


def source_manifest(extra=()):
    require(type(extra) in (tuple, list) and all(type(name) is str for name in extra),
        "final_manifest_paths_required")
    # Include import initializers and the Gateway's non-Starship routing/core
    # dependencies too. A source-bound mission must not silently keep running
    # after one of those files changes merely because its basename differs.
    names = {str(p.relative_to(ROOT)) for p in (ROOT/"src").rglob("*.py")}
    names |= {str(p.relative_to(ROOT)) for package in ("missionos-core", "missionos-cli", "missionos-gateway")
        for p in (ROOT/"packages"/package/"src").rglob("*.py")}
    names |= set(extra)
    return {name: sha256(relative(name).read_bytes()).hexdigest() for name in sorted(names)}


def check_manifest(manifest):
    require(type(manifest) is dict and manifest, "final_manifest_required")
    for name, expected in manifest.items():
        require(type(expected) is str and len(expected) == 64 and all(c in "0123456789abcdef" for c in expected)
            and sha256(relative(name).read_bytes()).hexdigest() == expected, "final_source_or_input_changed")


def clean_commit():
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT)
    require(not dirty.strip(), "final_clean_committed_source_required")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def environment():
    import numpy
    import scipy
    import src
    import scripts
    import missionos_core
    for module in (src, scripts, missionos_core):
        names = list(getattr(module, "__path__", ()))
        require(names and all(Path(name).resolve().is_relative_to(ROOT) for name in names),
            "final_foreign_checkout_import")
    return {"python": sys.version, "platform": sys.platform, "machine": platform.machine(),
        "numpy": numpy.__version__, "scipy": scipy.__version__,
        "executable_sha256": sha256(Path(sys.executable).read_bytes()).hexdigest()}


def ascent_settings(record):
    """Reconstruct only the registered v7 ascent, without running a mission.

    The input is ``AscentScreenSettings.record()``, not the enclosing
    experimental settings record. Explicit nested types preserve each
    constructor's validation and the frozen healthy/outage configuration gate.
    """
    from .starship_state_mission import ExperimentalStateMissionAscentSettings
    from .starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
        AscentScreenSettings, AscentShutdown, StateInsertionOptions)
    require(type(record) is dict and set(record) == {"stack_pitch_offset_deg", "ship_pitch_offset_deg",
        "finite_mode", "policy", "shutdowns", "objectives", "control_options", "insertion_options"},
        "final_ascent_record_required")
    require(record["policy"] == "state_insertion_v7", "final_ascent_policy_changed")
    require(type(record["shutdowns"]) is list and all(type(row) is dict for row in record["shutdowns"])
        and all(type(record[key]) is dict for key in ("objectives", "control_options", "insertion_options")),
        "final_ascent_nested_records_required")
    try:
        settings = ExperimentalStateMissionAscentSettings(ascent=AscentScreenSettings(
            stack_pitch_offset_deg=record["stack_pitch_offset_deg"],
            ship_pitch_offset_deg=record["ship_pitch_offset_deg"], policy=record["policy"],
            finite_mode=record["finite_mode"],
            shutdowns=tuple(AscentShutdown(**row) for row in record["shutdowns"]),
            objectives=AscentCoastOrbitObjectives(**record["objectives"]),
            control_options=AscentControlOptions(**record["control_options"]),
            insertion_options=StateInsertionOptions(**record["insertion_options"])))
    except (KeyError, TypeError) as exc:
        raise ValueError("final_ascent_nested_record_fields_changed") from exc
    require(wire(settings.ascent.record()) == wire(record), "final_ascent_record_roundtrip_changed")
    return settings


def booster_control(record):
    """Reconstruct the canonical sparse terminal-control declaration only."""
    from .starship_booster_terminal_control import BoosterTerminalRecoverySettings
    require(type(record) is dict and record.get("schema") == "missionos.booster_terminal_recovery_settings.v1"
        and record.get("policy_id") == "availability_terminal_recovery_v1", "final_booster_policy_changed")
    try:
        settings = BoosterTerminalRecoverySettings(**{key: value for key, value in record.items()
            if key not in ("schema", "policy_id")})
    except TypeError as exc:
        raise ValueError("final_booster_record_fields_changed") from exc
    require(wire(settings.record()) == wire(record), "final_booster_record_roundtrip_changed")
    return settings


def sites_and_goal(profile):
    """Load hashable model goals and bind them to the actual launch profile."""
    from .starship_return_sites import ReturnSite, ReturnSites, validate_return_site
    from .starship_splashdown import SplashdownGoal
    catalog, goal_record = read(relative(SITES)), read(relative(GOAL))
    require(type(catalog) is dict and type(catalog.get("sites")) is list and len(catalog["sites"]) == 2,
        "final_site_catalog_required")
    try:
        sites = ReturnSites(catalog["profile_id"], *(ReturnSite.from_dict(row) for row in catalog["sites"]))
    except (KeyError, TypeError) as exc:
        raise ValueError("final_site_catalog_fields_changed") from exc
    goal = SplashdownGoal.from_dict(goal_record)
    require(wire(sites.to_dict()) == wire(catalog) and wire(goal.to_dict()) == wire(goal_record),
        "final_site_or_goal_roundtrip_changed")
    validate_return_site(sites.capture, profile, terminal_goal="surrogate_pin_support")
    validate_return_site(sites.divert, profile, terminal_goal="model_surface_contact")
    goal.validate_site(sites.divert)
    return sites, goal
