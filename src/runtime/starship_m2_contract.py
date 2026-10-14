"""One bounded grant for deployment and continuation from its observed state.

This is model-development authority, not physical flight approval. The outer
provider policy must be enforced by the M2 broker: the unchanged legacy Director
contract alone does not enforce Jev-only deployment. No physics executes here.
"""

from dataclasses import asdict
import json
import math

from .starship_operational_trials import REGISTERED_CASES, SCOPE_CLOSURE_CASE, is_trial
from .starship_call_limits import combined_call_limit
from .starship_deployment_handoff import validate_handoff
from .starship_mission_director import contract as deployment_contract
from .starship_replanning import ROOT, contract as return_contract, digest
from .starship_return_prediction import STATE_KEYS
from .starship_sixdof_mission import vehicle

SCHEMA = "missionos.starship_m2_authority.v1"
SCOPE = "local_simulation_deployment_to_observed_return_continuation"
CASES = ("normal", "operations_notice", "release_fault", "hold_resume", "orbital_update", *REGISTERED_CASES)
CHECKPOINT_TIME_S = 1000.7000000000407
PROFILE = "examples/spaceflight/starship-sixdof-profile.json"


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _same(a, b):
    return digest(a) == digest(b)


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def m2_contract(mode, case, *, legacy_call_limits=False, authority_gap_reasoning=None):
    if mode not in ("fixture", "live") or case not in CASES:
        raise ValueError("m2_mode_or_case_not_registered")
    if case == "hold_resume" and mode != "fixture":
        raise ValueError("m2_synthetic_hold_fixture_only")
    from .starship_return_time_search import CONFIG as search_config
    deployment = deployment_contract(mode)
    return_case = case if is_trial(case) else "comparison_orbital_update" if case == "orbital_update" else "event_normal"
    # A scope-capable trial explicitly grants an exception-reasoning route. It
    # selects a provider only; without bound active authority it is ineligible.
    # Other M2 grants retain their exact prior envelope.
    if authority_gap_reasoning is None:
        authority_gap_reasoning = case == SCOPE_CLOSURE_CASE
    if type(authority_gap_reasoning) is not bool or (authority_gap_reasoning and case != SCOPE_CLOSURE_CASE):
        raise ValueError("m2_authority_gap_reasoning_not_registered")
    returning = return_contract(mode, return_case, legacy_call_limits=legacy_call_limits,
                               authority_gap_reasoning=authority_gap_reasoning)
    jev = deployment["maximum_jev_calls"] + returning["maximum_jev_calls"] if mode == "live" else 0
    llm = returning["maximum_llm_calls"] if mode == "live" else 0
    return {
        "schema": SCHEMA, "scope": SCOPE, "mode": mode, "case": case,
        "deployment_case": "normal" if case in ("hold_resume", "orbital_update") or is_trial(case) else case,
        "return_case": return_case,
        "return_time_search": _copy(search_config),
        "deployment_response_fault": "hold_deployment_start" if case == "hold_resume" else None,
        "deployment_authority": deployment, "return_authority": returning,
        "profile_sha256": digest(json.loads((ROOT / PROFILE).read_text())),
        "checkpoint_time_s": CHECKPOINT_TIME_S,
        "handoff_policy": "closed_deployment_exact_state_observed_inventory_generation_v1",
        "continuation_policy": "advance_existing_state_never_assign_prediction_state",
        "phase_provider_policy": {
            "deployment": "jev_only_with_remaining_deadline",
            "return": "existing_return_authority_with_deadline_headroom",
            "phase_mailboxes": "separate_request_protocols_bound_to_one_run",
        },
        "maximum_jev_calls": jev, "maximum_llm_calls": llm,
        "maximum_model_calls": combined_call_limit(jev, llm),
        "approval_policy": "one_preflight_grant_both_phases_no_implicit_expansion",
        "ship_return_dispatch_authorized": True,
        "dispatch_rule": "current_generation_and_fresh_independent_return_checks_required",
        "no_admissible_return": "bounded_coast_then_unresolved_not_success",
        "physical_execution_authorized": False,
    }


def m2_contract_matches(authority):
    """Verify current or finite historical authority without upgrading it.

    New workers still require the current factory's complete approval. This
    compatibility check is for recorded receipts and their timeout validation.
    """
    try:
        gaps = (False, True) if authority["case"] == SCOPE_CLOSURE_CASE else (False,)
        return any(_same(authority, m2_contract(authority["mode"], authority["case"],
                    legacy_call_limits=legacy, authority_gap_reasoning=gap))
                   for legacy in (False, True) for gap in gaps)
    except (KeyError, TypeError, ValueError):
        return False


