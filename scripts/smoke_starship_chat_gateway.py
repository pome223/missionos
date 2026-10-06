#!/usr/bin/env python3
"""Exercise explicit simulation commands over a real loopback Gateway HTTP route.

This is a test operator, not evidence of an authenticated human identity.
Start scripts/start_starship_gateway.py separately in fixture or opted-in live mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_sixdof_catalog import SIXDOF_SCENARIOS, SUPERVISED_SCENARIOS, RETAINED_RETURN_SCENARIOS, CATCH_CATALOG, LAUNCH_CATCH_SCENARIO, BOOSTER_RECOVERY_POLICY  # noqa: E402


def sixdof_evidence(scenario: str, plan: dict, study: dict, verification: dict) -> dict:
    """Check the served artifact is the approved integrated runtime, not a replay fixture.

    This HTTP smoke does not independently establish SpaceX fidelity or mission
    success. The production verifier owns the complete saved-state checks.
    """
    assert plan["backend"] == "starship_sixdof", plan
    assert plan["simulation"]["scenario"] == SIXDOF_SCENARIOS[scenario], plan
    assert plan["simulation"]["dt_scale"] == 1.0, plan
    assert plan["simulation"]["duration_override_s"] is None, plan
    assert study["schema"] == "missionos.starship_sixdof_study.v1"
    assert study["provenance"]["profile_sha256"] == plan["simulation"]["profile_sha256"]
    assert study["provenance"]["dt_scale"] == plan["simulation"]["dt_scale"]
    assert study["provenance"]["duration_override_s"] is None
    assert study["provenance"]["return_policy"] == plan["simulation"]["return_policy"]
    assert len(study["runs"]) == 1
    run = study["runs"][0]
    assert run["scenario"] == SIXDOF_SCENARIOS[scenario]
    assert run["outcome"]["six_dof_integrated"] is True
    assert run["outcome"]["attitude_prescribed"] is False
    assert study["provenance"]["physical_execution_invoked"] is False
    assert verification["passed"] is True
    assert verification["mission_completed"] is False
    assert verification["physical_execution"] is False
    samples = run["samples"]
    assert len(samples) > 1 and samples[-1]["time_s"] > samples[0]["time_s"]
    for sample in samples:
        for key, length in (("r_eci_m", 3), ("v_eci_mps", 3),
                            ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
            assert len(sample[key]) == length and all(math.isfinite(x) for x in sample[key])
        assert abs(sum(x * x for x in sample["q_body_to_eci"]) - 1) < 1e-8
    evidence = {
        "six_dof_integrated": True,
        "attitude_prescribed": False,
        "saved_samples": len(samples),
        "simulation_duration_s": run["outcome"]["duration_s"],
        "observed_outcomes": verification["observed_outcomes"],
        "starship_vehicle_validated": False,
    }
    if scenario in SUPERVISED_SCENARIOS:
        supervision = verification["flight_supervision"]
        assert supervision["passed"] is True
        assert run["supervision"]["request"]["allowed_actions"] == plan["flight_supervision"]["allowed_actions"]
        evidence["flight_supervision"] = supervision
    if scenario in RETAINED_RETURN_SCENARIOS:
        retained = verification["retained_return"]
        assert retained["passed"] is True
        assert run["retained_return"]["policy_id"] == plan["return_policy"]["policy_id"] == plan["simulation"]["return_policy"]
        evidence["retained_return"] = retained
    if scenario in CATCH_CATALOG:
        capture = verification["booster_catch"]
        assert capture["passed"] is True
        assert run["catch_record"]["initialization"]["launch_connected"] is False
        assert study["provenance"]["catch_profile_sha256"] == plan["simulation"]["catch_profile_sha256"]
        assert run["outcome"]["catch_verified"] is False
        evidence["booster_catch"] = capture
    if scenario == LAUNCH_CATCH_SCENARIO:
        recovery = verification["booster_recovery"]
        assert recovery["passed"] is True
        assert plan["booster_recovery"]["policy_id"] == BOOSTER_RECOVERY_POLICY
        assert study["provenance"]["booster_policy"] == plan["simulation"]["booster_policy"] == BOOSTER_RECOVERY_POLICY
        assert study["provenance"]["catch_profile_sha256"] == plan["simulation"]["catch_profile_sha256"]
        assert type(recovery["handoff_reached"]) is bool
        assert type(recovery["catch_supported_after_handoff"]) is bool
        assert type(verification["launch_connected_catch_supported"]) is bool
        assert verification["launch_connected_catch_supported"] == (recovery["handoff_reached"] and recovery["catch_supported_after_handoff"])
        assert run["booster_run"] is not None
        assert plan["booster_recovery"]["capture_planning_contract"] == "missionos.starship_capture_planning.v1"
        planning = recovery["capture_planning"]
        record = run["booster_run"]["recovery_record"]
        assert planning["assessed"] is True and planning["dispatch_authority_created"] is False
        assert planning["forecast_dynamics_reexecuted"] is False
        assert planning["forecast_count"] == record["full_coast_prediction_count"]
        assert planning["admissible_prediction_count"] == record["capture_planning"]["admissible_prediction_count"]
        evidence["booster_recovery"] = recovery
        evidence["capture_planning"] = planning
        evidence["launch_connected_catch_supported"] = verification["launch_connected_catch_supported"]
    return evidence


def run(port: int, scenario: str, output: Path) -> dict:
    # Refuse before planning/approval so an old failed attempt is never replaced.
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("starship_smoke_output_must_be_empty")
    output.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port}"
    session = "starship-smoke-" + uuid.uuid4().hex
    turns = []
    context = None

    def turn(text: str, override=None):
        payload = {"operator_instruction": text, "session_id": session}
        if context is not None:
            payload["starship_context"] = context if override is None else override
        request = Request(base + "/missionos/autonomy-conversation/run",
                          data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=45) as response:
            result = json.load(response)
        turns.append({"instruction": text, "response": result})
        (output / "chat-transcript.json").write_text(json.dumps(turns, ensure_ascii=False, indent=2) + "\n")
        return result

    response = turn(f"Starship {scenario} のシミュレーション計画を作成してください。選択するscenarioは {scenario} です。")
    result = response["operation_result"]
    assert result["status"] == "awaiting_approval", result
    assert result["plan"]["scenario"] == scenario, result
    context = response["starship_context"]
    assert result["approval"] is None and result["execution"] == {}
    assert turn("/run")["operation_result"]["status"] == "blocked"
    assert turn("はい")["operation_result"]["status"] == "blocked"
    assert turn("/approve", {**context, "plan_sha256": "0" * 64})["operation_result"]["status"] == "blocked"
    approved = turn("/approve")["operation_result"]
    assert approved["status"] == "approved" and approved["execution"] == {}
    assert approved["approval"]["authenticated_operator_identity"] is False
    if scenario in RETAINED_RETURN_SCENARIOS:
        assert approved["approval"]["return_policy"] == approved["plan"]["return_policy"]
    if scenario in CATCH_CATALOG:
        assert approved["approval"]["scope"] == "local_initialized_booster_catch_simulation"
        assert approved["approval"]["booster_catch"] == approved["plan"]["booster_catch"]
    if scenario == LAUNCH_CATCH_SCENARIO:
        assert approved["approval"]["scope"] == "local_launch_connected_booster_catch_simulation"
        assert approved["approval"]["booster_recovery"] == approved["plan"]["booster_recovery"]
    launched = turn("/run")["operation_result"]
    assert launched["status"] == "running" and launched["execution"]["subprocess_spawned"] is True
    assert turn("/run")["operation_result"]["status"] == "blocked"
    deadline = time.monotonic() + (1200 if scenario == LAUNCH_CATCH_SCENARIO else 600 if scenario in SIXDOF_SCENARIOS else
                                   480 if scenario == "dispenser_jev_shadow" else 300)
    while time.monotonic() < deadline:
        response = turn("/status")
        result = response["operation_result"]
        if result["status"] != "running":
            break
        time.sleep(1)
    assert result["status"] == "verified", result
    assert result["execution"]["worker_receipt_verified"] is True
    if scenario == "dispenser_jev_shadow":
        assert result["execution"]["process_role"] == "jev_shadow_observer_broker"
        assert result["execution"]["simulator_provider_credentials_present"] is False
    else:
        assert result["execution"]["provider_credentials_present"] is False
    assert result["physical_execution"] is False and result["mission_completed"] is False
    hashes = {}
    for name, link in response["artifact_links"].items():
        with urlopen(base + link, timeout=30) as artifact:
            data = artifact.read()
        checksum = hashlib.sha256(data).hexdigest()
        assert checksum == result["execution"]["artifact_sha256"][name]
        (output / name).write_bytes(data)
        hashes[name] = checksum
    report_link = response["artifact_links"]["report.html"]
    try:
        urlopen(base + report_link.replace(session, "wrong-session"), timeout=10)
    except HTTPError as error:
        assert error.code in {400, 404}
    else:
        raise AssertionError("cross-session artifact exposed")
    summary = {
        "schema": "missionos.starship_chat_http_smoke.v1", "scenario": scenario,
        "status": result["status"], "test_operator": True,
        "authenticated_human_identity_verified": False,
        "planner_invocation": result["planner_invocation"],
        "plan_sha256": result["plan"]["sha256"],
        "worker_pid": result["execution"]["worker_pid"],
        "worker_receipt_verified": True,
        "process_role": result["execution"].get("process_role", "simulation_worker"),
        "provider_credentials_present_in_process": result["execution"]["provider_credentials_present"],
        "simulator_provider_credentials_present": result["execution"].get("simulator_provider_credentials_present", False),
        "artifact_sha256": hashes, "report_path": report_link,
        "physical_execution": False, "mission_completed": False,
    }
    if scenario in SIXDOF_SCENARIOS:
        summary.update(sixdof_evidence(
            scenario, result["plan"], json.loads((output / "study.json").read_text()),
            json.loads((output / "verification.json").read_text()),
        ))
    (output / "chat-receipt.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (output / "chat-transcript.json").write_text(json.dumps(turns, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", choices=("dispenser_comparison", "flight14_inspired", "dispenser_jev_shadow", *SIXDOF_SCENARIOS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("invalid loopback port")
    print(json.dumps(run(args.port, args.scenario, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
