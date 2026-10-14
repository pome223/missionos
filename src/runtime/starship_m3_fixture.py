"""Fixture composition: shared-clock prefix into the existing M2 return actor.

The first bounded connection requires the booster to have physically terminated
before the Ship checkpoint. It never silently freezes an unfinished booster.
This module adds no flight model, model provider, or authenticated approval.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .starship_artifacts import write_verified_input
from .starship_m2_contract import m2_contract
from .starship_m3_prefix import m3_prefix_contract, run_fixture as run_prefix, sources as prefix_sources
from .starship_m3_prefix_verifier import verify as verify_prefix
from .starship_replanning import digest


def contract(case, *, inflight=None):
    prefix, inner = m3_prefix_contract(case), m2_contract("fixture", case)
    authority = {
        "schema": "missionos.starship_m3_fixture_composition_authority.v1",
        "scope": "concurrent_fixture_prefix_to_existing_ship_return",
        "mode": "fixture", "case": case, "prefix_authority": prefix,
        "prefix_authority_sha256": digest(prefix), "m2_authority": inner,
        "m2_authority_sha256": digest(inner), "maximum_attempts": 1,
        "maximum_model_calls": 0, "maximum_human_inflight_commands": 0,
        "booster_continuation": "requires_surface_contact_before_ship_checkpoint",
        "ascent_and_booster_decisions": "fixture_only",
        "ship_return_dispatch_authorized": True,
        "dispatch_rule": "unchanged_m2_independent_fresh_return_checks",
        "physical_execution_authorized": False,
    }
    if inflight is not None:
        authority.update(inflight_contract=deepcopy(inflight), inflight_contract_sha256=digest(inflight),
                         maximum_inflight_scope_amendments=1,
                         initial_return_scope="nominal_only_with_candidate_bound_host_amendment")
    return authority


def sources(*, include_inflight=False):
    from .starship_m2_runner import sources as m2_sources
    result = {**m2_sources(), **prefix_sources(),
              "src/runtime/starship_m3_fixture.py": sha256(Path(__file__).read_bytes()).hexdigest()}
    if include_inflight:
        from . import starship_inflight_approval, starship_inflight_runtime, starship_inflight_executor
        for module in (starship_inflight_approval, starship_inflight_runtime, starship_inflight_executor):
            result["src/runtime/"+Path(module.__file__).name] = sha256(Path(module.__file__).read_bytes()).hexdigest()
    return result


def validate_prefix(prefix, authority, profile, *, expected_prefix_authority=None, expected_run_id=None):
    """Refuse incomplete parallel physics before constructing a return actor."""
    expected_prefix_authority = (m3_prefix_contract(authority["case"])
        if expected_prefix_authority is None else expected_prefix_authority)
    if (digest(expected_prefix_authority) != digest(m3_prefix_contract(authority["case"]))
            or digest(prefix["authority"]) != digest(expected_prefix_authority)):
        raise ValueError("m3_prefix_outer_authority_mismatch")
    if expected_run_id is not None and prefix.get("run_id") != expected_run_id:
        raise ValueError("m3_prefix_run_id_mismatch")
    checked = verify_prefix(prefix)
    if not checked["passed"] or not checked["handoff_ready"]:
        raise ValueError("m3_prefix_evidence_or_handoff_unavailable")
    if (digest(prefix["authority"]["m2_authority"]) != digest(authority)
            or digest(prefix["profile"]) != digest(profile)):
        raise ValueError("m3_prefix_inner_authority_or_profile_mismatch")
    run, handoff = prefix["prefix_run"], prefix["handoff"]
    booster = run["booster_run"]
    contact = booster.get("contact")
    if (booster["shared_clock_checkpoint"]["unfinished"] is not False
            or booster["outcome"]["termination"] != "surface_contact"
            or not isinstance(contact, dict) or contact.get("contact") is not True
            or booster["final_state"]["time_s"] > handoff["execution_state"]["time_s"]):
        raise ValueError("m3_unfinished_booster_requires_concurrent_return_scheduler")
    if digest(handoff["execution_state"]) != digest(run["final_state"]):
        raise ValueError("m3_prefix_state_reset")
    return checked


def produce_prefix(profile, authority, output, *, expected_prefix_authority=None, run_id="m3-prefix-fixture"):
    """Persist the prefix before the independent continuation guard runs."""
    if (expected_prefix_authority is not None and digest(expected_prefix_authority)
            != digest(m3_prefix_contract(authority["case"]))):
        raise ValueError("m3_prefix_outer_authority_mismatch")
    return run_prefix(profile, authority["case"], output, run_id=run_id)


def run_fixture(profile, case, output, *, run_id="m3-fixture", inflight_runtime=None):
    """One new launch plus same-state return; only with a separate run budget."""
    from .starship_m2_runner import run_simulation
    from .starship_m2_verifier import verify as verify_m2
    output, approved = Path(output), contract(case, inflight=
        inflight_runtime["inflight"] if inflight_runtime is not None else None)
    if output.exists():
        raise ValueError("m3_fresh_output_required")
    output.mkdir(parents=True, mode=0o700)
    write_verified_input(output/"authority.json", approved)
    write_verified_input(output/"approval-binding.json", {
        "schema": "missionos.starship_m3_fixture_approval_binding.v1",
        "authority_sha256": digest(approved), "mode": "fixture",
        "human_approval_authenticated": False})
    inner = run_simulation(profile, case, output/"mission", mode="fixture",
        authority=approved["m2_authority"], run_id=run_id, concurrent_prefix=True,
        expected_prefix_authority=approved["prefix_authority"],
        inflight_runtime=inflight_runtime)
    prefix = inner.get("concurrent_prefix")
    prefix_check = verify_prefix(prefix) if prefix is not None else None
    # The outer input source manifest is frozen by the M2 runner before launch.
    inner_check = verify_m2(inner, expected_authority=approved["m2_authority"],
        expected_sources=inner["source_sha256"], expected_return_sources=inner["return_source_sha256"],
        expected_run_id=run_id)
    result = {
        "schema": "missionos.starship_m3_fixture_composition.v1", "authority": approved,
        "authority_sha256": digest(approved), "m2_study": inner,
        "prefix_verification": prefix_check, "return_verification": inner_check,
        "same_state_continuation_checked": bool(prefix_check and prefix_check["passed"] and inner_check["passed"]),
        "booster_outcome": prefix["prefix_run"]["booster_run"]["outcome"] if prefix else None,
        "ascent_and_booster_decisions": "fixture_only", "model_calls": 0,
        "human_inflight_commands": 0, "m3_completed": False,
        "mission_completed": False, "physical_execution": False,
    }
    if inflight_runtime is not None:
        result["inflight_approval_verification"] = {
            "performed": False, "reason": "host_ledger_and_runtime_record_required",
            "scope_amendment_success_claimed": False}
    write_verified_input(output/"study.json", result)
    write_verified_input(output/"summary.json", {k:v for k,v in result.items() if k not in {"m2_study", "authority"}})
    return result
