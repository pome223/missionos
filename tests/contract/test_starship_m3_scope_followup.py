"""Separate scope-coverage scenario; preserve the original frozen campaign."""
from copy import deepcopy
from hashlib import sha256
import json
import math
import time

import pytest

from src.runtime.starship_m3_live import SCENARIOS, contract
from src.runtime.starship_m3_inflight import binding, validate_binding
from src.runtime.starship_operational_trials import delivered_notice, trial_manifest
from src.runtime.starship_replanning import digest

CASE = "inflight_op_scope_01"
BASE = "op_scope_01"
# Evidence identities in the frozen 2026-10-12 completion campaign and 61ebb8fe.
# Keep the full historical authority digest below; only its evidence binding may
# change when the unchanged scenario is qualified against current source.
HISTORICAL_CERTIFICATE_SHA256 = "dc925be6529d2d17a4de38ad89c8ecf66ccfe5e4285f2d0bace77bd20f20eced"
HISTORICAL_RETURN_SOURCES_SHA256 = "fbdff83740011abfd816b26e2183e10e8800dcca295c9b5167b0d2c42d0dabb9"


def _director_authorities(authority):
    return (
        authority["m2_authority"]["deployment_authority"],
        authority["prefix_authority"]["m2_authority"]["deployment_authority"],
        authority["prefix_authority"]["booster_authority"],
    )


def test_historical_scope_authority_is_not_rewritten():
    historical = deepcopy(contract("live", "inflight_op_trial_02", legacy_booster_jev=True))
    for director in _director_authorities(historical):
        director["return_qualification_sha256"] = HISTORICAL_CERTIFICATE_SHA256
        director["return_model_source_sha256"] = HISTORICAL_RETURN_SOURCES_SHA256
    # Rebind only the envelopes containing those two historical evidence fields.
    prefix = historical["prefix_authority"]
    prefix["m2_authority_sha256"] = digest(prefix["m2_authority"])
    prefix["booster_authority_sha256"] = digest(prefix["booster_authority"])
    historical["m2_authority_sha256"] = digest(historical["m2_authority"])
    historical["prefix_authority_sha256"] = digest(prefix)
    assert digest(historical) == (
        "99bd0e46cef377db073c4a9a8ce974fbe9bf3c23d29bee4bb177f7afcf37f13b"
    )
    from src.runtime.starship_m3_completion import FLIGHT_CASES
    assert FLIGHT_CASES["scope_amendment"] == "inflight_op_trial_02"


def test_scope_authority_binds_current_qualification_and_source():
    from src.runtime.starship_return_feasibility import CERTIFICATE, CORE_SOURCES, ROOT
    certificate_sha256 = sha256((ROOT / CERTIFICATE).read_bytes()).hexdigest()
    source_sha256 = digest({name: sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in CORE_SOURCES})
    authority = contract("live", "inflight_op_trial_02")
    for director in _director_authorities(authority):
        assert director["return_qualification_sha256"] == certificate_sha256
        assert director["return_model_source_sha256"] == source_sha256
    prefix = authority["prefix_authority"]
    assert authority["m2_authority_sha256"] == digest(authority["m2_authority"])
    assert authority["prefix_authority_sha256"] == digest(prefix)
    assert prefix["m2_authority_sha256"] == digest(prefix["m2_authority"])
    assert prefix["booster_authority_sha256"] == digest(prefix["booster_authority"])


def test_separate_trial_withdraws_nominal_without_reinstatement():
    old, new = trial_manifest("op_trial_02"), trial_manifest(BASE)
    assert new["case"] == BASE and len(new["publications"]) == 2
    assert new["publications"][0] == old["publications"][0]
    second = new["publications"][1]
    assert second["delivery_at_s"] == old["publications"][1]["delivery_at_s"]
    assert set(second["notice"]["areas"]) == {"west"}
    assert "clearance_reopening_estimate" not in second["notice"]
    assert "east" not in delivered_notice(BASE, 2200.7)["areas"]
    assert "east" not in delivered_notice(BASE, 7732)["areas"]
    assert delivered_notice(BASE, math.nextafter(second["delivery_at_s"], -math.inf)) == new["publications"][0]["notice"]
    assert delivered_notice(BASE, second["delivery_at_s"]) == second["notice"]
    assert not {"expected_action", "winner", "correct_action"}.intersection(new)
    for publication in new["publications"]:
        serialized = json.dumps(publication["notice"])
        assert BASE not in serialized and "select_next_orbit" not in serialized
        assert not {"seed", "profile", "split", "publications", "expected_action"}.intersection(publication["notice"])
    changed = trial_manifest(BASE)
    changed["publications"][1]["notice"]["areas"].clear()
    assert trial_manifest(BASE) == new


def test_new_scope_is_candidate_bound_and_never_preapproved():
    a = contract("live", CASE)
    assert SCENARIOS["sixdof_m3_"+CASE] == CASE
    assert a["m2_authority"]["case"] == a["prefix_authority"]["case"] == BASE
    assert a["inflight_scope_amendment_enabled"] is True
    assert a["maximum_llm_calls"] is None and a["maximum_model_calls"] is None
    assert a["inflight_policy"]["initial_scope"]["commands"] == ["select_nominal", "keep_reserved_return"]
    assert a["m2_authority"]["return_authority"]["operational_trial"]["manifest_sha256"] == digest(trial_manifest(BASE))
    b = binding(a, run_id="scope-followup", plan_sha256="a"*64,
                source_sha256={"source.py": "b"*64}, started=time.monotonic())
    assert validate_binding(b, a, run_id="scope-followup", sources=b["source_sha256"]) == b["inflight_contract"]
    old = contract("live", "inflight_op_trial_02")
    with pytest.raises(ValueError, match="inflight_binding_mismatch"):
        validate_binding(b, old, run_id="scope-followup", sources=b["source_sha256"])


def test_new_case_cannot_smuggle_old_or_widened_outer_authority_into_worker(tmp_path):
    from src.runtime.starship_m2_runner import run_simulation
    a = contract("live", CASE)
    old = contract("live", "inflight_op_trial_02")
    widened = deepcopy(a)
    widened["inflight_policy"]["initial_scope"]["commands"].append("select_next_orbit")
    for outer in (None, old, widened, {**deepcopy(a), "case": "unregistered"}):
        with pytest.raises(ValueError):
            run_simulation({}, BASE, tmp_path/"not-created", mode="live", authority=a["m2_authority"],
                concurrent_prefix=True, expected_prefix_authority=a["prefix_authority"],
                inflight_outer_authority=outer,
                inflight_runtime={"inflight": {}, "request_writer": None, "grant_reader": None,
                    "snapshot_sink": lambda _: None, "clock": time.monotonic})
    assert not (tmp_path/"not-created").exists()


@pytest.mark.parametrize("case", ["op_scope_00", "op_scope_02", "op_scope_01x"])
def test_unregistered_targeted_scenarios_fail_closed(case):
    from src.runtime.starship_replanning import contract as return_contract, notices
    for invoke in (lambda: return_contract("live", case), lambda: notices(case, 1350.),
                   lambda: contract("live", "inflight_"+case)):
        with pytest.raises(ValueError):
            invoke()
