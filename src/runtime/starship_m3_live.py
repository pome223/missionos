"""Explicit MissionOS grant for live ascent, booster, deployment and return.

This uses the existing shared-clock plant and M2 continuation, not a new flight
model. Fixture and live grants are distinct; no provider key enters the worker.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

from .starship_operational_trials import REGISTERED_CASES, is_trial, trial_manifest
from .starship_m3_inflight import CASES as INFLIGHT_CASES, policy as inflight_policy
from .starship_artifacts import write_verified_checkpoint, write_verified_input
from .starship_call_limits import call_count_allowed, combined_call_limit
from .starship_m2_contract import PROFILE, m2_contract
from .starship_m3_prefix import m3_prefix_contract
from .starship_replanning import ROOT, digest

SCENARIOS = {"sixdof_m3_orbital_comparison": "orbital_update",
             "sixdof_m3_release_fault": "release_fault",
             **{f"sixdof_m3_{case}": case for case in INFLIGHT_CASES},
             **{f"sixdof_m3_{case}": case for case in REGISTERED_CASES}}
SCOPE = "local_simulation_concurrent_ascent_booster_deployment_return_control"
EXTRA_SOURCES = (
    "src/runtime/starship_m3_live.py", "src/runtime/starship_recovery_reports.py", "scripts/run_starship_m3.py", "scripts/start_starship_gateway.py",
    "src/intelligence/starship_m3_broker.py", "src/gateway/starship_chat.py",
    "src/intelligence/starship_mission_planner.py", "src/runtime/starship_runtime_budget.py",
    "src/runtime/starship_mission_control.py", "scripts/run_starship_mission_worker.py",
    "src/runtime/starship_m3_inflight.py", "src/runtime/starship_m3_completion.py",
    "src/gateway/starship_inflight_approval.py",
    "src/runtime/assets/starship_inflight_operator.html",
)


def contract(mode, case="orbital_update", *, legacy_call_limits=False, authority_gap_reasoning=None, legacy_booster_jev=False, recorded_booster_report=None):
    if mode not in {"fixture", "live"} or (case not in {"orbital_update", "release_fault", *INFLIGHT_CASES} and not is_trial(case)):
        raise ValueError("m3_control_case_not_registered")
    base_case = INFLIGHT_CASES.get(case, case)
    inner = m2_contract(mode, base_case, legacy_call_limits=legacy_call_limits,
                        authority_gap_reasoning=authority_gap_reasoning)
    prefix = m3_prefix_contract(base_case, mode=mode, legacy_call_limits=legacy_call_limits,
                                authority_gap_reasoning=authority_gap_reasoning, legacy_booster_jev=legacy_booster_jev,
                                recorded_booster_report=recorded_booster_report)
    extra = 5 if mode == "live" else 0
    result = {
        "schema": "missionos.starship_m3_control_authority.v1", "scope": SCOPE,
        "mode": mode, "case": case,
        "m2_authority": inner, "m2_authority_sha256": digest(inner),
        "prefix_authority": prefix, "prefix_authority_sha256": digest(prefix),
        "maximum_ascent_calls": 4 if mode == "live" else 0,
        "maximum_booster_calls": 1 if mode == "live" else 0,
        "maximum_jev_calls": inner["maximum_jev_calls"] + extra,
        "maximum_llm_calls": inner["maximum_llm_calls"],
        "maximum_model_calls": combined_call_limit(inner["maximum_model_calls"], extra),
        "maximum_attempts": 1, "maximum_wall_time_s": 2400,
        "maximum_output_bytes": 1200000000, "minimum_free_bytes": 1073741824,
        "phase_provider_policy": "jev_ascent_booster_deployment_deepseek_admitted_orbital_comparison",
        "booster_continuation": "surface_contact_before_ship_return_handoff",
        "approval_policy": "one_explicit_preflight_grant_no_implicit_scope_expansion",
        "inflight_scope_amendment_enabled": False,
        "maximum_human_inflight_commands": 0, "physical_execution_authorized": False,
    }

    if inner["return_authority"].get("authority_gap_reasoning"):
        result["phase_provider_policy"] = (
            "jev_ascent_booster_deployment_deepseek_admitted_comparison_or_authority_gap"
        )
    if mode == "live" and not legacy_booster_jev:
        result["maximum_jev_calls"] -= 1
        result["maximum_llm_calls"] = None
        result["phase_provider_policy"] = "jev_ascent_deployment_deepseek_booster_report_and_admitted_return"
    if case in INFLIGHT_CASES:
        result.update(inflight_scope_amendment_enabled=True, maximum_inflight_scope_amendments=1,
            inflight_policy=inflight_policy(inner["return_authority"]))
    return result


def sources():
    from .starship_m3_fixture import sources as existing_sources
    from src.intelligence.starship_booster_recovery_evidence import REPORT_SOURCES
    return {**existing_sources(include_inflight=True), **{
        name: sha256((ROOT / name).read_bytes()).hexdigest() for name in (*EXTRA_SOURCES, "src/intelligence/starship_booster_recovery_evidence.py", *REPORT_SOURCES)}}


def plan_fields(mode, case, source_sha256):
    authority = contract(mode, case)
    report_sources = authority["prefix_authority"].get("booster_recovery_report_source_sha256", {})
    if any(source_sha256.get(name) != value for name, value in report_sources.items()):
        raise ValueError("approved_recovery_report_source_mismatch")
    from src.intelligence.starship_m2_broker import require_live_configuration
    require_live_configuration(authority)
    if case in INFLIGHT_CASES:
        from .starship_m3_inflight import operator_configuration
        operator_configuration()
    return {
        "backend": "starship_concurrent_mission_control", "mission_envelope": authority,
        "release_policy": "observed_ascent_and_deployment_then_same_plant_return",
        "model_authority": "bounded_concurrent_mission_control",
        "jev_authority": "ascent_booster_deployment_and_return",
        "simulation": {"case": case, "profile": PROFILE,
            "profile_sha256": source_sha256[PROFILE], "dt_scale": 1.0,
            "maximum_wall_time_s": authority["maximum_wall_time_s"]},
        "verification_scope": "phase_bound_model_decisions_same_flight_observations_and_contact",
        "limitations": [
            "一度の承認で上昇・ブースター・放出・帰還の限定された判断を委任します。実機権限はありません。",
            "短い判断はJev、軌道上の比較は時間余裕を独立確認してDeepSeekへ渡します。未応答は記録し、期限と実行制約を守ります。",
            "海域・待機資源は開発用の設定です。接触条件の達成は実機回収や水中生存性の証明ではありません。",
        ],
    }


def validate_worker(authority, *, mode, case, run_id, mailbox):
    from .starship_m2_control import validate_worker_input
    if digest(authority) != digest(contract(mode, case)):
        raise ValueError("m3_control_authority_mismatch")
    validate_worker_input(authority["m2_authority"], mode=mode, case=authority["m2_authority"]["case"],
                          run_id=run_id, mailbox=mailbox)
    if mode == "live":
        root = Path(mailbox)
        for phase in ("ascent", "booster"):
            if not (root / phase).is_dir() or (root / phase).is_symlink():
                raise ValueError("m3_phase_mailbox_required")
        binding = json.loads((root / "m3-authority.json").read_text())
        if binding != {"run_id": run_id, "authority_sha256": digest(authority)}:
            raise ValueError("m3_mailbox_authority_mismatch")


def produce_prefix(profile, authority, output, *, expected_prefix_authority, run_id, mailbox):
    from .starship_m3_prefix import run_prefix
    expected = m3_prefix_contract(authority["case"], mode=authority["mode"])
    if digest(expected_prefix_authority) != digest(expected):
        raise ValueError("m3_prefix_outer_authority_mismatch")
    return run_prefix(profile, authority["case"], output, mode=authority["mode"],
                      mailbox=mailbox, run_id=run_id)


def validate_prefix(prefix, authority, profile, *, expected_prefix_authority, expected_run_id):
    from .starship_m3_prefix_verifier import verify as verify_prefix
    expected = m3_prefix_contract(authority["case"], mode=authority["mode"])
    if (digest(expected_prefix_authority) != digest(expected)
            or digest(prefix["authority"]) != digest(expected)
            or prefix["run_id"] != expected_run_id
            or digest(prefix["profile"]) != digest(profile)):
        raise ValueError("m3_prefix_identity_mismatch")
    checked = verify_prefix(prefix)
    if not checked["passed"] or not checked["handoff_ready"]:
        raise ValueError("m3_live_prefix_evidence_or_handoff_unavailable")
    run, handoff = prefix["prefix_run"], prefix["handoff"]
    booster = run["booster_run"]
    if (booster["shared_clock_checkpoint"]["unfinished"] is not False
            or booster["outcome"]["termination"] != "surface_contact"
            or (booster.get("contact") or {}).get("contact") is not True
            or booster["final_state"]["time_s"] > handoff["execution_state"]["time_s"]
            or digest(handoff["execution_state"]) != digest(run["final_state"])):
        raise ValueError("m3_live_prefix_continuation_not_closed")
    return checked


def run(profile, case, output, *, mode, authority, mailbox, run_id, inflight_runtime=None, inflight_binding=None):
    from .starship_m2_runner import run_simulation
    validate_worker(authority, mode=mode, case=case, run_id=run_id, mailbox=mailbox)
    if (case in INFLIGHT_CASES) != (inflight_runtime is not None and inflight_binding is not None):
        raise ValueError("m3_registered_inflight_binding_required")
    if case in INFLIGHT_CASES:
        from .starship_m3_inflight import validate_binding
        expected_contract = validate_binding(inflight_binding, authority, run_id=run_id, sources=sources())
        if inflight_runtime["inflight"] != expected_contract:
            raise ValueError("m3_inflight_runtime_binding_mismatch")
    output = Path(output)
    if output.exists():
        raise ValueError("m3_fresh_output_required")
    output.mkdir(parents=True, mode=0o700)
    frozen = sources()
    base_case = authority["m2_authority"]["case"]
    trial = trial_manifest(base_case) if is_trial(base_case) else None
    if trial is not None:
        write_verified_input(output / "operational-trial.json", trial)
    inner = run_simulation(profile, base_case, output / "mission", mode=mode,
        authority=authority["m2_authority"], mailbox=mailbox, run_id=run_id,
        concurrent_prefix=True, expected_prefix_authority=authority["prefix_authority"],
        inflight_runtime=inflight_runtime, inflight_outer_authority=authority if inflight_runtime else None,
        inflight_binding=inflight_binding)
    study = {
        "schema": "missionos.starship_m3_control_study.v1", "run_id": run_id,
        "authority": deepcopy(authority), "profile": deepcopy(profile),
        "source_sha256": frozen, "m2_study": inner,
        **({"inflight_binding": deepcopy(inflight_binding)} if inflight_binding is not None else {}),
        **({"operational_trial_manifest": trial} if trial is not None else {}),
        "sources_unchanged": frozen == sources(),
        "human_inflight_commands": 0, "retry_performed": False,
        "physical_execution": False, "m3_completed": False,
    }
    if inflight_binding is not None:
        # A completed checkpoint wakes the host evidence collector. Its atomic
        # reference is published only after the full gzip payload is durable.
        return write_verified_checkpoint(output / "flight-study.json", study)
    return write_verified_input(output / "study.json", study)


def verify(study, *, expected_authority, expected_sources, expected_return_sources, expected_run_id,
           expected_inflight_binding=None, expected_host_evidence=None):
    from .starship_m2_verifier import verify as verify_m2
    from .starship_m3_prefix_verifier import verify as verify_prefix
    from .starship_m3_prefix import EXTRA_SOURCES as prefix_files
    result = {"schema": "missionos.starship_m3_control_verification.v1",
        "passed": False, "issues": [], "case_accepted": False, "case_issues": [],
        "ai_control_demonstrated": False, "m3_completed": False,
        "booster_recovery_judgment_observed": False, "live_phase_decisions": {},
        "mission_completed": False, "physical_execution": False}
    try:
        if (study["schema"] != "missionos.starship_m3_control_study.v1"
                or digest(study["authority"]) != digest(expected_authority)
                or not any(digest(expected_authority) == digest(contract(
                    expected_authority["mode"], expected_authority["case"],
                    legacy_call_limits=legacy, authority_gap_reasoning=gap,
                    legacy_booster_jev=expected_authority["prefix_authority"].get("prefix_provider_policy") == "jev_only",
                    recorded_booster_report=expected_authority["prefix_authority"].get("booster_recovery_report")))
                    for legacy in (False, True) for gap in (None, False))
                or study["run_id"] != expected_run_id or study["source_sha256"] != expected_sources
                or study["sources_unchanged"] is not True
                or study["m3_completed"] is not False
                or study["human_inflight_commands"] != 0
                or study["retry_performed"] is not False or study["physical_execution"] is not False):
            raise ValueError("m3_approved_inputs_or_runtime_mismatch")
        report_sources = expected_authority["prefix_authority"].get("booster_recovery_report_source_sha256", {})
        if any(expected_sources.get(name) != value for name, value in report_sources.items()):
            raise ValueError("approved_recovery_report_source_mismatch")
        base_case = expected_authority["m2_authority"]["case"]
        if is_trial(base_case):
            manifest = study.get("operational_trial_manifest")
            binding = expected_authority["m2_authority"]["return_authority"]["operational_trial"]
            if (digest(manifest) != binding["manifest_sha256"]
                    or digest(manifest) != digest(trial_manifest(base_case))):
                raise ValueError("m3_operational_trial_manifest_mismatch")
        inner = study["m2_study"]
        prefix = inner["concurrent_prefix"]
        if (digest(prefix["authority"]) != expected_authority["prefix_authority_sha256"]
                or digest(inner["profile"]) != digest(study["profile"])
                or digest(prefix["profile"]) != digest(study["profile"])
                or prefix["run_id"] != expected_run_id
                or set(prefix["source_sha256"]) != set(expected_return_sources) | set(prefix_files)
                or any(expected_sources.get(name) != value
                       for name, value in prefix["source_sha256"].items())):
            raise ValueError("m3_body_grant_binding")
        if any(digest(prefix[left]) != digest(inner[right]) for left, right in (
                ("prefix_run", "deployment_run"), ("trace", "trace"), ("handoff", "handoff"))):
            raise ValueError("m3_same_flight_binding")
        if expected_authority["mode"] == "live" and digest(prefix["deployment_attempt_ledger"]) != digest(
                inner["provider_attempt_ledger"]["deployment"]):
            raise ValueError("m3_same_deployment_attempt_history")
        prefix_check = verify_prefix(prefix)
        inner_check = verify_m2(inner, expected_authority=expected_authority["m2_authority"],
            expected_sources=expected_sources, expected_return_sources=expected_return_sources,
            expected_run_id=expected_run_id)
        result.update(prefix_verification=prefix_check, return_verification=inner_check)
        result["issues"] = ["prefix:" + s for s in prefix_check["issues"]] + [
            "return:" + s for s in inner_check["issues"]]
        result["passed"] = prefix_check["passed"] and inner_check["passed"]
        if expected_authority["case"] in INFLIGHT_CASES:
            from .starship_m3_inflight import validate_binding
            from .starship_inflight_executor import verify_inflight_return
            inflight = validate_binding(expected_inflight_binding, expected_authority,
                run_id=expected_run_id, sources=expected_sources)
            host = expected_host_evidence
            if (study.get("inflight_binding") != expected_inflight_binding
                    or inner.get("inflight_contract") != inflight
                    or host is None or study.get("inflight_host_evidence") != host
                    or host.get("binding_sha256") != digest(expected_inflight_binding)
                    or host.get("failure") is not None):
                raise ValueError("m3_inflight_host_binding_required")
            checked = verify_inflight_return(inner["return_study"], expected_contract=inflight,
                host_record=host["host_record"], signed_ledger_record=host["signed_ledger_record"])
            result["inflight_verification"] = checked
            if not checked["passed"]:
                result["issues"].extend("inflight:"+issue for issue in checked["issues"])
                result["passed"] = False
        elif any(key in study for key in ("inflight_binding", "inflight_host_evidence")) or inner.get("inflight_contract") is not None:
            raise ValueError("m3_unapproved_inflight_authority")
        wall = inner["runtime"]["wall_time_s"]
        if type(wall) not in (int, float) or not math.isfinite(wall) or not 0 <= wall < expected_authority["maximum_wall_time_s"]:
            raise ValueError("m3_wall_time_budget")
        extra_ledgers = prefix.get("provider_attempt_ledger") or {}
        extra_calls = sum(len(ledger["attempts"]) for ledger in extra_ledgers.values())
        result["model_calls"] = inner["runtime"]["model_calls"] + extra_calls
        if (not call_count_allowed(result["model_calls"], expected_authority["maximum_model_calls"])
                or not call_count_allowed(inner["runtime"]["model_calls"],
                    expected_authority["m2_authority"]["maximum_model_calls"])
                or any(len(extra_ledgers.get(phase, {}).get("attempts", [])) > expected_authority[key]
                    for phase, key in (("ascent", "maximum_ascent_calls"), ("booster", "maximum_booster_calls")))):
            raise ValueError("m3_total_provider_budget")
        water = prefix_check.get("booster_splashdown") or {}
        result["booster_water_entry_met"] = water.get("controlled_water_entry_envelope_met") is True
        result["case_issues"] = list(inner_check["case_issues"])
        if not result["booster_water_entry_met"]:
            result["case_issues"].append("booster_water_entry_not_met")
        # Actual provider evidence is reported independently of flight survival.
        ascent = prefix["ascent"]["decisions"]
        booster = prefix["prefix_run"]["booster_mission_director"]["records"]
        deployment = inner["deployment_run"]["mission_director"]["records"]
        returning = inner["return_study"]["decisions"]
        def live_adopted(rows, key, ascent=False):
            return any((r.get("response") or {}).get(key, {}).get("model_inference_invoked") is True
                and (r.get("accepted_action") is not None if ascent or "accepted_action" in r
                     else (r.get("dispatch") or {}).get("rules_accepted") is True) for r in rows
                if isinstance((r.get("response") or {}).get(key), dict))
        result["live_phase_decisions"] = {
            "ascent_jev": live_adopted(ascent, "jev_invocation", ascent=True),
            ("booster_deepseek" if prefix["authority"].get("prefix_provider_policy") == "jev_ascent_deepseek_booster_report_v1" else "booster_jev"):
                live_adopted(booster, "llm_invocation" if prefix["authority"].get("maximum_booster_llm_calls") is None and "maximum_booster_llm_calls" in prefix["authority"] else "jev_invocation"),
            "deployment_jev": live_adopted(deployment, "jev_invocation"),
            "return_jev": live_adopted(returning, "jev_invocation"),
            "return_deepseek": live_adopted(returning, "llm_invocation"),
        }
        result["booster_recovery_judgment_observed"] = result["passed"] and any(
            (r.get("response") or {}).get("action") in {"capture", "splashdown"}
            and ((r.get("response") or {}).get("llm_invocation") or {}).get("model_inference_invoked") is True
            for r in booster)
        result["ai_control_demonstrated"] = result["passed"] and all(result["live_phase_decisions"].values())
        # Per-flight survival and record acceptance are separate from aggregate
        # live phase coverage. A normal flight need not invent a DeepSeek call.
        result["live_phase_coverage_complete"] = result["ai_control_demonstrated"]
        result["case_accepted"] = result["passed"] and not result["case_issues"]
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
        result.update(passed=False, case_accepted=False, ai_control_demonstrated=False,
            booster_recovery_judgment_observed=False, live_phase_decisions={})
        result["issues"].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return result
