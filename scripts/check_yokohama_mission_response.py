#!/usr/bin/env python3
"""Run explicit CPU-only aircraft wind-assistance cases through Mission Assurance."""

import argparse
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.yokohama_mission_response import evaluate_fixture  # noqa: E402


def cases():
    base = dict(
        request_id="sea-gust",
        phase="sea_inbound",
        region="sea",
        observed_sim_s=10,
        now_sim_s=11,
        battery_fraction=0.7,
        calm_duration_sim_s=0,
        hazard_active=True,
        local_fallback_policy_ref="authored-local-contingency-v1",
        refuges=[
            dict(
                id="shore-A",
                kind="shore",
                eta_s=50,
                feasibility_age_s=1,
                preapproved=True,
                route_clear=True,
                wind_reachable=True,
                arrival_area_available=True,
                estimated_arrival_battery_fraction=0.5,
            )
        ],
    )
    yield "sea-reachable-shore", copy.deepcopy(base), "replan", "divert_to_preapproved_refuge"
    r = copy.deepcopy(base)
    r["refuges"][0]["wind_reachable"] = False
    yield "sea-no-reachable-refuge", r, "operator_escalation", "retain_preconfigured_local_failsafe"
    r = copy.deepcopy(base)
    r.update(region="land", safe_hold_verified=True)
    yield "land-safe-hold", r, "hold", "hold_at_verified_site"
    r = copy.deepcopy(base)
    r.update(
        hazard_active=False,
        calm_duration_sim_s=12,
        stable_vehicle_observed=True,
        fresh_route_revalidated=True,
        mission_deadline_valid=True,
    )
    yield "calm-reobserve-resume", copy.deepcopy(r), "continue", "recapture_and_reassess_mission"
    r["fresh_route_revalidated"] = False
    yield "calm-stale-route", r, "operator_escalation", "retain_preconfigured_local_failsafe"
    r = copy.deepcopy(base)
    r["battery_fraction"] = 0.15
    yield "reserve-insufficient", r, "operator_escalation", "retain_preconfigured_local_failsafe"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error("Preserve existing evidence; choose a new output")
    records = []
    for name, report, kind, action in cases():
        report["request_id"] = name
        evidence = evaluate_fixture(report)
        proposal = evidence["proposal"]
        passed = (
            proposal["proposed_response_kind"] == kind
            and proposal["parameters"]["action"] == action
            and proposal["judgment_status"] == "proposal_guardrail_passed"
            and not any(
                proposal[k]
                for k in (
                    "model_inference_invoked",
                    "operator_approved",
                    "dispatch_request_sent",
                    "physical_execution_invoked",
                )
            )
        )
        records.append(dict(case=name, passed=passed, **evidence))
    output = dict(
        scope="authored CPU Mission Assurance fixture; no AP dispatch or wind-flight result",
        status="passed" if all(r["passed"] for r in records) else "failed",
        cases=records,
    )
    a.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(dict(status=output["status"], cases=len(records))))
    return 0 if output["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
