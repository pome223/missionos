"""Real final configuration/manifest validation, never registration permission.

Only Git cleanliness is replaced for this uncommitted development test. Every
configuration constructor, source hash, backend and plan validator is real.
"""

from copy import deepcopy
from hashlib import sha256
import socket
import subprocess

import pytest

from src.runtime import starship_final_plan as plan, starship_final_shared as shared
from src.runtime import starship_sixdof as dyn, starship_physics as physics


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    # Existing developer venv may omit the workspace core package. Resolve the
    # actual current source rather than stubbing environment() or importing a
    # different checkout's editable installation.
    monkeypatch.syspath_prepend(str(shared.ROOT / "packages/missionos-core/src"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Plan contracts cannot run physics, providers or processes")

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (physics, "step"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr(shared, "clean_commit", lambda: "2" * 40)


@pytest.fixture
def configuration():
    return plan.configuration(shared.read(shared.relative(plan.CONFIG)))


def pins(configuration, mode="fixture"):
    return {
        "kind": "fixed_source_boundary_preflight"
        if mode == "fixture"
        else "accepted_fixture_cohort",
        "same_configuration_sha256": shared.digest(configuration),
        "artifacts": [
            {
                "path": "README.md",
                "sha256": sha256(shared.relative("README.md").read_bytes()).hexdigest(),
            }
        ],
    }


@pytest.fixture
def registered(configuration):
    return plan.register(
        run_id="contract-only-not-approved",
        case_id="T",
        mode="fixture",
        prerequisite_pins=pins(configuration),
    )


def rehash(row):
    row["plan_sha256"] = shared.digest({k: v for k, v in row.items() if k != "plan_sha256"})
    return row


def test_real_configuration_and_full_source_manifest_project_without_execution(
    registered, configuration
):
    from src.runtime.starship_final_visual import FinalVisualPolicy
    from src.runtime.starship_final_prefix_supervision import FinalPrefixSupervisionPolicy

    before = shared.wire(registered)
    checked = plan.validate_plan(registered, current=True)
    projected = plan.verification_inputs(registered)
    assert checked == registered and before == shared.wire(registered)
    assert checked["source_sha256"] == shared.source_manifest([*plan.INPUTS, *plan.RUNTIME_SOURCES])
    assert checked["execution_authority_granted"] is False
    assert (
        projected["visual_policy"] == FinalVisualPolicy(**configuration["visual_policy"]).record()
    )
    assert (
        projected["supervision_policy"]
        == FinalPrefixSupervisionPolicy(**configuration["supervision_policy"]).record()
    )
    assert projected["sensor_policy"] == {"mode": "fixture", **configuration["sensor_policy"]}
    assert projected["backend"] == shared.environment()
    assert projected["source_commit"] == "2" * 40
    assert len(projected["source_sha256"]) > 50


def test_strata_are_lowest_manifest_seed_per_family_and_not_flight_search():
    from src.runtime.starship_final_faults import selection

    strata = plan.stratified_selections()
    assert set(strata) == set(plan.CASE_FAMILIES)
    for case, record in strata.items():
        assert record["selected_family"] == plan.CASE_FAMILIES[case]
        assert record == selection(record["seed"]) and 0 <= record["seed"] < 1000
        assert all(
            selection(seed)["selected_family"] != record["selected_family"]
            for seed in range(record["seed"])
        )
        assert record["candidate_selection_from_outcomes"] is False
        assert record["provider_input"] is False and record["harness_only"] is True


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "authority",
        "context",
        "case",
        "composition",
        "source",
        "backend",
        "commit",
        "mode",
        "prerequisite",
        "input",
        "unknown",
        "run",
        "configuration",
    ],
)
def test_source_backend_plan_and_authority_tampering_cannot_become_executable(registered, change):
    row = deepcopy(registered)
    if change == "hash":
        row["plan_sha256"] = "0" * 64
    elif change == "authority":
        row["execution_authority_granted"] = True
    elif change == "context":
        row["context"]["vehicle_id"] = "other-vehicle"
    elif change == "case":
        row["case_id"] = "A"
    elif change == "composition":
        row["composition"]["showcase"] = False
    elif change == "source":
        row["source_sha256"]["src/runtime/starship_final_native.py"] = "0" * 64
    elif change == "backend":
        row["backend"]["numpy"] = "changed"
    elif change == "commit":
        row["source_commit"] = "3" * 40
    elif change == "mode":
        row["mode"] = "live"
    elif change == "prerequisite":
        row["prerequisites"]["same_configuration_sha256"] = "0" * 64
    elif change == "input":
        row["input_sha256"][shared.PROFILE] = "0" * 64
    elif change == "unknown":
        row["producer_passed"] = True
    elif change == "run":
        row["run_id"] = "../escape"
    else:
        row["configuration"]["bounds"]["automatic_retry"] = True
    if change != "hash":
        rehash(row)
    with pytest.raises(ValueError):
        plan.validate_plan(row, current=True)


@pytest.mark.parametrize(
    "change", ["window", "clock", "retry", "budget", "catch", "deepcap", "mass", "mode"]
)
def test_configuration_rejects_scope_budget_and_control_changes(configuration, change):
    row = deepcopy(configuration)
    if change == "window":
        row["supervision_policy"]["booster_window_s"] = 35.0
    elif change == "clock":
        row["visual_policy"]["first_counter_delay_s"] = 0.0
    elif change == "retry":
        row["bounds"]["automatic_retry"] = True
    elif change == "budget":
        row["bounds"]["per_mission_wall_s"] = 3600.0
    elif change == "catch":
        row["authority"]["tower_catch_allowed"] = True
    elif change == "deepcap":
        row["authority"]["permanent_deepseek_call_count_cap"] = 1
    elif change == "mass":
        row["ascent_settings"]["dry_mass_kg"] = 100000.0
    else:
        row["sensor_policy"]["mode"] = "live"
    with pytest.raises((ValueError, TypeError)):
        plan.configuration(row)


def test_registration_never_accepts_wrong_prerequisite_mode_or_private_path(configuration):
    proof = pins(configuration)
    for change in ("mode", "absolute", "escape", "hash", "empty"):
        value = deepcopy(proof)
        if change == "mode":
            value["kind"] = "accepted_fixture_cohort"
        elif change == "absolute":
            value["artifacts"][0]["path"] = "/Users/someone/private.json"
        elif change == "escape":
            value["artifacts"][0]["path"] = "../private.json"
        elif change == "hash":
            value["artifacts"][0]["sha256"] = "not-a-hash"
        else:
            value["artifacts"] = []
        with pytest.raises(ValueError):
            plan.register(
                run_id="not-authorized", case_id="T", mode="fixture", prerequisite_pins=value
            )