def _deployment_evidence(handoff, launch):
    """Cross-link the counter's observation intervals with recorded operations."""
    observed = handoff["release_observations"]
    releases = [e for e in launch["events"] if e["event"] == "payload_released"]
    _require(len(releases) == len(observed) == handoff["released_count"], "m2_release_count_evidence")
    _require(all(_finite(e["time_s"]) for e in releases)
             and all(a["time_s"] < b["time_s"] for a, b in zip(releases, releases[1:])),
             "m2_release_event_chronology")
    for event, sample in zip(releases, observed):
        _require(sample["previous_observation_time_s"] <= event["time_s"] <= sample["observed_time_s"],
                 "m2_release_observation_interval")
    commands = [{k: r[k] for k in ("action", "time_s")} for r in handoff["history"] if r["kind"] == "command"]
    events = [{k: e[k] for k in ("action", "time_s")} for e in launch["events"]
              if e["event"] == "managed_mission_command" and e["point"].startswith("deployment")]
    _require(_same(commands, events), "m2_deployment_command_evidence")
    recorded = []
    for record in launch["mission_director"]["records"]:
        if record["clock_id"] != "ship":
            continue
        request, dispatch, later = record["request"], record["dispatch"], record["later_observation"]
        _require(request["point"].startswith("deployment") and dispatch is not None and later is not None,
                 "m2_deployment_confirmation_missing")
        _require(request["observation"]["time_s"] <= dispatch["time_s"] < later["time_s"]
                 <= handoff["execution_state"]["time_s"], "m2_deployment_confirmation_time")
        recorded.append({k: dispatch[k] for k in ("action", "time_s")})
    _require(_same(commands, recorded), "m2_director_command_binding")
    _require(not any(e["event"] in ("return_requested", "deorbit_ignition_command")
                     for e in launch["events"]), "m2_ship_already_returning")


def validate_deployment_origin(profile, envelope, handoff, launch):
    """Return copied continuation inputs; never execute, admit or reset a plant.

    Hash and recorded-evidence checks do not establish independent physical
    accuracy. The composite verifier must still check the executed trajectory,
    sensor/event linkage, candidate admission and subsequent return outcome.
    """
    try:
        _require(type(envelope) is dict and _same(envelope, m2_contract(envelope["mode"], envelope["case"])),
                 "m2_authority_not_current")
        validate_handoff(handoff)
        _require(digest(profile) == envelope["profile_sha256"] == handoff["profile_sha256"],
                 "m2_profile_binding")
        _require(handoff["authority_sha256"] == digest(envelope), "m2_handoff_authority_binding")
        _require(_same(launch["mission_director"]["envelope"], envelope["deployment_authority"])
                 and launch["mission_director"]["response_fault"] == envelope["deployment_response_fault"],
                 "m2_deployment_authority_binding")
        _require(launch["mission_management_case"] == envelope["deployment_case"]
                 and launch["scenario"] == "launch", "m2_deployment_case_binding")
        outcome = launch["outcome"]
        _require(outcome["orbit_gate_reached"] is True and outcome["phase"] == "orbital_coast"
                 and outcome["termination"] == "time_limit", "m2_coast_handoff_required")
        _require(type(outcome["payload_released_count"]) is int
                 and outcome["payload_released_count"] == handoff["released_count"]
                 and profile["payload"]["count"] == handoff["total_count"], "m2_inventory_binding")
        _require(_same(launch["final_state"], handoff["execution_state"]), "m2_exact_state_continuation")
        last = launch["samples"][-1]
        _require(last["phase"] == "orbital_coast"
                 and _same({k: last[k] for k in STATE_KEYS}, handoff["execution_state"]),
                 "m2_final_sample_binding")
        when = handoff["execution_state"]["time_s"]
        _require(_finite(when) and math.isclose(when, envelope["checkpoint_time_s"], rel_tol=0., abs_tol=1e-8)
                 and outcome["duration_s"] == when, "m2_checkpoint_time")
        expected_vehicle = _copy(asdict(vehicle(profile, payload_count=handoff["retained_count"])))
        _require(_same(launch["final_vehicle"], expected_vehicle), "m2_retained_vehicle_binding")
        _deployment_evidence(handoff, launch)
        cutoffs = [e["time_s"] for e in launch["events"] if e["event"] == "orbit_cutoff_command"]
        _require(len(cutoffs) == 1 and _finite(cutoffs[0]) and 0 <= cutoffs[0] < when,
                 "m2_original_cutoff_required")
        scheduled = cutoffs[0] + profile["guidance"]["coast_before_return_s"]
        _require(_finite(scheduled) and when < scheduled, "m2_original_return_window_expired")
        return _copy({
            "execution_state": handoff["execution_state"], "final_vehicle": launch["final_vehicle"],
            "observation_origin": handoff["origin"], "scheduled_s": scheduled,
            "original_cutoff_s": cutoffs[0],
            "original_deadline_s": scheduled + envelope["return_authority"]["maximum_delay_s"],
            **{k: handoff[k] for k in ("retained_count", "released_count", "generation",
                                     "handoff_sha256", "authority_sha256", "profile_sha256")},
        })
    except (KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError("invalid_m2_deployment_origin") from error
