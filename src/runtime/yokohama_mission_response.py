"""CPU fixture adapter for aircraft-requested wind assistance.

This exercises the shared Mission Assurance proposal boundary. It neither
changes AP modes nor declares a refuge safe. Input feasibility must come from
an independent route/reserve checker, and execution needs fresh revalidation.
"""

from datetime import datetime, timezone
import math

from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
)
from src.runtime.yokohama_payload import digest


def select_fixture_response(report):
    """A test judge, deliberately not a deployed weather or flight controller."""
    if report.get("region") not in ("sea", "land"):
        raise ValueError("Explicit observed sea/land region is required")
    for k in ("observed_sim_s", "now_sim_s", "battery_fraction", "calm_duration_sim_s"):
        if not isinstance(report.get(k), (int, float)) or not math.isfinite(report[k]):
            raise ValueError("Finite aircraft observations required")
    if not 0 <= report["now_sim_s"] - report["observed_sim_s"] <= 2:
        raise ValueError("Stale aircraft assistance request")
    if not 0 <= report["battery_fraction"] <= 1 or report["calm_duration_sim_s"] < 0:
        raise ValueError("Invalid reserve or calm window")
    if not report.get("request_id") or not report.get("local_fallback_policy_ref"):
        raise ValueError("Bounded request and preconfigured local fallback required")
    candidates = []
    for c in report.get("refuges", []):
        if not c.get("id") or c.get("kind") not in ("shore", "ship", "land_site"):
            continue
        remaining = c.get("estimated_arrival_battery_fraction")
        age = c.get("feasibility_age_s")
        eta = c.get("eta_s")
        if (
            all(isinstance(v, (int, float)) and math.isfinite(v) for v in (remaining, age, eta))
            and 0 <= age <= 2
            and eta >= 0
            and 0.2 <= remaining <= report["battery_fraction"]
            and c.get("preapproved") is True
            and c.get("route_clear") is True
            and c.get("wind_reachable") is True
            and c.get("arrival_area_available") is True
        ):
            candidates.append(c)
    if report.get("hazard_active") is True or report["battery_fraction"] < 0.2:
        if (
            report["region"] == "land"
            and report.get("safe_hold_verified") is True
            and report["battery_fraction"] >= 0.2
        ):
            return "hold", dict(action="hold_at_verified_site")
        if candidates:
            destination = min(candidates, key=lambda c: c["eta_s"])
            return "replan", dict(
                action="divert_to_preapproved_refuge", refuge_id=destination["id"]
            )
        return "operator_escalation", dict(
            action="retain_preconfigured_local_failsafe",
            policy_ref=report["local_fallback_policy_ref"],
        )
    if (
        report.get("hazard_active") is False
        and report["calm_duration_sim_s"] >= 10
        and report.get("stable_vehicle_observed") is True
        and report.get("fresh_route_revalidated") is True
        and report.get("mission_deadline_valid") is True
        and report["battery_fraction"] >= 0.2
    ):
        return "continue", dict(action="recapture_and_reassess_mission")
    return "operator_escalation", dict(
        action="retain_preconfigured_local_failsafe", policy_ref=report["local_fallback_policy_ref"]
    )


def evaluate_fixture(report):
    kind, parameters = select_fixture_response(report)
    now = datetime.now(timezone.utc).isoformat()
    situation = MissionSituation(
        situation_id=report["request_id"],
        observed_at=now,
        mission_contract=dict(objective="deliver cargo and return; preserve declared reserve"),
        progress=dict(phase=report.get("phase")),
        observations=report,
        constraints=dict(
            local_fallback_policy_ref=report["local_fallback_policy_ref"], model_use="city_only"
        ),
        uncertainty=dict(
            source="authored CPU fixture; route and wind feasibility not measured here"
        ),
        source_refs=(report["request_id"],),
        source_schema_version="missionos.aircraft-wind-request.v1",
        input_digest=digest(report),
        execution_scope="fixture",
    )

    class FixtureJudge:
        def judge(self, prompt):
            return ModelJudgment(
                output=dict(
                    proposed_response_kind=kind,
                    parameters=parameters,
                    rationale="Response conditioned on aircraft region, control capability and candidate feasibility.",
                    expected_outcome="A bounded proposal for independent approval and fresh executor checks.",
                    uncertainty="CPU fixture only; no weather inference or AP dispatch.",
                    operator_question="Review the response within the preapproved contingency scope.",
                ),
                invocation_evidence=dict(invocation_kind="deterministic_fixture"),
                model_inference_invoked=False,
            )

    proposal = MissionAssuranceAgent(FixtureJudge()).evaluate(situation)
    return dict(situation=situation.to_dict(), proposal=proposal.to_dict())
